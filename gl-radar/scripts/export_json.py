"""Write data.json for the GL Radar artifact.

The dashboard moved from a hosted HTML page to a Claude artifact, which has a
permanent URL and needs no hosting. The artifact renders the page itself and
reads this file for the content, so the workflow's job is now to publish data
rather than markup.

Writes to the repository root so the raw.githubusercontent.com URL is stable
and short. `public/` is gitignored, which is why it does not go there.

Run: python scripts/export_json.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from radar import db
from radar.config import load_entities, load_settings
from radar.freshness import load_window

OUT = Path(__file__).resolve().parent.parent / "data.json"


def main() -> int:
    settings = load_settings()
    window = load_window(settings)
    entities = load_entities()
    by_key = {e.key: e for e in entities}

    conn = db.connect()
    db.init(conn)

    floor = settings["scoring"]["min_importance"]
    since = window.oldest_allowed.isoformat()
    archive_floor = window.archive_floor.isoformat()

    rows = conn.execute(
        """SELECT i.id, i.entity_key, i.source, i.url, i.title, i.author,
                  i.published_at, i.lang, i.title_en, i.raw,
                  s.category, s.importance, s.summary, s.repost_angle, s.caption
           FROM items i
           JOIN signals s ON s.item_id = i.id
           LEFT JOIN reviews r ON r.item_id = i.id
           WHERE s.relevant = 1
             AND s.importance >= ?
             AND i.published_at >= ?
             AND COALESCE(r.status, 'pending') != 'dismissed'
           ORDER BY i.published_at DESC""",
        (floor, archive_floor),
    ).fetchall()

    items = []
    for r in rows:
        if r["entity_key"] not in by_key:
            continue
        # Anything inside the window is work; everything older is reference.
        view = "pending" if (r["published_at"] or "") >= since else "archive"
        item = {
            "id": r["id"],
            "company": r["entity_key"],
            "title": r["title_en"] or r["title"],
            "url": r["url"] or "",
            "source": r["source"],
            "author": r["author"] or "",
            "published_at": r["published_at"],
            "category": r["category"] or "update",
            "importance": int(r["importance"] or 0),
            "summary": r["summary"] or "",
            "angle": r["repost_angle"] or "",
            "caption": r["caption"] or "",
            "view": view,
        }
        # Keep the original so the card can show what was translated.
        if r["lang"] == "es" and r["title_en"]:
            item["lang"] = "es"
            item["title_original"] = r["title"]
        # Social cards show engagement instead of a category and byline.
        if r["source"] in ("instagram", "linkedin"):
            try:
                raw = json.loads(r["raw"] or "{}")
            except (TypeError, ValueError):
                raw = {}
            if raw.get("likes") is not None:
                item["likes"] = int(raw["likes"] or 0)
            if raw.get("comments") is not None:
                item["comments"] = int(raw["comments"] or 0)
        items.append(item)

    # What the social scraping has cost this month, so it is visible on the
    # board rather than discovered on a bill.
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    try:
        billed = int(db.get_cursor(conn, f"apify:results:{month}") or 0)
    except (TypeError, ValueError):
        billed = 0
    apify_cfg = settings.get("apify") or {}
    rate = float(apify_cfg.get("usd_per_1000_results", 0.50))

    payload = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "apify": {
            "month": month,
            "results": billed,
            "spent_usd": round(billed * rate / 1000.0, 2),
            "budget_usd": float(apify_cfg.get("monthly_budget_usd", 4.0)),
        },
        "window": {
            "lookback_days": window.lookback_days,
            "lookahead_days": window.lookahead_days,
            "archive_days": window.archive_days,
        },
        "companies": [
            {
                "key": e.key,
                "name": e.name,
                "sector": getattr(e, "sector", "Other"),
                "instagram": e.instagram[0] if e.instagram else "",
            }
            for e in entities
        ],
        "items": items,
    }

    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    pending = sum(1 for i in items if i["view"] == "pending")
    print(f"\nWrote {OUT}  ({OUT.stat().st_size:,} bytes)")
    print(f"{pending} to review, {len(items) - pending} archived, "
          f"{len(payload['companies'])} companies")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
