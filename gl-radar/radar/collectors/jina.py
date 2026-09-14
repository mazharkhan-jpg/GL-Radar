"""Jina Reader: free page rendering, no key, no login, no ban risk.

Borrowed from Agent-Reach's stack, which picks Jina Reader as its default for
reading arbitrary web pages and as its no-login fallback for LinkedIn.

Two jobs here:

1. LinkedIn company pages. Previously written off as paid-only, because the
   post-level APIs are. But the public company page renders without an account,
   and Jina turns it into clean text. Thinner than a real feed and the logged-out
   view hides a lot, so treat it as a bonus channel rather than a replacement.

2. Brand sites the plain fetcher cannot read. Webflow, React and Squarespace
   sites often ship an empty shell plus JavaScript, so the existing
   website_diff collector sees no links at all. Jina executes the page first.

Costs nothing, needs no credentials, and runs fine in GitHub Actions — which
is the part that matters, since anything needing a logged-in desktop browser
cannot run there at all.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone

from ..config import Entity
from ..db import Item
from .base import Collector, get

log = logging.getLogger("radar.jina")

READER = "https://r.jina.ai/"
LINKEDIN_COMPANY = "https://www.linkedin.com/company/{slug}/posts/"

# Jina returns markdown. These are the boilerplate lines every LinkedIn page
# carries, which would otherwise look like new content on every single run.
# Matched against the START of a block, not the whole of it. The consent
# notices run long, so an anchored full-line match let them through.
NOISE = re.compile(
    r"^(sign in|join now|skip to|agree & join|new to linkedin|by clicking|"
    r"continue with|continue to join|forgot password|people also viewed|"
    r"similar pages|browse jobs|see all|show more|show less|"
    r"linkedin corporation|privacy policy|user agreement|cookie policy|"
    r"create your free account|welcome back|email or phone|"
    r"this button displays|report this|about us|accessibility|\W*$)",
    re.I,
)

# Phrases that mark a block as chrome wherever they appear in it.
NOISE_ANYWHERE = re.compile(
    r"(agree to linkedin|user agreement, privacy policy|"
    r"cookie policy|new to linkedin\?|already on linkedin\?|"
    r"sign in to see|join to view|to see who you already know)",
    re.I,
)


def _read(url: str, timeout: float = 45.0) -> str:
    """Fetch a page as markdown. Returns empty string on any failure."""
    resp = get(READER + url, retries=1, timeout=timeout,
               headers={"X-Return-Format": "markdown", "Accept": "text/plain"})
    return resp.text if resp else ""


def _blocks(markdown: str, min_len: int = 60) -> list[str]:
    """Split rendered markdown into candidate post-sized chunks."""
    out = []
    for raw in re.split(r"\n{2,}", markdown):
        line = re.sub(r"\s+", " ", re.sub(r"[#*_>`\[\]()]", " ", raw)).strip()
        if len(line) < min_len or NOISE.match(line) or NOISE_ANYWHERE.search(line):
            continue
        out.append(line)
    return out


class JinaCollector(Collector):
    name = "jina"

    def collect(self, entity: Entity):
        yield from self._linkedin(entity)
        yield from self._rendered_pages(entity)

    def _linkedin(self, entity: Entity):
        for slug in entity.linkedin:
            url = LINKEDIN_COMPANY.format(slug=slug)
            text = _read(url)
            if not text:
                log.info("no LinkedIn content for %s", slug)
                continue
            for block in _blocks(text)[:8]:
                yield Item(
                    entity_key=entity.key,
                    source="linkedin",
                    # Hash the content: LinkedIn gives no stable post id in the
                    # logged-out view, so the text itself is the identity.
                    source_id=hashlib.sha256(block.encode()).hexdigest()[:20],
                    title=block[:140] + ("..." if len(block) > 140 else ""),
                    url=url,
                    body=block[:2000],
                    author=f"linkedin.com/company/{slug}",
                    published_at=datetime.now(timezone.utc).isoformat(),
                    raw={"slug": slug, "via": "jina-reader"},
                )

    def _rendered_pages(self, entity: Entity):
        """Pages the plain fetcher returns empty, because they need JavaScript."""
        for url in getattr(entity, "rendered_pages", []) or []:
            text = _read(url)
            if not text:
                continue
            for block in _blocks(text, min_len=80)[:10]:
                yield Item(
                    entity_key=entity.key,
                    source="website",
                    source_id=hashlib.sha256((url + block).encode()).hexdigest()[:20],
                    title=block[:140] + ("..." if len(block) > 140 else ""),
                    url=url,
                    body=block[:2000],
                    author=url.split("/")[2] if "/" in url else url,
                    published_at=datetime.now(timezone.utc).isoformat(),
                    raw={"page": url, "via": "jina-reader"},
                )
