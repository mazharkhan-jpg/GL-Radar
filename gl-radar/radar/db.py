"""SQLite storage. One file, no server, survives restarts."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

from .config import DATA_DIR

# Overridable so tests write to their own file. The fixture data in
# scripts/smoke_test.py once landed in the live database and was mistaken for
# real findings; a separate path makes that impossible rather than unlikely.
DB_PATH = Path(os.getenv("RADAR_DB") or (DATA_DIR / "radar.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint   TEXT UNIQUE NOT NULL,
    entity_key    TEXT NOT NULL,
    source        TEXT NOT NULL,
    source_id     TEXT,
    url           TEXT,
    title         TEXT,
    body          TEXT,
    author        TEXT,
    image_url     TEXT,
    published_at  TEXT,
    first_seen    TEXT NOT NULL,
    raw           TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_entity ON items(entity_key, published_at DESC);

CREATE TABLE IF NOT EXISTS signals (
    item_id        INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    relevant       INTEGER NOT NULL,
    category       TEXT,
    importance     INTEGER NOT NULL DEFAULT 0,
    repostable     INTEGER NOT NULL DEFAULT 0,
    summary        TEXT,
    repost_angle   TEXT,
    caption        TEXT,
    reasoning      TEXT,
    model          TEXT,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reviews (
    item_id     INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    status      TEXT NOT NULL,            -- pending | ticketed | dismissed
    clickup_id  TEXT,
    clickup_url TEXT,
    decided_at  TEXT,
    note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_reviews_status ON reviews(status);

CREATE TABLE IF NOT EXISTS cursors (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT
);

-- Scheduled events live here rather than only in cursor state, so the
-- supervisor can independently ask "is anything happening in the next two
-- days that we never alerted on?"
CREATE TABLE IF NOT EXISTS events (
    event_id     TEXT PRIMARY KEY,
    entity_key   TEXT NOT NULL,
    name         TEXT,
    event_date   TEXT NOT NULL,
    end_date     TEXT,                  -- multi-day festivals stay live until this
    venue        TEXT,
    city         TEXT,
    url          TEXT,
    image_url    TEXT,
    lineup       TEXT,
    announced_at TEXT,
    alerted      TEXT DEFAULT '',       -- comma-separated day marks already fired
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_date ON events(event_date);

CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    collector  TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ok         INTEGER,
    found      INTEGER DEFAULT 0,
    new        INTEGER DEFAULT 0,
    error      TEXT
);
"""


@dataclass
class Item:
    """One raw thing found on the internet, before any judgement is applied."""

    entity_key: str
    source: str
    title: str
    url: str = ""
    body: str = ""
    source_id: str = ""
    author: str = ""
    image_url: str = ""
    published_at: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        basis = f"{self.entity_key}|{self.source}|{self.source_id or self.url or self.title}"
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# SQLite connections cannot be shared across threads, and the web server runs
# request handlers in a pool while collection runs on its own thread. One
# connection per thread, created lazily, is the simplest correct answer at this
# scale. WAL lets the readers carry on while a poll is writing.
_local = threading.local()


def connect() -> sqlite3.Connection:
    """Return this thread's connection, opening it on first use."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        return conn
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # Wait rather than raising if a write is in flight on another thread.
    conn.execute("PRAGMA busy_timeout=10000")
    _local.conn = conn
    return conn


def close_thread_connection() -> None:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None


def init(conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    conn = conn or connect()
    conn.executescript(SCHEMA)
    conn.commit()
    if own:
        conn.close()


def _normalise(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").lower()
    return " ".join(ch for ch in text if ch.isalnum() or ch.isspace()).split().__str__()


def is_near_duplicate(conn: sqlite3.Connection, item: Item, threshold: float = 0.86) -> bool:
    """Catch the same story arriving from five different outlets.

    Compares against items for the same entity from the last three days. Cheap
    enough at this volume that a vector index would be over-engineering.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    rows = conn.execute(
        "SELECT title FROM items WHERE entity_key = ? AND first_seen > ?",
        (item.entity_key, cutoff),
    ).fetchall()
    candidate = _normalise(item.title)
    for row in rows:
        if SequenceMatcher(None, candidate, _normalise(row["title"])).ratio() >= threshold:
            return True
    return False


def insert_item(conn: sqlite3.Connection, item: Item) -> int | None:
    """Insert and return the new row id, or None if we have already seen it."""
    if is_near_duplicate(conn, item):
        return None
    try:
        cur = conn.execute(
            """INSERT INTO items
               (fingerprint, entity_key, source, source_id, url, title, body,
                author, image_url, published_at, first_seen, raw)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                item.fingerprint, item.entity_key, item.source, item.source_id,
                item.url, item.title, item.body, item.author, item.image_url,
                item.published_at or _now(), _now(), json.dumps(item.raw, default=str),
            ),
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


def save_signal(conn: sqlite3.Connection, item_id: int, verdict: dict, model: str) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO signals
           (item_id, relevant, category, importance, repostable, summary,
            repost_angle, caption, reasoning, model, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            item_id,
            int(bool(verdict.get("relevant"))),
            verdict.get("category", "unknown"),
            int(verdict.get("importance", 0)),
            int(bool(verdict.get("repostable"))),
            verdict.get("summary", ""),
            verdict.get("repost_angle", ""),
            verdict.get("caption", ""),
            verdict.get("reasoning", ""),
            model,
            _now(),
        ),
    )
    conn.commit()


def set_review(conn: sqlite3.Connection, item_id: int, status: str,
               clickup_id: str = "", clickup_url: str = "", note: str = "") -> None:
    conn.execute(
        """INSERT INTO reviews (item_id, status, clickup_id, clickup_url, decided_at, note)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(item_id) DO UPDATE SET
             status=excluded.status, clickup_id=excluded.clickup_id,
             clickup_url=excluded.clickup_url, decided_at=excluded.decided_at,
             note=excluded.note""",
        (item_id, status, clickup_id, clickup_url, _now(), note),
    )
    conn.commit()


def get_cursor(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM cursors WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else ""


def set_cursor(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """INSERT INTO cursors (key, value, updated_at) VALUES (?,?,?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
        (key, value, _now()),
    )
    conn.commit()


def log_run(conn: sqlite3.Connection, collector: str, ok: bool,
            found: int = 0, new: int = 0, error: str = "") -> None:
    conn.execute(
        "INSERT INTO runs (collector, started_at, ok, found, new, error) VALUES (?,?,?,?,?,?)",
        (collector, _now(), int(ok), found, new, error[:500]),
    )
    conn.commit()


def feed(conn: sqlite3.Connection, status: str = "pending", limit: int = 100,
         entity: str = "", min_importance: int = 0, since: str = "",
         view: str = "review", archive_since: str = "") -> list[dict]:
    """The dashboard's main query: items joined to their verdict and decision.

    Two views, and the difference is only ever a date range:

      review    inside the window — the last week of news plus the coming
                week of shows. This is the working queue.
      archive   everything that has fallen out of the window, back to the
                archive floor. Nothing is deleted; it just stops being urgent.

    `since` is not optional in practice. Callers get it from the Window so the
    recency contract is applied in SQL rather than hoped for downstream.
    """
    sql = """
        SELECT i.*, s.category, s.importance, s.repostable, s.summary,
               s.repost_angle, s.caption, s.reasoning,
               COALESCE(r.status, 'pending') AS status, r.clickup_url
        FROM items i
        JOIN signals s ON s.item_id = i.id
        LEFT JOIN reviews r ON r.item_id = i.id
        WHERE s.relevant = 1 AND s.importance >= ?
    """
    params: list[Any] = [min_importance]

    if view == "archive":
        # Out of the window but not yet forgotten. Status is ignored here:
        # once something is history, whether it was ticketed or passed over
        # matters less than being able to find it again.
        if since:
            sql += " AND i.published_at < ?"
            params.append(since)
        if archive_since:
            sql += " AND i.published_at >= ?"
            params.append(archive_since)
        sql += " ORDER BY i.published_at DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in conn.execute(sql, params).fetchall()]

    if since:
        sql += " AND i.published_at >= ?"
        params.append(since)
    if status != "all":
        sql += " AND COALESCE(r.status, 'pending') = ?"
        params.append(status)
    if entity:
        sql += " AND i.entity_key = ?"
        params.append(entity)
    sql += " ORDER BY s.importance DESC, i.published_at DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def expire_stale(conn: sqlite3.Connection, cutoff: str) -> int:
    """Move pending items that aged out of the window to 'expired'.

    Leaving them in the queue is worse than dropping them: they look actionable,
    someone tickets a week-old story, and it goes up as if it were news.
    """
    rows = conn.execute(
        """SELECT i.id FROM items i
           JOIN signals s ON s.item_id = i.id
           LEFT JOIN reviews r ON r.item_id = i.id
           WHERE COALESCE(r.status, 'pending') = 'pending'
             AND i.published_at < ?""",
        (cutoff,),
    ).fetchall()
    for row in rows:
        set_review(conn, row["id"], "expired", note="aged out of the recency window")
    return len(rows)


def stale_in_queue(conn: sqlite3.Connection, cutoff: str) -> list[dict]:
    """Anything the supervisor would consider a contract violation."""
    return [dict(r) for r in conn.execute(
        """SELECT i.id, i.entity_key, i.title, i.published_at, i.source
           FROM items i
           JOIN signals s ON s.item_id = i.id
           LEFT JOIN reviews r ON r.item_id = i.id
           WHERE COALESCE(r.status, 'pending') = 'pending'
             AND i.published_at < ?""",
        (cutoff,),
    ).fetchall()]


def upsert_event(conn: sqlite3.Connection, event: dict) -> None:
    conn.execute(
        """INSERT INTO events
           (event_id, entity_key, name, event_date, end_date, venue, city, url,
            image_url, lineup, announced_at, alerted, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,'',?)
           ON CONFLICT(event_id) DO UPDATE SET
             name=excluded.name, event_date=excluded.event_date,
             end_date=excluded.end_date,
             venue=excluded.venue, city=excluded.city, url=excluded.url,
             image_url=excluded.image_url, lineup=excluded.lineup,
             updated_at=excluded.updated_at""",
        (event["event_id"], event["entity_key"], event.get("name", ""),
         event["event_date"], event.get("end_date") or event["event_date"],
         event.get("venue", ""), event.get("city", ""),
         event.get("url", ""), event.get("image_url", ""), event.get("lineup", ""),
         _now(), _now()),
    )
    conn.commit()


def events_between(conn: sqlite3.Connection, start: str, end: str) -> list[dict]:
    """Events overlapping the range, not just those starting inside it.

    A two-day festival that began yesterday is still on today, and is exactly
    the thing you want a show-day reminder for.
    """
    return [dict(r) for r in conn.execute(
        """SELECT * FROM events
           WHERE COALESCE(end_date, event_date) >= ? AND event_date <= ?
           ORDER BY event_date""",
        (start, end),
    ).fetchall()]


def event_alerted(conn: sqlite3.Connection, event_id: str, mark: int) -> bool:
    row = conn.execute("SELECT alerted FROM events WHERE event_id = ?", (event_id,)).fetchone()
    if not row:
        return False
    return str(mark) in (row["alerted"] or "").split(",")


def mark_event_alerted(conn: sqlite3.Connection, event_id: str, mark: int) -> None:
    row = conn.execute("SELECT alerted FROM events WHERE event_id = ?", (event_id,)).fetchone()
    marks = set(filter(None, (row["alerted"] if row else "").split(",")))
    marks.add(str(mark))
    conn.execute("UPDATE events SET alerted = ?, updated_at = ? WHERE event_id = ?",
                 (",".join(sorted(marks)), _now(), event_id))
    conn.commit()


def last_runs(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute(
        """SELECT collector, MAX(started_at) AS started_at, ok, found, new, error
           FROM runs GROUP BY collector"""
    ).fetchall()
    return {r["collector"]: dict(r) for r in rows}


def unclassified(conn: sqlite3.Connection, limit: int = 60, since: str = "") -> list[dict]:
    """Items awaiting a verdict. Stale ones are skipped so we never pay the
    classifier for something that could not be displayed anyway."""
    sql = """SELECT i.* FROM items i
             LEFT JOIN signals s ON s.item_id = i.id
             WHERE s.item_id IS NULL"""
    params: list[Any] = []
    if since:
        sql += " AND (i.published_at >= ? OR i.source = 'events')"
        params.append(since)
    sql += " ORDER BY i.first_seen DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    row = conn.execute(
        """SELECT
             (SELECT COUNT(*) FROM items) AS items,
             (SELECT COUNT(*) FROM signals WHERE relevant = 1) AS relevant,
             (SELECT COUNT(*) FROM reviews WHERE status = 'ticketed') AS ticketed,
             (SELECT COUNT(*) FROM reviews WHERE status = 'dismissed') AS dismissed,
             (SELECT COUNT(*) FROM reviews WHERE status = 'expired') AS expired"""
    ).fetchone()
    return dict(row)
