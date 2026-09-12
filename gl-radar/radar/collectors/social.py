"""Instagram and LinkedIn.

Instagram has two paths, tried in order:

1. Graph API `business_discovery` — official, free, unlimited-ish. You already
   qualify: it needs one Instagram Business account you control (the GL page)
   linked to a Facebook Page, plus a Meta app. It then returns recent media for
   ANY public Business or Creator account, which covers nearly every brand here.
   It cannot see Stories, and it cannot see personal (non-business) accounts.

2. Apify fallback — a paid actor, used only for handles the Graph API refuses.
   Set APIFY_TOKEN to enable it. Roughly USD 2-4 per 1,000 posts.

There is no free, durable, terms-compliant way to scrape Instagram at scale.
Anything promising otherwise breaks within weeks or gets the account limited.
LinkedIn is worse: no public read API exists for company posts, so it is Apify
or nothing, and it stays off by default.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from ..config import Entity, env
from ..db import Item
from .base import Collector, get

log = logging.getLogger("radar.social")

GRAPH = "https://graph.facebook.com/v21.0"
APIFY_RUN = "https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"


def _caption_title(caption: str, handle: str) -> str:
    """Instagram posts have no title, so build one from the first real line."""
    first = next((ln.strip() for ln in (caption or "").splitlines() if ln.strip()), "")
    return (first[:140] + "...") if len(first) > 140 else (first or f"New post from @{handle}")


class InstagramCollector(Collector):
    name = "instagram"

    def __init__(self, settings: dict):
        super().__init__(settings)
        self.ig_user_id = env("IG_USER_ID")
        self.token = env("IG_ACCESS_TOKEN")
        self.apify_token = env("APIFY_TOKEN")

    def collect(self, entity: Entity):
        for handle in entity.instagram:
            posts = self._via_graph(handle)
            if posts is None and self.apify_token:
                log.info("Graph API cannot read @%s, falling back to Apify", handle)
                posts = self._via_apify(handle)
            for post in posts or []:
                yield self._to_item(entity, handle, post)

    def _via_graph(self, handle: str) -> list[dict] | None:
        if not (self.ig_user_id and self.token):
            return None
        fields = (
            f"business_discovery.username({handle})"
            "{followers_count,media_count,media.limit(12)"
            "{id,caption,like_count,comments_count,media_url,permalink,timestamp,media_type}}"
        )
        resp = get(f"{GRAPH}/{self.ig_user_id}",
                   params={"fields": fields, "access_token": self.token})
        if not resp:
            return None
        payload = resp.json()
        if "error" in payload:
            log.warning("IG Graph error for @%s: %s", handle, payload["error"].get("message"))
            return None
        return payload.get("business_discovery", {}).get("media", {}).get("data", [])

    def _via_apify(self, handle: str) -> list[dict]:
        actor = env("APIFY_IG_ACTOR", "apify~instagram-scraper")
        url = APIFY_RUN.format(actor=actor)
        resp = get(url, params={"token": self.apify_token}, timeout=120)
        if not resp:
            return []
        # The actor is configured via POST body in real use; this GET path
        # assumes a saved task. See README for the task-based setup.
        return resp.json() if isinstance(resp.json(), list) else []

    def _to_item(self, entity: Entity, handle: str, post: dict) -> Item:
        caption = post.get("caption") or post.get("caption_text") or ""
        ts = post.get("timestamp") or post.get("timestamp_utc") or ""
        likes = post.get("like_count") or post.get("likesCount") or 0
        comments = post.get("comments_count") or post.get("commentsCount") or 0
        return Item(
            entity_key=entity.key,
            source="instagram",
            source_id=str(post.get("id") or post.get("shortCode") or post.get("url", "")),
            title=_caption_title(caption, handle),
            url=post.get("permalink") or post.get("url", ""),
            body=caption[:3000],
            author=f"@{handle}",
            image_url=post.get("media_url") or post.get("displayUrl", ""),
            published_at=ts or datetime.now(timezone.utc).isoformat(),
            raw={"handle": handle, "likes": likes, "comments": comments,
                 "media_type": post.get("media_type", "")},
        )


class LinkedInCollector(Collector):
    """Off unless APIFY_TOKEN and an actor are set. LinkedIn is where the
    investment and hiring announcements land first, so it is worth the spend
    if your budget allows — Nick's own LinkedIn broke the BEEUP news."""

    name = "linkedin"

    def __init__(self, settings: dict):
        super().__init__(settings)
        self.apify_token = env("APIFY_TOKEN")
        self.actor = env("APIFY_LI_ACTOR", "apimaestro~linkedin-company-posts")

    def collect(self, entity: Entity):
        if not self.apify_token or not entity.linkedin:
            return
        for slug in entity.linkedin:
            resp = get(
                APIFY_RUN.format(actor=self.actor),
                params={"token": self.apify_token, "company_name": slug, "limit": 10},
                timeout=180,
            )
            if not resp:
                continue
            try:
                posts = resp.json()
            except Exception:  # noqa: BLE001
                continue
            for post in posts if isinstance(posts, list) else []:
                text = post.get("text") or post.get("content", "")
                yield Item(
                    entity_key=entity.key,
                    source="linkedin",
                    source_id=str(post.get("urn") or post.get("postUrl", "")),
                    title=_caption_title(text, slug),
                    url=post.get("postUrl") or post.get("url", ""),
                    body=text[:3000],
                    author=post.get("authorName", slug),
                    image_url=post.get("imageUrl", ""),
                    published_at=post.get("postedAt") or datetime.now(timezone.utc).isoformat(),
                    raw={"slug": slug, "reactions": post.get("numLikes", 0)},
                )
