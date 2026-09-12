"""Load hand-verified real events into the live database.

Use this once, before you have a Ticketmaster key, so the app opens with real
data instead of nothing. Everything loaded here is marked `hand-verified` in
the signals table so you can always tell it apart from classifier output.

Run: python scripts/seed_live.py
     python scripts/seed_live.py --dry-run
"""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from radar import db
from radar.collectors.events import label_for, reminder_item
from radar.config import CONFIG_DIR, load_entities, load_settings
from radar.db import Item
from radar.freshness import days_until, event_status, load_window

# Hand-written because these are real and I checked them. The classifier will
# score everything from here on; this is only the starting position.
VERDICTS = {
    "seed_warped_mexico_2026": {
        "importance": 96, "category": "event_reminder",
        "summary": "Warped Tour's first ever Mexico City edition runs today and tomorrow, "
                   "with Goldfinger, girlfriends and Big Noise artist Mod Sun all on the bill.",
        "repost_angle": "Three Gross Labs acts on one stage, at the first Warped Tour ever "
                        "held in Mexico. Lead with the rarity, not the lineup poster.",
        "caption": "First Warped Tour ever in Mexico. Goldfinger, girlfriends and Mod Sun "
                   "all on the bill. Autódromo Hermanos Rodríguez, today and tomorrow.",
    },
    "seed_warped_mexico_2026_gf": {
        "importance": 88, "category": "event_reminder",
        "summary": "girlfriends play Warped Tour Mexico City today and tomorrow.",
        "repost_angle": "Pull stories from @girlfriendsxo during the set.",
        "caption": "girlfriends at Warped Tour Mexico City. Mexico's first ever Warped.",
    },
    "seed_breakaway_philly_2026": {
        "importance": 92, "category": "event_reminder",
        "summary": "Breakaway Philadelphia is running now at Subaru Park with Tiësto, "
                   "Marshmello and SOFI TUKKER.",
        "repost_angle": "Show-day content. Repost from the festival account while it is live.",
        "caption": "Breakaway Philadelphia, happening now. Tiësto and Marshmello on the bill.",
    },
}

DEFAULT = {
    "importance": 58, "category": "event_announcement",
    "repost_angle": "Announce the date, save the show-day push for the week of.",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="show what would load, write nothing")
    args = ap.parse_args()

    data = yaml.safe_load((CONFIG_DIR / "seed_events.yaml").read_text())
    settings = load_settings()
    window = load_window(settings)
    by_key = {e.key: e for e in load_entities()}

    conn = db.connect()
    db.init(conn)

    print(f"\nSeed verified {data['verified_on']} · window is {window.describe()}")
    print(f"Database: {db.DB_PATH}\n")

    loaded = alerted = skipped = 0
    for ev in data["events"]:
        days, running, inside = event_status(
            ev["event_date"], ev.get("end_date"), window)
        entity = by_key.get(ev["entity_key"])
        name = entity.name if entity else ev["entity_key"]

        when = ("happening now" if running else
                label_for(days) if days is not None and days > 0 else
                f"ended {abs(days)}d ago")
        mark = "ALERT" if inside else "track"
        print(f"  {mark:<6} {name:<26} {ev['event_date']}  {when:<16} {ev['city']}")

        if args.dry_run:
            continue

        record = {k: ev.get(k, "") for k in
                  ("event_id", "entity_key", "name", "event_date", "end_date",
                   "venue", "city", "url", "lineup")}
        record["image_url"] = ""
        db.upsert_event(conn, record)
        loaded += 1

        if not inside:
            skipped += 1
            continue

        item = reminder_item(record, max(days or 0, 0), window, running)
        item.body = (ev.get("note") or item.body).strip()
        item.raw["verified_source"] = ev.get("source", "")
        item_id = db.insert_item(conn, item)
        if not item_id:
            continue

        verdict = {**DEFAULT, **VERDICTS.get(ev["event_id"], {})}
        verdict["relevant"] = True
        verdict["repostable"] = True
        verdict.setdefault("summary", f"{record['name']} on {record['event_date']}.")
        verdict["reasoning"] = f"Hand-verified against {ev.get('source', 'source')}"
        db.save_signal(conn, item_id, verdict, "hand-verified")
        db.set_review(conn, item_id, "pending")
        for m in window.reminder_marks:
            db.mark_event_alerted(conn, record["event_id"], m)
        alerted += 1

    if args.dry_run:
        print("\nDry run, nothing written.")
        return 0

    print(f"\n{loaded} event(s) recorded, {alerted} in the queue now, "
          f"{skipped} tracked for later.")

    gaps = data.get("checked_no_news", [])
    if gaps:
        print("\nChecked and found no news inside the window:")
        for g in gaps:
            print(f"  {by_key.get(g['entity'], type('x', (), {'name': g['entity']})).name:<22} {g['note']}")

    print("\nNext: python -m radar.cli serve")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
