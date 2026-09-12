"""Offline end-to-end check. No network, no API keys, no ClickUp writes.

Deliberately adversarial: it plants stale items, a missed show reminder and an
out-of-range config, then asserts the system catches every one.

Run: python scripts/smoke_test.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Set before importing radar.db: fixtures must never touch the live database.
# An earlier version shared one file and its invented test rows were mistaken
# for real findings.
os.environ["RADAR_DB"] = str(Path(__file__).resolve().parent.parent / "data" / "test.db")

from radar import db
from radar.config import load_entities, load_settings
from radar.enrich.classifier import apply_priority
from radar.freshness import is_fresh, is_upcoming, load_window
from radar.sinks.clickup import ClickUp

NOW = datetime.now(timezone.utc)


def ago(days):
    return (NOW - timedelta(days=days)).isoformat()


def ahead(days):
    return (NOW + timedelta(days=days)).date().isoformat()


FIXTURES = [
    # fresh, high value
    (db.Item(entity_key="beeup", source="google_news", source_id="n1",
             title="Gross Labs joins forces with BEEUP to redefine the energy category",
             url="https://grosslabs.com/news/beeup", author="Gross Labs",
             published_at=ago(1)),
     {"relevant": True, "category": "investment", "importance": 88, "repostable": True,
      "summary": "Gross Labs deepens its BEEUP stake as the brand moves into energy drinks.",
      "repost_angle": "Lead with the category expansion, not the cheque.",
      "caption": "BEEUP is moving into energy. Honey first, same as always."}),
    # near-duplicate, must be rejected before it costs an API call
    (db.Item(entity_key="beeup", source="rss", source_id="n2",
             title="Gross Labs Joins Forces With BEEUP To Redefine The Energy Category",
             url="https://othersite.com/beeup-energy", author="Trade Weekly",
             published_at=ago(1)), None),
    # inside the window, mid value
    (db.Item(entity_key="big_noise", source="google_news", source_id="n4",
             title="Big Noise signs a new artist to its publishing arm",
             url="https://example.com/bn", author="Music Trade", published_at=ago(5)),
     {"relevant": True, "category": "milestone", "importance": 62, "repostable": True,
      "summary": "Big Noise adds a publishing signing.", "repost_angle": "Roster growth.",
      "caption": "New to the Big Noise family."}),
    # collision: the Bond film
    (db.Item(entity_key="goldfinger", source="google_news", source_id="n3",
             title="Goldfinger at 60: how the Bond film defined a franchise",
             url="https://filmsite.com/goldfinger-60", author="Film Site",
             published_at=ago(2)),
     {"relevant": False, "category": "noise", "importance": 0, "repostable": False,
      "reasoning": "This is the 1964 James Bond film, not the ska-punk band."}),
]

STALE = db.Item(entity_key="girlfriends", source="google_news", source_id="old1",
                title="girlfriends announced a tour last month",
                url="https://example.com/old", author="Old News", published_at=ago(9))


def main() -> int:
    db.DB_PATH.unlink(missing_ok=True)
    conn = db.connect()
    db.init(conn)
    settings = load_settings()
    window = load_window(settings, now=NOW)
    by_key = {e.key: e for e in load_entities()}
    fails = []

    def check(label, ok, detail=""):
        print(f"   {'ok  ' if ok else 'FAIL'} {label}{('  ' + detail) if detail else ''}")
        if not ok:
            fails.append(label)

    print(f"1. Window contract — {window.describe()}")
    check("lookahead clamped to ceiling",
          load_window({"freshness": {"lookahead_days": 30}}).lookahead_days == 5,
          "asked 30, got 5")
    check("6-day-old item is fresh", is_fresh(ago(6 - 0.1), window))
    check("9-day-old item is stale", not is_fresh(ago(9), window))
    check("show in 2 days is upcoming", is_upcoming(ahead(2), window))
    check("show in 4 days is outside", not is_upcoming(ahead(4), window))
    check("yesterday's show is not upcoming", not is_upcoming(ahead(-1), window))

    print("\n2. Ingest, dedup and stale rejection")
    ids = []
    for item, _ in FIXTURES:
        ids.append(db.insert_item(conn, item))
    check("near-duplicate headline rejected", ids[1] is None)
    check("unique items stored", all(ids[i] for i in (0, 2, 3)))

    # Plant the stale item directly, bypassing the collector guard, to prove the
    # later layers catch what an upstream bug would let through.
    stale_id = db.insert_item(conn, STALE)
    db.save_signal(conn, stale_id, {"relevant": True, "category": "tour_date",
                                    "importance": 70, "repostable": True,
                                    "summary": "Old tour news."}, "fixture")
    db.set_review(conn, stale_id, "pending")
    check("stale item planted for the audit", stale_id is not None, "published 9d ago")

    print("\n3. Scoring with priority boost")
    for (item, verdict), item_id in zip(FIXTURES, ids):
        if not item_id or verdict is None:
            continue
        entity = by_key[item.entity_key]
        raw = verdict["importance"]
        verdict["importance"] = apply_priority(raw, entity.priority, settings)
        db.save_signal(conn, item_id, verdict, "fixture")
        db.set_review(conn, item_id, "pending")
        print(f"        {entity.name:<26} p{entity.priority}  {raw} -> {verdict['importance']}")
    check("priority-1 boost applied", FIXTURES[0][1]["importance"] == 100)

    print("\n4. Upcoming events")
    db.upsert_event(conn, {"event_id": "ev_soon", "entity_key": "breakaway",
                           "name": "Breakaway Philadelphia 2026", "event_date": ahead(1),
                           "venue": "Subaru Park", "city": "Chester, PA",
                           "url": "https://breakawayfestival.com", "lineup": "Tiesto"})
    db.upsert_event(conn, {"event_id": "ev_far", "entity_key": "breakaway",
                           "name": "Breakaway Carolina 2026", "event_date": ahead(14),
                           "venue": "zMAX Dragway", "city": "Concord, NC",
                           "url": "https://breakawayfestival.com", "lineup": "TBA"})
    inside = db.events_between(conn, NOW.date().isoformat(),
                               window.furthest_allowed.isoformat())
    check("only the near show is inside the window",
          [e["event_id"] for e in inside] == ["ev_soon"], f"{len(inside)} of 2")
    check("near show has no alert yet (a real miss)", not db.event_alerted(conn, "ev_soon", 1))

    print("\n5. Supervisor audit")
    from radar.pipeline import Pipeline
    pipe = Pipeline()
    # Pipeline resolves its own thread-local connection
    pipe.window = window
    report = pipe.supervise(repair=True)
    for c in report.checks:
        print(f"   {c.symbol} {c.name:<20} {c.detail[:72]}")
    check("stale item was expired", db.stale_in_queue(conn, window.oldest_allowed.isoformat()) == [])
    check("missed reminder was recovered", db.event_alerted(conn, "ev_soon", 1))
    check("recovery item exists in the queue",
          any("tomorrow" in r["title"] for r in conn.execute(
              "SELECT title FROM items WHERE source='events'")))
    check("audit made repairs", report.repairs >= 2, f"{report.repairs} repaired")

    print("\n5b. Blind-company detection")
    from radar.coverage import gaps, pipeline_ready, report as cov_report

    os.environ.pop("ANTHROPIC_API_KEY", None)
    ready, note = pipeline_ready(settings)
    check("no key still scores, using free rules", ready and "keyword rules" in note)

    forced = {**settings, "classifier": {**settings["classifier"], "mode": "claude"}}
    hard, why = pipeline_ready(forced)
    check("mode 'claude' without a key fails loudly", not hard and "ANTHROPIC_API_KEY" in why)

    from radar.enrich.rules import RuleClassifier
    rc = RuleClassifier(settings)
    bond = rc.classify({"source": "google_news", "body": "",
                        "title": "Goldfinger at 60: how the Bond film defined a franchise"},
                       by_key["goldfinger"])
    check("free classifier drops the Bond film", not bond["relevant"])
    real = rc.classify({"source": "google_news", "body": "",
                        "title": "Goldfinger announce Warped Tour Mexico City date"},
                       by_key["goldfinger"])
    check("free classifier keeps a real announcement above the floor",
          real["relevant"] and real["importance"] >= settings["scoring"]["min_importance"],
          f"scored {real['importance']}")
    owned = rc.classify({"source": "instagram", "body": "", "title": "new drop today"},
                        by_key["beeup"])
    check("owned-source posts need no alias match", owned["relevant"])

    os.environ["ANTHROPIC_API_KEY"] = "test-key-not-used"
    cov = {c.key: c for c in cov_report(conn, settings, [by_key[k] for k in by_key])}
    check("LAGC has a real web source after the fix",
          "website_diff" in cov["lagc"].working, ", ".join(cov["lagc"].working))
    check("every priority-1 company is covered",
          all(c.status == "ok" for c in cov.values() if c.priority == 1))
    check("thin coverage is surfaced, not hidden",
          any(c.status == "thin" for c in cov.values()),
          ", ".join(c.name for c in cov.values() if c.status == "thin"))

    # A company stripped of every source must be caught, not silently ignored.
    import copy
    blinded = copy.deepcopy(by_key["beeup"])
    blinded.domains, blinded.watch_pages, blinded.rss = [], [], []
    blinded.instagram, blinded.linkedin, blinded.ticketmaster = [], [], ""
    blind_cov = cov_report(conn, settings, [blinded])[0]
    check("a company with no sources reads as blind, not quiet",
          blind_cov.status in ("blind", "thin"), blind_cov.headline[:46])
    check("it appears in the gap list", len(gaps([blind_cov])) == 1)

    print("\n6. Ticketing refuses stale input")
    try:
        pipe.ticket(stale_id)
        check("stale ticket blocked", False, "it was allowed through")
    except ValueError as exc:
        check("stale ticket blocked", True, str(exc)[:56])
    except Exception as exc:
        check("stale ticket blocked", False, f"wrong error: {type(exc).__name__}")

    print("\n7. Review queue after enforcement")
    rows = db.feed(conn, status="pending", min_importance=settings["scoring"]["min_importance"],
                   since=window.oldest_allowed.isoformat())
    for r in rows:
        print(f"        [{r['importance']:>3}] {by_key[r['entity_key']].name:<26} {r['category']}")
    check("Bond film excluded", not any(r["entity_key"] == "goldfinger" for r in rows))
    check("stale girlfriends item excluded", not any(r["id"] == stale_id for r in rows))
    check("fresh signals present", len(rows) >= 2, f"{len(rows)} in queue")

    print("\n8. Second audit is clean")
    again = pipe.supervise(repair=True)
    check("no violations remain", again.ok)
    check("nothing left to repair", again.repairs == 0, f"{again.repairs} repairs")

    print("\n9. ClickUp payload (not sent)")
    body = ClickUp(settings)._body(rows[0], by_key[rows[0]["entity_key"]].name)
    print("        " + "\n        ".join(body.splitlines()[:4]))

    print("\n10. Dashboard render")
    import radar.server as server
    server.pipe = pipe
    html = server.index(status="pending")
    for label, ok in {
        "title present": "GL Radar" in html,
        "row rendered": "BEEUP" in html,
        "meter drawn": 'class="fill' in html,
        "status bar present": 'class="bar' in html,
        "stale item hidden": "announced a tour last month" not in html,
    }.items():
        check(label, ok)
    print(f"        {len(html):,} bytes rendered")

    print()
    if fails:
        print(f"FAILED ({len(fails)}):")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("All checks passed. Recency contract holds under adversarial input.")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
