"""The supervisor.

Everything else in this system is written to respect the recency contract. The
supervisor assumes it didn't.

It re-queries the database directly and checks the invariants from the outside.
That distinction matters: a check that asks the pipeline "did you filter
correctly?" only ever confirms the pipeline's own opinion of itself. The bugs
that actually bite are the ones where a call site forgot the filter and nothing
noticed for three weeks.

Six checks, each of which can repair itself where repair is safe:

  1. config_valid          windows are inside their ceilings
  2. queue_is_fresh        nothing past the lookback is sitting in the queue
  3. events_covered        nothing inside the lookahead went unalerted
  4. no_stale_tickets      nothing stale was ever pushed to ClickUp
  5. collectors_alive      every enabled collector has run recently and cleanly
  6. credentials_valid     the Instagram token has not quietly expired

Exit code is non-zero on failure, so cron or CI catches a silent degradation.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import db
from .freshness import LOOKAHEAD_CEILING, LOOKBACK_CEILING, Window, days_until, parse_ts

log = logging.getLogger("radar.supervisor")

OK, WARN, FAIL = "ok", "warn", "fail"

# A collector is considered dead if it has not run in this multiple of its
# configured interval. Three cycles allows for one missed wake-up plus slack.
DEAD_AFTER_CYCLES = 3


@dataclass
class Check:
    name: str
    status: str
    detail: str
    repaired: int = 0

    @property
    def symbol(self) -> str:
        return {OK: "ok  ", WARN: "warn", FAIL: "FAIL"}[self.status]


@dataclass
class Report:
    window: Window
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(c.status == FAIL for c in self.checks)

    @property
    def repairs(self) -> int:
        return sum(c.repaired for c in self.checks)

    @property
    def worst(self) -> str:
        for level in (FAIL, WARN):
            if any(c.status == level for c in self.checks):
                return level
        return OK

    def to_text(self) -> str:
        lines = [f"Supervisor · {self.window.describe()}",
                 f"Checked {self.window.now.strftime('%Y-%m-%d %H:%M')} UTC", ""]
        for c in self.checks:
            lines.append(f"  {c.symbol} {c.name:<20} {c.detail}")
        if self.repairs:
            lines.append(f"\n  {self.repairs} issue(s) repaired automatically.")
        lines.append("\n" + ("All invariants hold." if self.ok
                             else "CONTRACT VIOLATED — see failures above."))
        return "\n".join(lines)


class Supervisor:
    def __init__(self, pipeline):
        self.pipe = pipeline
        self.conn = pipeline.conn
        self.settings = pipeline.settings

    def audit(self, repair: bool = True, window: Window | None = None) -> Report:
        window = window or self.pipe.window
        report = Report(window=window)
        for check in (self._config_valid, self._queue_is_fresh, self._events_covered,
                      self._no_stale_tickets, self._updated_today,
                      self._collectors_alive, self._every_company_covered,
                      self._credentials_valid):
            try:
                report.checks.append(check(window, repair))
            except Exception as exc:  # noqa: BLE001 - a broken check is itself a finding
                log.exception("check %s crashed", check.__name__)
                report.checks.append(
                    Check(check.__name__.strip("_"), FAIL, f"check crashed: {exc}"))
        return report

    # ---- 1. config ----

    def _config_valid(self, window: Window, repair: bool) -> Check:
        raw = self.settings.get("freshness", {}) or {}
        problems = []
        if int(raw.get("lookahead_days", 2)) > LOOKAHEAD_CEILING:
            problems.append(f"lookahead_days clamped to {LOOKAHEAD_CEILING}")
        if int(raw.get("lookback_days", 6)) > LOOKBACK_CEILING:
            problems.append(f"lookback_days clamped to {LOOKBACK_CEILING}")
        dropped = [m for m in raw.get("reminder_marks", [2, 1])
                   if int(m) > window.lookahead_days]
        if dropped:
            problems.append(f"reminder marks outside the window dropped: {dropped}")

        if problems:
            return Check("config_valid", WARN, "; ".join(problems))
        return Check("config_valid", OK,
                     f"lookback {window.lookback_days}d, lookahead {window.lookahead_days}d, "
                     f"marks {list(window.reminder_marks)}")

    # ---- 2. backward window ----

    def _queue_is_fresh(self, window: Window, repair: bool) -> Check:
        cutoff = window.oldest_allowed.isoformat()
        stale = db.stale_in_queue(self.conn, cutoff)
        if not stale:
            return Check("queue_is_fresh", OK,
                         f"no items older than {window.lookback_days}d in the queue")

        if repair and self.settings.get("freshness", {}).get("auto_expire", True):
            moved = db.expire_stale(self.conn, cutoff)
            oldest = min(s["published_at"] for s in stale)[:10]
            return Check("queue_is_fresh", WARN,
                         f"expired {moved} item(s), oldest from {oldest}", repaired=moved)

        return Check("queue_is_fresh", FAIL,
                     f"{len(stale)} stale item(s) visible in the queue")

    # ---- 3. forward window ----

    def _events_covered(self, window: Window, repair: bool) -> Check:
        from .collectors.events import due_reminders, pending_marks

        today = window.now.date().isoformat()
        edge = window.furthest_allowed.isoformat()
        upcoming = db.events_between(self.conn, today, edge)
        if not upcoming:
            return Check("events_covered", OK,
                         f"no shows inside the next {window.lookahead_days}d")

        # Same predicate the collector uses, so the two cannot drift apart.
        missed = [ev for ev in upcoming if pending_marks(self.conn, ev, window)]

        if not missed:
            names = ", ".join(f"{e['name'][:28]} ({days_until(e['event_date'], window)}d)"
                              for e in upcoming[:3])
            return Check("events_covered", OK,
                         f"{len(upcoming)} upcoming, all alerted: {names}")

        if repair:
            recovered = 0
            for ev in missed:
                for item in due_reminders(self.conn, ev, window):
                    db.insert_item(self.conn, item)
                    recovered += 1
                    log.warning("supervisor recovered a missed reminder: %s", item.title)
            return Check("events_covered", WARN,
                         f"recovered {recovered} missed reminder(s), "
                         f"will classify next pass", repaired=recovered)

        names = ", ".join(f"{e['name'][:30]} ({days_until(e['event_date'], window)}d)"
                          for e in missed)
        return Check("events_covered", FAIL,
                     f"{len(missed)} show(s) inside the window unalerted: {names}")

    # ---- 4. nothing stale was ever shipped ----

    def _no_stale_tickets(self, window: Window, repair: bool) -> Check:
        cutoff = window.oldest_allowed.isoformat()
        rows = self.conn.execute(
            """SELECT i.title, i.published_at FROM items i
               JOIN reviews r ON r.item_id = i.id
               WHERE r.status = 'ticketed' AND i.source != 'events'
                 AND i.published_at < ? AND r.decided_at > ?""",
            (cutoff, cutoff),
        ).fetchall()
        if rows:
            # Not auto-repairable: the ticket already exists in ClickUp.
            return Check("no_stale_tickets", FAIL,
                         f"{len(rows)} ticket(s) created from out-of-window items — "
                         f"check the filter before trusting the queue")
        return Check("no_stale_tickets", OK, "every ticket came from inside the window")

    # ---- 5. the daily promise ----

    def _updated_today(self, window: Window, repair: bool) -> Check:
        """The board must never be more than a day behind.

        Every other check verifies the data is correct. This one verifies it
        exists at all, which is the failure a sleeping laptop actually produces.
        """
        hours = self.pipe.hours_since_poll()
        if hours is None:
            return Check("updated_today", WARN, "no cycle has run yet")
        if hours < 24:
            return Check("updated_today", OK, f"last full check {hours:.1f}h ago")

        if repair:
            log.warning("no successful cycle in %.0fh, running one now", hours)
            try:
                self.pipe.run_cycle("all", supervise=False)
                return Check("updated_today", WARN,
                             f"was {hours:.0f}h stale, caught up just now", repaired=1)
            except Exception as exc:  # noqa: BLE001
                return Check("updated_today", FAIL,
                             f"{hours:.0f}h stale and catch-up failed: {exc}")
        return Check("updated_today", FAIL, f"no update in {hours:.0f}h")

    # ---- 6. is anything actually running ----

    def _collectors_alive(self, window: Window, repair: bool) -> Check:
        enabled = {k for k, v in self.settings.get("collectors", {}).items() if v}
        intervals = {
            "google_news": self.settings["poll"]["news_minutes"],
            "rss": self.settings["poll"]["news_minutes"],
            "website_diff": self.settings["poll"]["website_minutes"],
            "instagram": self.settings["poll"]["instagram_minutes"],
            "linkedin": self.settings["poll"]["linkedin_minutes"],
            "events": self.settings["poll"]["events_minutes"],
        }
        runs = db.last_runs(self.conn)
        silent, failing = [], []

        for name in enabled:
            run = runs.get(name)
            if not run:
                silent.append(f"{name} (never run)")
                continue
            if not run["ok"]:
                failing.append(f"{name}: {(run['error'] or '')[:40]}")
                continue
            last = parse_ts(run["started_at"])
            deadline = timedelta(minutes=intervals.get(name, 60) * DEAD_AFTER_CYCLES)
            if last and window.now - last > deadline:
                hours = (window.now - last).total_seconds() / 3600
                silent.append(f"{name} ({hours:.0f}h silent)")

        if failing:
            return Check("collectors_alive", FAIL, "; ".join(failing))
        if silent:
            return Check("collectors_alive", WARN, "; ".join(silent))
        return Check("collectors_alive", OK, f"{len(enabled)} collector(s) reporting on schedule")

    # ---- 7. can we see every company at all ----

    def _every_company_covered(self, window: Window, repair: bool) -> Check:
        """Catches the LAGC failure: a company with no usable source looks
        exactly like a company with no news, and only one of those is fine."""
        from .coverage import gaps, pipeline_ready, report

        ready, why = pipeline_ready(self.settings)
        if not ready:
            return Check("company_coverage", FAIL, why)

        cov = report(self.conn, self.settings, self.pipe.entities)
        problems = gaps(cov)
        if not problems:
            return Check("company_coverage", OK,
                         f"all {len(cov)} entities have a working source")

        blind = [c.name for c in problems if c.status == "blind"]
        blocked = [c.name for c in problems if c.status == "blocked"]
        thin = [c.name for c in problems if c.status == "thin"]
        parts = []
        if blind:
            parts.append(f"no source at all: {', '.join(blind)}")
        if blocked:
            parts.append(f"sources unusable: {', '.join(blocked)}")
        if thin:
            parts.append(f"Google News only: {', '.join(thin)}")
        detail = "; ".join(parts) + " — run: python -m radar.cli doctor"
        # Not auto-repairable: only a human can decide what source to add.
        return Check("company_coverage", FAIL if blind or blocked else WARN, detail)

    # ---- 8. the thing that silently breaks in 60 days ----

    def _credentials_valid(self, window: Window, repair: bool) -> Check:
        if not self.settings.get("collectors", {}).get("instagram"):
            return Check("credentials_valid", OK, "Instagram disabled, nothing to expire")
        if not os.getenv("IG_ACCESS_TOKEN"):
            return Check("credentials_valid", WARN, "Instagram on but IG_ACCESS_TOKEN is empty")

        issued = parse_ts(os.getenv("IG_TOKEN_ISSUED", ""))
        if not issued:
            return Check("credentials_valid", WARN,
                         "set IG_TOKEN_ISSUED=YYYY-MM-DD in .env to track the 60-day expiry")
        age = (window.now - issued).days
        left = 60 - age
        if left <= 0:
            return Check("credentials_valid", FAIL,
                         f"Instagram token expired {abs(left)}d ago — refresh it")
        if left <= 10:
            return Check("credentials_valid", WARN, f"Instagram token expires in {left}d")
        return Check("credentials_valid", OK, f"Instagram token good for {left}d")
