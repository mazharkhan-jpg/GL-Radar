"""Live dates.

This is the collector that answers "Breakaway has a show around the 11th".

Every show Ticketmaster returns is written to the `events` table whether or not
it fires an alert. That record is what lets the supervisor independently ask
"is anything happening inside the forward window that we never told anyone
about?" — a question that cannot be answered if the only state is a cursor
saying a reminder was sent.

Alerts fire on two triggers:
  * announcement — a date we have never seen before, at any distance
  * countdown    — a date crossing a reminder mark inside the forward window
                   (default 2 and 1 days out), so show-day content gets queued
                   before the show rather than after it
"""
from __future__ import annotations

from datetime import datetime, timezone

from .. import db
from ..config import Entity, env
from ..db import Item
from ..freshness import Window, days_until, event_status
from .base import Collector, get

DISCOVERY = "https://app.ticketmaster.com/discovery/v2/events.json"

LABELS = {0: "today", 1: "tomorrow", 2: "in two days", 3: "in three days",
          4: "in four days", 5: "in five days"}


def label_for(days: int, running: bool = False) -> str:
    if running:
        return "happening now" if days <= 0 else LABELS.get(days, f"in {days} days")
    return LABELS.get(days, f"in {days} days")


class EventsCollector(Collector):
    name = "events"

    def __init__(self, settings: dict, conn, window: Window):
        super().__init__(settings)
        self.conn = conn
        self.window = window
        self.key = env("TICKETMASTER_API_KEY")

    def collect(self, entity: Entity):
        if not (self.key and entity.ticketmaster):
            return
        resp = get(DISCOVERY, params={
            "apikey": self.key,
            "keyword": entity.ticketmaster,
            "size": 50,
            "sort": "date,asc",
            "startDateTime": self.window.now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
        if not resp:
            return

        for ev in resp.json().get("_embedded", {}).get("events", []):
            date_str = ev.get("dates", {}).get("start", {}).get("localDate", "")
            event_id = ev.get("id", "")
            if not (date_str and event_id):
                continue

            venue, city = self._where(ev)
            record = {
                "event_id": event_id,
                "entity_key": entity.key,
                "name": ev.get("name", entity.name),
                "event_date": date_str,
                "end_date": (ev.get("dates", {}).get("end", {}).get("localDate")
                             or date_str),
                "venue": venue,
                "city": city,
                "url": ev.get("url", ""),
                "image_url": self._image(ev),
                "lineup": ", ".join(a.get("name", "") for a in
                                    ev.get("_embedded", {}).get("attractions", [])[:12]),
            }

            known = self.conn.execute(
                "SELECT event_id FROM events WHERE event_id = ?", (event_id,)).fetchone()
            db.upsert_event(self.conn, record)

            if not known:
                yield announcement_item(record, self.window)

            yield from due_reminders(self.conn, record, self.window)

    @staticmethod
    def _where(ev: dict) -> tuple[str, str]:
        venues = ev.get("_embedded", {}).get("venues", [])
        if not venues:
            return "", ""
        v = venues[0]
        city = f"{v.get('city', {}).get('name', '')}, {v.get('state', {}).get('stateCode', '')}"
        return v.get("name", ""), city.strip(", ")

    @staticmethod
    def _image(ev: dict) -> str:
        images = sorted(ev.get("images", []), key=lambda i: i.get("width", 0), reverse=True)
        return images[0]["url"] if images else ""


def _base(record: dict) -> dict:
    return {
        "entity_key": record["entity_key"],
        "source": "events",
        "url": record.get("url", ""),
        "author": record.get("venue") or "Ticketmaster",
        "image_url": record.get("image_url", ""),
        "published_at": datetime.now(timezone.utc).isoformat(),
    }


def announcement_item(record: dict, window: Window) -> Item:
    days = days_until(record["event_date"], window)
    return Item(
        source_id=f"{record['event_id']}:announce",
        title=f"{record['name']} — {record['event_date']}, {record.get('city', '')}",
        body=(f"New date on sale. {record.get('venue', '')}, {record.get('city', '')} "
              f"on {record['event_date']} ({days} days out). "
              f"Lineup: {record.get('lineup') or 'TBA'}."),
        raw={**record, "kind": "announcement", "days_out": days},
        **_base(record),
    )


def reminder_item(record: dict, days: int, window: Window, running: bool = False) -> Item:
    """`days` is the real distance to the show, not the mark that triggered it.

    Those differ during catch-up: if the two-day reminder never fired and we
    only notice on the eve of the show, the message must say "tomorrow". Saying
    "in two days" the day before is worse than saying nothing.
    """
    when = label_for(days, running)
    lead = ("Doors are open" if running else "Show is")
    dates = record["event_date"]
    if record.get("end_date") and record["end_date"] != dates:
        dates = f"{dates} to {record['end_date']}"
    return Item(
        source_id=f"{record['event_id']}:d{days}",
        title=f"{record['name']} is {when} — {dates}, {record.get('city', '')}",
        body=(f"{lead} {when} at {record.get('venue', '')}, "
              f"{record.get('city', '')}. Queue show-day content and be ready to "
              f"repost stories from the account. Lineup: {record.get('lineup') or 'TBA'}."),
        raw={**record, "kind": "reminder", "days_out": days, "running": running},
        **_base(record),
    )


def pending_marks(conn, record: dict, window: Window) -> list[int]:
    """Reminder marks this event has reached but never fired. Read-only.

    The supervisor uses this to detect a miss without causing one, and
    `due_reminders` uses it to act. Sharing the predicate is the point: a
    supervisor with its own copy of the rule eventually disagrees with the
    collector, and then you are debugging two clocks.
    """
    starts_in, running, relevant = event_status(
        record["event_date"], record.get("end_date"), window)
    if not relevant:
        return []
    # A festival already under way is at distance zero, not a negative number.
    days = max(starts_in or 0, 0)
    return [m for m in window.reminder_marks
            if days <= m and not db.event_alerted(conn, record["event_id"], m)]


def due_reminders(conn, record: dict, window: Window):
    """Emit at most one reminder per event per cycle, worded for today.

    Every mark the show has already passed is retired at the same time, so a
    machine that was asleep for two days sends one accurate message instead of
    a burst of increasingly wrong ones.
    """
    pending = pending_marks(conn, record, window)
    if not pending:
        return
    starts_in, running, _ = event_status(
        record["event_date"], record.get("end_date"), window)
    for mark in pending:
        db.mark_event_alerted(conn, record["event_id"], mark)
    yield reminder_item(record, max(starts_in or 0, 0), window, running)
