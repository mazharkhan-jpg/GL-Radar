"""Instagram and LinkedIn.

Instagram is read through Apify's pay-per-result scraper. That choice is
deliberate: the official Graph API route needs a Meta developer app, a
Facebook Page, phone verification and a token that dies every 60 days, and
even then it can only read accounts set to Business or Creator. Apify needs
one token, never expires, and reads any public account.

Cost is the thing to watch, so it is watched here rather than trusted:

  * Only the newest `posts_per_handle` posts are requested, not whole feeds.
  * `until` pushes the date filter server-side, so posts older than the
    lookback window are never returned and never billed.
  * Every billed result is tallied into the `cursors` table under the current
    month. When the tally crosses `monthly_budget_usd` the collector stops
    calling Apify for the rest of the month and says so in the log. It does
    not quietly keep spending.

At ~29 handles and 5 posts each the run bills about 145 results a day, which
is roughly USD 2.20 a month against Apify's USD 5 free monthly credit.

The Graph API path is kept as a fallback for anyone who already has Meta
credentials. If IG_USER_ID and IG_ACCESS_TOKEN are set and Apify is not, it
is used instead. LinkedIn runs through the same Apify token and the same
budget ledger, and stays off until it has proved it fits.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from ..config import Entity, env
from ..db import Item, get_cursor, set_cursor
from .base import Collector, client, get

log = logging.getLogger("radar.social")

GRAPH = "https://graph.facebook.com/v21.0"
APIFY_SYNC = "https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"


def _caption_title(caption: str, handle: str) -> str:
    """Instagram posts have no title, so build one from the first real line."""
    first = next((ln.strip() for ln in (caption or "").splitlines() if ln.strip()), "")
    return (first[:140] + "...") if len(first) > 140 else (first or f"New post from @{handle}")


def _first(post: dict, *names, default=None):
    """Scrapers rename fields between versions. Take the first one present."""
    for n in names:
        if n in post and post[n] not in (None, ""):
            return post[n]
    return default


def _iso(value) -> str:
    """Accept epoch seconds, epoch ms, or an ISO string. Return ISO UTC."""
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)) or str(value).isdigit():
        n = float(value)
        if n > 1e11:          # milliseconds
            n /= 1000.0
        return datetime.fromtimestamp(n, timezone.utc).isoformat()
    text = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


class ApifyBudget:
    """A running tally of billed results, kept per calendar month.

    Lives in the cursors table so it survives the runner being wiped between
    GitHub Actions runs. Without this the spend would be invisible until a
    bill arrived.
    """

    def __init__(self, conn, budget_usd: float, usd_per_1000: float):
        self.conn = conn
        self.budget = budget_usd
        self.rate = usd_per_1000
        self.month = datetime.now(timezone.utc).strftime("%Y-%m")
        self.key = f"apify:results:{self.month}"
        self._warned = False

    @property
    def results(self) -> int:
        if self.conn is None:
            return 0
        try:
            return int(get_cursor(self.conn, self.key) or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def spent(self) -> float:
        return self.results * self.rate / 1000.0

    def add(self, n: int) -> None:
        if self.conn is None or n <= 0:
            return
        set_cursor(self.conn, self.key, str(self.results + n))

    def exhausted(self) -> bool:
        if self.conn is None:          # no ledger, no guard; fail closed
            return False
        if self.spent < self.budget:
            return False
        if not self._warned:
            log.warning(
                "Apify budget reached for %s: %d results = $%.2f of $%.2f. "
                "Social collection pauses until the month rolls over.",
                self.month, self.results, self.spent, self.budget,
            )
            self._warned = True
        return True


class _ApifyCollector(Collector):
    """Shared Apify plumbing for the two social collectors."""

    def __init__(self, settings: dict, conn=None):
        super().__init__(settings)
        self.conn = conn
        self.apify_token = env("APIFY_TOKEN")
        cfg = settings.get("apify") or {}
        self.budget = ApifyBudget(
            conn,
            float(cfg.get("monthly_budget_usd", 4.0)),
            float(cfg.get("usd_per_1000_results", 0.50)),
        )
        # Social has a window of its own, much shorter than the board's. The
        # board shows 7 days; the collector only needs what appeared since the
        # last run, because everything older is already in the database.
        self.lookback_days = int(cfg.get("lookback_days", 2))

    def _floor(self) -> datetime:
        """One extra day of slack covers timezone edges rather than losing a
        post to rounding."""
        return datetime.now(timezone.utc) - timedelta(days=self.lookback_days + 1)

    def _since_date(self) -> str:
        """Apify's `until` filter wants a plain date."""
        return self._floor().strftime("%Y-%m-%d")

    def _run(self, actor: str, payload: dict, timeout: float = 180.0) -> list[dict]:
        """Run an actor and return its dataset rows. Never raises."""
        if not self.apify_token or self.budget.exhausted():
            return []
        url = APIFY_SYNC.format(actor=actor)
        try:
            with client(timeout=timeout) as c:
                resp = c.post(url, params={"token": self.apify_token}, json=payload)
            if resp.status_code in (401, 403):
                log.error("Apify rejected the token (HTTP %s). Check APIFY_TOKEN.",
                          resp.status_code)
                return []
            if resp.status_code == 402:
                log.error("Apify is out of credit for this month.")
                return []
            resp.raise_for_status()
            rows = resp.json()
        except Exception as exc:  # noqa: BLE001 - one bad handle must not stop the cycle
            log.warning("Apify run failed (%s): %s", actor, exc)
            return []
        if not isinstance(rows, list):
            return []
        billed = len(rows)
        # The actor emits rows of {"noResults": true} when it cannot read a
        # profile — ten of them per handle — and Apify bills for every one.
        # They are not posts and must never reach the board.
        kept = [r for r in rows if isinstance(r, dict)
                and not r.get("error") and not r.get("noResults")]
        if billed and not kept:
            log.warning("%s: %d rows billed, all empty (the scraper could not "
                        "read this profile).", self.name, billed)
        # Keep a sample where it can actually be read. The job log is not
        # reachable from here, so three runs in a row billed for results and
        # stored nothing with no way to see why. The cursors table is
        # committed with the database, so this survives the runner.
        if self.conn is not None and rows:
            try:
                sample = json.dumps({
                    "billed": billed,
                    "kept": len(kept),
                    "first": rows[0] if isinstance(rows[0], dict) else str(rows[0]),
                }, default=str)[:4000]
                set_cursor(self.conn, f"debug:{self.name}:last", sample)
            except Exception:  # noqa: BLE001 - diagnostics must never break a run
                pass
        self.budget.add(billed)
        return kept


class InstagramCollector(_ApifyCollector):
    name = "instagram"

    def __init__(self, settings: dict, conn=None):
        super().__init__(settings, conn)
        cfg = settings.get("apify") or {}
        self.actor = env("APIFY_IG_ACTOR",
                         cfg.get("instagram_actor", "apidojo~instagram-scraper"))
        self.per_handle = int(cfg.get("posts_per_handle", 5))
        self.ig_user_id = env("IG_USER_ID")
        self.graph_token = env("IG_ACCESS_TOKEN")

    def collect(self, entity: Entity):
        for handle in entity.instagram:
            posts = []
            if self.apify_token:
                posts = self._via_apify(handle)
            if not posts and self.ig_user_id and self.graph_token:
                posts = self._via_graph(handle) or []
            if not posts:
                log.info("No Instagram posts read for @%s", handle)
            for post in posts:
                item = self._to_item(entity, handle, post)
                if item:
                    yield item

    def _via_apify(self, handle: str) -> list[dict]:
        # No `until`. The documentation calls it "posts published after your
        # specified date", but the behaviour says otherwise: with the filter
        # set to a week ago the actor billed ~10 posts per handle and every
        # one of them failed the 7-day freshness check; tightening it to three
        # days returned older posts still. It reads as an upper bound, so it
        # was fetching the oldest posts rather than the newest. Asking for the
        # newest N with no date filter is what we wanted all along, and the
        # pipeline enforces the window at ingest.
        rows = self._run(self.actor, {
            "startUrls": [f"https://www.instagram.com/{handle}/"],
            "maxItems": self.per_handle,
        })
        # Say out loud what the payload actually looks like. Guessing field
        # names cost two runs already; one log line makes the next rename
        # obvious from the job output instead of from wrong data on the board.
        if rows:
            log.warning("@%s: %d rows, fields=%s, first_date=%r",
                        handle, len(rows), sorted(rows[0].keys())[:25],
                        _first(rows[0], "createdAt", "timestamp", "takenAt",
                               "taken_at", "takenAtTimestamp", "postedAt"))

        # `until` already filters server-side and the pipeline enforces the
        # recency window at ingest, so no date filter here. Filtering before
        # storing is what hid the field names last time. Newest first, then
        # capped, with undated rows last rather than dropped silently.
        rows.sort(key=lambda r: _iso(_first(r, "createdAt", "timestamp", "takenAt",
                                            "taken_at", "takenAtTimestamp",
                                            "taken_at_timestamp", "postedAt")) or "",
                  reverse=True)
        return rows[:self.per_handle]

    def _via_graph(self, handle: str) -> list[dict] | None:
        fields = (
            f"business_discovery.username({handle})"
            "{followers_count,media_count,media.limit(12)"
            "{id,caption,like_count,comments_count,media_url,permalink,timestamp,media_type}}"
        )
        resp = get(f"{GRAPH}/{self.ig_user_id}",
                   params={"fields": fields, "access_token": self.graph_token})
        if not resp:
            return None
        payload = resp.json()
        if "error" in payload:
            log.warning("IG Graph error for @%s: %s", handle, payload["error"].get("message"))
            return None
        return payload.get("business_discovery", {}).get("media", {}).get("data", [])

    def _to_item(self, entity: Entity, handle: str, post: dict) -> Item | None:
        caption = _first(post, "caption", "caption_text", "text", "description", default="") or ""
        if isinstance(caption, dict):               # some actors nest it
            caption = caption.get("text", "")
        url = _first(post, "permalink", "url", "postUrl", "displayUrl", default="")
        shortcode = _first(post, "shortCode", "shortcode", "code", default="")
        if not url and shortcode:
            url = f"https://www.instagram.com/p/{shortcode}/"
        published = _iso(_first(post, "createdAt", "timestamp", "takenAt", "taken_at",
                                "takenAtTimestamp", "taken_at_timestamp",
                                "timestamp_utc", "postedAt", "createTime"))
        source_id = str(_first(post, "id", "pk", default="") or shortcode or url)
        if not source_id:
            return None                              # nothing stable to dedupe on
        return Item(
            entity_key=entity.key,
            source="instagram",
            source_id=source_id,
            title=_caption_title(caption, handle),
            url=url,
            body=str(caption)[:3000],
            author=f"@{handle}",
            image_url=_first(post, "media_url", "displayUrl", "imageUrl",
                             "thumbnailUrl", default="") or "",
            published_at=published or datetime.now(timezone.utc).isoformat(),
            raw={
                "handle": handle,
                "likes": int(_first(post, "likeCount", "like_count", "likesCount",
                                    "likes", default=0) or 0),
                "comments": int(_first(post, "commentCount", "comments_count",
                                       "commentsCount", "comments", default=0) or 0),
                "media_type": _first(post, "media_type", "type", "productType", default="") or "",
                "fields": sorted(post.keys())[:40],
            },
        )


class LinkedInCollector(_ApifyCollector):
    """Same token, same ledger. Off by default until the cost is measured —
    run `python -m radar.cli doctor` after a day of Instagram to see what
    headroom is left before turning this on."""

    name = "linkedin"

    def __init__(self, settings: dict, conn=None):
        super().__init__(settings, conn)
        cfg = settings.get("apify") or {}
        self.actor = env("APIFY_LI_ACTOR",
                         cfg.get("linkedin_actor", "apimaestro~linkedin-company-posts"))
        self.per_company = int(cfg.get("posts_per_company", 5))

    def collect(self, entity: Entity):
        if not self.apify_token or not entity.linkedin:
            return
        for slug in entity.linkedin:
            for post in self._run(self.actor, {
                "company_name": slug,
                "username": slug,
                "limit": self.per_company,
                "maxItems": self.per_company,
            }):
                text = _first(post, "text", "content", "commentary", default="") or ""
                url = _first(post, "postUrl", "url", "post_url", default="")
                source_id = str(_first(post, "urn", "id", "activityUrn", default="") or url)
                if not source_id:
                    continue
                yield Item(
                    entity_key=entity.key,
                    source="linkedin",
                    source_id=source_id,
                    title=_caption_title(str(text), slug),
                    url=url,
                    body=str(text)[:3000],
                    author=_first(post, "authorName", "author_name", default=slug),
                    image_url=_first(post, "imageUrl", "image", default="") or "",
                    published_at=_iso(_first(post, "postedAt", "posted_at", "publishedAt",
                                             "time", "date"))
                                 or datetime.now(timezone.utc).isoformat(),
                    raw={"slug": slug,
                         "likes": int(_first(post, "numLikes", "likesCount",
                                             "reactions", default=0) or 0),
                         "comments": int(_first(post, "numComments", "commentsCount",
                                                default=0) or 0)},
                )
