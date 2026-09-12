"""Time windows. Every date decision in the system routes through this module.

Two windows, and nothing is allowed to use its own arithmetic instead:

  LOOKBACK   how far back a news item may have been published and still count.
             Default 6 days. Anything older is invisible everywhere: not in the
             queue, not in a notification, not in a ticket.

  LOOKAHEAD  how far forward we alert on a scheduled event. Default 2 days,
             hard-capped at 5. A config file asking for 30 gets 5 and a warning,
             because the point of the forward window is "act now", and a reminder
             three weeks out is just noise that trains you to ignore the app.

The supervisor verifies these are actually honoured rather than trusting that
every call site remembered to ask.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

log = logging.getLogger("radar.freshness")

# Hard ceilings. Config cannot exceed these.
LOOKAHEAD_CEILING = 5
LOOKBACK_CEILING = 30


@dataclass(frozen=True)
class Window:
    lookback_days: int
    lookahead_days: int
    reminder_marks: tuple[int, ...]
    now: datetime

    @property
    def oldest_allowed(self) -> datetime:
        """Published before this instant means the item is dead to us."""
        return self.now - timedelta(days=self.lookback_days)

    @property
    def furthest_allowed(self) -> date:
        """An event after this date is too far out to alert on yet."""
        return (self.now + timedelta(days=self.lookahead_days)).date()

    def describe(self) -> str:
        return (f"last {self.lookback_days} days of news, "
                f"next {self.lookahead_days} days of events")


def load_window(settings: dict, now: datetime | None = None) -> Window:
    """Build the window from settings, clamping anything out of range."""
    cfg = settings.get("freshness", {}) or {}
    lookback = int(cfg.get("lookback_days", 6))
    lookahead = int(cfg.get("lookahead_days", 2))

    if not 1 <= lookback <= LOOKBACK_CEILING:
        log.warning("lookback_days %s out of range, clamping", lookback)
        lookback = max(1, min(lookback, LOOKBACK_CEILING))
    if not 1 <= lookahead <= LOOKAHEAD_CEILING:
        log.warning("lookahead_days %s exceeds the %s-day ceiling, clamping",
                    lookahead, LOOKAHEAD_CEILING)
        lookahead = max(1, min(lookahead, LOOKAHEAD_CEILING))

    # Reminders only fire inside the forward window; drop any configured beyond it.
    marks = sorted({int(m) for m in cfg.get("reminder_marks", [2, 1])
                    if 0 < int(m) <= lookahead}, reverse=True)
    if not marks:
        marks = [lookahead]

    return Window(lookback, lookahead, tuple(marks),
                  now or datetime.now(timezone.utc))


def parse_ts(value: str | None) -> datetime | None:
    """Parse the assorted timestamp shapes that feeds and APIs return."""
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    for attempt in (text, text[:19], text[:10]):
        try:
            parsed = datetime.fromisoformat(attempt)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    log.debug("unparseable timestamp: %r", value)
    return None


def is_fresh(published_at: str | None, window: Window) -> bool:
    """False for anything older than the lookback window.

    An unparseable or missing timestamp counts as fresh. Feeds sometimes omit
    dates, and dropping those would silently lose real news — the classifier
    still has to approve it, so the blast radius is small.
    """
    when = parse_ts(published_at)
    if when is None:
        return True
    return when >= window.oldest_allowed


def age_days(published_at: str | None, window: Window) -> float | None:
    when = parse_ts(published_at)
    if when is None:
        return None
    return (window.now - when).total_seconds() / 86400


def parse_event_date(value: str | None) -> date | None:
    parsed = parse_ts(value)
    return parsed.date() if parsed else None


def days_until(event_date: str | date | None, window: Window) -> int | None:
    if isinstance(event_date, str):
        event_date = parse_event_date(event_date)
    if not event_date:
        return None
    return (event_date - window.now.date()).days


def event_status(start: str | date | None, end: str | date | None,
                 window: Window) -> tuple[int | None, bool, bool]:
    """(days to start, running right now, worth alerting on).

    Single-day events pass end=None. Multi-day events count as live from their
    start through the end of their last day — Breakaway Philadelphia on the
    11th-12th is still on during the 12th, and a system that keyed off the
    start date alone would call it history on the morning of day two.
    """
    starts_in = days_until(start, window)
    ends_in = days_until(end or start, window)
    if starts_in is None or ends_in is None:
        return None, False, False
    running = starts_in <= 0 <= ends_in
    relevant = ends_in >= 0 and starts_in <= window.lookahead_days
    return starts_in, running, relevant


def is_upcoming(event_date: str | date | None, window: Window) -> bool:
    """True only for events between today and the forward edge, inclusive."""
    days = days_until(event_date, window)
    return days is not None and 0 <= days <= window.lookahead_days
