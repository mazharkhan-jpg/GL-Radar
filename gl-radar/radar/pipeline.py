"""collect -> dedupe -> classify -> route."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from . import db
from .collectors.events import EventsCollector
from .collectors.jina import JinaCollector
from .collectors.social import InstagramCollector, LinkedInCollector
from .collectors.web import GoogleNewsCollector, RSSCollector, WebsiteDiffCollector
from .config import Entity, load_entities, load_settings, registry_fingerprint
from .enrich.classifier import apply_priority, build as build_classifier
from .freshness import Window, is_fresh, load_window, parse_ts
from .sinks.clickup import ClickUp
from .sinks.notify import Notifier

log = logging.getLogger("radar.pipeline")


class Pipeline:
    def __init__(self):
        self.settings = load_settings()
        self.entities = load_entities()
        self.by_key = {e.key: e for e in self.entities}
        db.init(db.connect())
        self.classifier = build_classifier(self.settings)

        self.clickup = ClickUp(self.settings)
        self.notifier = Notifier(self.settings)
        self.window = load_window(self.settings)
        log.info("Recency contract: %s", self.window.describe())

    @property
    def conn(self):
        """Always this thread's connection. Accessing a connection opened on
        another thread raises, so every call site must go through here."""
        return db.connect()

    def refresh_window(self) -> Window:
        """Recompute `now` so a long-running process does not drift. The server
        runs for weeks; a window pinned at boot would slowly go stale."""
        self.window = load_window(self.settings)
        return self.window

    # ---------- collection ----------

    def _collectors(self, group: str = "all") -> list:
        on = self.settings.get("collectors", {})
        pool = {
            "google_news": lambda: GoogleNewsCollector(self.settings),
            "rss": lambda: RSSCollector(self.settings),
            "website_diff": lambda: WebsiteDiffCollector(self.settings, self.conn),
            "instagram": lambda: InstagramCollector(self.settings, self.conn),
            "linkedin": lambda: LinkedInCollector(self.settings, self.conn),
            "events": lambda: EventsCollector(self.settings, self.conn, self.window),
            "jina": lambda: JinaCollector(self.settings),
        }
        groups = {
            "news": ["google_news", "rss"],
            "jina": ["jina"],
            "website": ["website_diff", "jina"],
            "instagram": ["instagram"],
            "linkedin": ["linkedin"],
            "events": ["events"],
            "all": list(pool),
        }
        return [pool[name]() for name in groups.get(group, [])
                if on.get(name) and name in pool]

    def collect(self, group: str = "all", only: str = "") -> int:
        """Run collectors and store anything new. Returns count of new items.

        First enforcement point for the recency contract: stale items are not
        stored at all, so they cannot leak into a later query by accident.
        """
        self.refresh_window()
        total_new = stale_dropped = 0

        for collector in self._collectors(group):
            found = new = 0
            error = ""
            try:
                for entity in self.entities:
                    if only and entity.key != only:
                        continue
                    for item in collector.collect(entity) or []:
                        found += 1
                        if not item.title.strip():
                            continue
                        # Event items look forward, so the backward window
                        # does not apply to them.
                        if item.source != "events" and not is_fresh(
                                item.published_at, self.window):
                            stale_dropped += 1
                            continue
                        if db.insert_item(self.conn, item):
                            new += 1
                log.info("%s: %d found, %d new", collector.name, found, new)
            except Exception as exc:  # noqa: BLE001
                error = str(exc)
                log.exception("collector %s failed", collector.name)
            db.log_run(self.conn, collector.name, not error, found, new, error)
            total_new += new
        if stale_dropped:
            log.info("dropped %d item(s) older than %dd", stale_dropped,
                     self.window.lookback_days)
        return total_new

    # ---------- classification and routing ----------

    def _in_english(self, row: dict, entity: Entity) -> dict:
        """Return the row as the classifier should see it: in English.

        Translates once, stores the result, and reuses it forever after. Only
        items that already name a tracked company are sent out, which keeps the
        free daily allowance for the items that could actually matter.
        """
        from .enrich.rules import _matches_any, _words
        from .translate import detect, translate

        if row.get("lang") == "es" and row.get("title_en"):
            return {**row, "title": row["title_en"], "body": row.get("body_en") or row["body"],
                    "original_title": row["title"]}
        if row.get("lang"):
            return row

        lang = detect(f"{row.get('title', '')} {row.get('body', '')[:200]}")
        if lang != "es":
            db.save_translation(self.conn, row["id"], "en")
            return row

        names = list(entity.aliases) + [entity.name]
        if not _matches_any(_words(f"{row.get('title','')} {row.get('body','')}"), names):
            db.save_translation(self.conn, row["id"], "es")
            return row

        title_en = translate(row.get("title", ""))
        body_en = translate((row.get("body") or "")[:300])
        db.save_translation(self.conn, row["id"], "es", title_en, body_en)
        log.info("translated: %s -> %s", row.get("title", "")[:50], title_en[:50])
        return {**row, "title": title_en, "body": body_en or row["body"],
                "original_title": row["title"]}

    def rescore_stale(self, limit: int = 400) -> int:
        """Re-judge anything scored under an older version of the rules.

        Edit an exclusion list and the next run cleans up after itself. Without
        this, a fix only ever applies to items discovered after it, and the
        false positives already in the queue stay there looking authoritative.
        """
        fingerprint = registry_fingerprint()
        stale = db.stale_signals(self.conn, fingerprint, limit)
        if not stale:
            return 0

        log.info("registry changed; re-scoring %d cached verdict(s)", len(stale))
        dropped = 0
        for row in stale:
            entity = self.by_key.get(row["entity_key"])
            if not entity:
                # The company was removed from the registry entirely.
                db.save_signal(self.conn, row["id"],
                               {"relevant": False, "importance": 0, "category": "noise",
                                "reasoning": "company no longer tracked"},
                               self.classifier.model, fingerprint)
                dropped += 1
                continue
            verdict = self.classifier.classify(self._in_english(row, entity), entity)
            verdict["importance"] = apply_priority(
                verdict.get("importance", 0), entity.priority, self.settings)
            db.save_signal(self.conn, row["id"], verdict,
                           self.classifier.model, fingerprint)
            if not verdict.get("relevant"):
                # Leave no trace in the queue; it was never really ours.
                db.set_review(self.conn, row["id"], "dismissed",
                              note="re-scored as irrelevant after a registry update")
                dropped += 1
        if dropped:
            log.info("re-scoring removed %d item(s) from the queue", dropped)
        return dropped

    def classify_pending(self, limit: int = 60) -> list[dict]:
        """Score everything unjudged. Returns the items worth telling someone about."""
        scoring = self.settings["scoring"]
        surfaced = []
        fingerprint = registry_fingerprint()
        # Second enforcement point: never pay the classifier for something
        # that has aged out since it was collected.
        since = self.window.oldest_allowed.isoformat()

        for row in db.unclassified(self.conn, limit, since=since):
            entity = self.by_key.get(row["entity_key"])
            if not entity:
                continue
            # Judge the English text. Spanish announcements were being scored as
            # noise because none of the keywords are Spanish.
            english = self._in_english(row, entity)
            verdict = self.classifier.classify(english, entity)
            verdict["importance"] = apply_priority(
                verdict.get("importance", 0), entity.priority, self.settings)
            db.save_signal(self.conn, row["id"], verdict, self.classifier.model, fingerprint)

            if not verdict.get("relevant") or verdict["importance"] < scoring["min_importance"]:
                continue
            db.set_review(self.conn, row["id"], "pending")
            merged = {**row, **verdict, "entity_name": entity.name}
            surfaced.append(merged)

            if verdict["importance"] >= scoring["auto_ticket_at"] and self.clickup.enabled:
                try:
                    self.ticket(row["id"])
                    merged["auto_ticketed"] = True
                except Exception as exc:  # noqa: BLE001
                    log.error("auto-ticket failed for %s: %s", row["id"], exc)
        return surfaced

    def ticket(self, item_id: int) -> tuple[str, str]:
        """Third enforcement point. Refuses outright rather than warning, so a
        stale row that somehow reached the UI still cannot become a ticket."""
        row = self._row(item_id)
        self.refresh_window()
        if row["source"] != "events" and not is_fresh(row["published_at"], self.window):
            age = (row.get("published_at") or "")[:10]
            raise ValueError(
                f"Refusing: published {age}, outside the "
                f"{self.window.lookback_days}-day window")
        entity = self.by_key.get(row["entity_key"])
        task_id, task_url = self.clickup.create_task(row, entity.name if entity else row["entity_key"])
        db.set_review(self.conn, item_id, "ticketed", task_id, task_url)
        return task_id, task_url

    def dismiss(self, item_id: int, note: str = "") -> None:
        db.set_review(self.conn, item_id, "dismissed", note=note)

    def _row(self, item_id: int) -> dict:
        row = self.conn.execute(
            """SELECT i.*, s.category, s.importance, s.summary, s.repost_angle, s.caption
               FROM items i LEFT JOIN signals s ON s.item_id = i.id WHERE i.id = ?""",
            (item_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"No item {item_id}")
        return dict(row)

    # ---------- the thing cron calls ----------

    def run_cycle(self, group: str = "all", supervise: bool = True) -> dict:
        new_count = self.collect(group)
        # Before judging anything new, re-judge anything the rules have
        # outgrown. Cleanup is part of every run, not a manual chore.
        rescored = self.rescore_stale()
        surfaced = self.classify_pending()
        notify_at = self.settings["scoring"]["notify_at"]
        # Fourth enforcement point: a notification is a claim that something
        # is happening now, so it gets the same filter as everything else.
        worth_pinging = [s for s in surfaced
                         if s["importance"] >= notify_at
                         and (s["source"] == "events"
                              or is_fresh(s.get("published_at"), self.window))]

        if worth_pinging:
            top = worth_pinging[0]
            extra = f" (+{len(worth_pinging) - 1} more)" if len(worth_pinging) > 1 else ""
            self.notifier.send(
                f"{top['entity_name']} — {top.get('category', 'update')}{extra}",
                top.get("summary", top["title"]),
                top.get("url", ""),
            )
        db.set_cursor(self.conn, "last_poll_at",
                      datetime.now(timezone.utc).isoformat())
        result = {"new_items": new_count, "surfaced": len(surfaced),
                  "notified": len(worth_pinging), "rescored_out": rescored}

        if supervise:
            report = self.supervise()
            result["supervisor"] = report.worst
            result["repairs"] = report.repairs
            if not report.ok:
                self.notifier.send(
                    "GL Radar needs attention",
                    "; ".join(c.detail for c in report.checks if c.status == "fail"),
                    force=True)
        return result

    # ---------- refresh scheduling state ----------

    def auto_refresh_enabled(self) -> bool:
        """Stored server-side rather than in the browser, so the setting is the
        same whichever device opens the dashboard."""
        raw = db.get_cursor(self.conn, "auto_refresh")
        if raw == "":
            return bool(self.settings["poll"].get("auto_refresh_default", True))
        return raw == "1"

    def set_auto_refresh(self, on: bool) -> None:
        db.set_cursor(self.conn, "auto_refresh", "1" if on else "0")

    def poll_is_due(self) -> bool:
        """True once the configured interval has elapsed since the last cycle.

        The server asks this rather than each browser tab keeping its own timer,
        so three open tabs still produce one poll.
        """
        minutes = int(self.settings["poll"].get("auto_refresh_minutes", 60))
        last = parse_ts(db.get_cursor(self.conn, "last_poll_at"))
        if not last:
            return True
        return (datetime.now(timezone.utc) - last) >= timedelta(minutes=minutes)

    def hours_since_poll(self) -> float | None:
        last = parse_ts(db.get_cursor(self.conn, "last_poll_at"))
        if not last:
            return None
        return (datetime.now(timezone.utc) - last).total_seconds() / 3600

    def supervise(self, repair: bool = True):
        """Audit the freshness contract from outside the pipeline."""
        from .supervisor import Supervisor
        self.refresh_window()
        report = Supervisor(self).audit(repair=repair, window=self.window)
        if not report.ok:
            log.error("supervisor found violations:\n%s", report.to_text())
        elif report.repairs:
            log.warning("supervisor repaired %d issue(s)", report.repairs)
        return report

    def close(self) -> None:
        db.close_thread_connection()
