"""Open-web collectors. Free, reliable, and the backbone of the system.

Between Google News and a handful of watched pages you catch most funding,
partnership and press announcements within an hour of publication — usually
before the brand posts about it on Instagram.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from urllib.parse import quote_plus, urljoin, urlparse

import feedparser
from bs4 import BeautifulSoup

from ..config import Entity
from ..db import Item
from .base import Collector, get

GOOGLE_NEWS = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"


def _ts(entry) -> str:
    parsed = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if parsed:
        return datetime(*parsed[:6], tzinfo=timezone.utc).isoformat()
    return datetime.now(timezone.utc).isoformat()


def _strip_html(html: str, limit: int = 1500) -> str:
    text = BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text)[:limit]


class GoogleNewsCollector(Collector):
    """One targeted query per entity, using the alias and exclusion lists.

    The exclusions matter enormously here: an unqualified 'Goldfinger' query
    returns the Bond film, and 'Breakaway' returns a cruise ship.
    """

    name = "google_news"

    def collect(self, entity: Entity):
        url = GOOGLE_NEWS.format(query=quote_plus(entity.search_query))
        resp = get(url)
        if not resp:
            return
        for entry in feedparser.parse(resp.text).entries[:25]:
            source = (entry.get("source", {}) or {}).get("title", "")
            yield Item(
                entity_key=entity.key,
                source="google_news",
                source_id=entry.get("id", entry.get("link", "")),
                title=entry.get("title", "").rsplit(" - ", 1)[0].strip(),
                url=entry.get("link", ""),
                body=_strip_html(entry.get("summary", "")),
                author=source,
                published_at=_ts(entry),
                raw={"publisher": source},
            )


class RSSCollector(Collector):
    """Direct feeds declared on an entity, plus auto-discovered ones."""

    name = "rss"

    def collect(self, entity: Entity):
        feeds = list(entity.rss)
        for domain in entity.domains:
            feeds.extend(self._discover(domain))
        for feed_url in dict.fromkeys(feeds):
            resp = get(feed_url)
            if not resp:
                continue
            for entry in feedparser.parse(resp.text).entries[:20]:
                yield Item(
                    entity_key=entity.key,
                    source="rss",
                    source_id=entry.get("id", entry.get("link", "")),
                    title=entry.get("title", ""),
                    url=entry.get("link", ""),
                    body=_strip_html(entry.get("summary", "")),
                    author=entry.get("author", urlparse(feed_url).netloc),
                    image_url=self._image(entry),
                    published_at=_ts(entry),
                    raw={"feed": feed_url},
                )

    @staticmethod
    def _image(entry) -> str:
        for media in entry.get("media_content", []) or []:
            if media.get("url"):
                return media["url"]
        for link in entry.get("links", []) or []:
            if link.get("type", "").startswith("image"):
                return link.get("href", "")
        return ""

    @staticmethod
    def _discover(domain: str) -> list[str]:
        """Most brand sites are Webflow, WordPress or Squarespace. All three
        expose a predictable feed path."""
        base = domain if domain.startswith("http") else f"https://{domain}"
        candidates = [f"{base}/feed", f"{base}/rss.xml", f"{base}/feed.xml",
                      f"{base}/blog/feed", f"{base}/news/feed"]
        found = []
        resp = get(base)
        if resp:
            soup = BeautifulSoup(resp.text, "html.parser")
            for link in soup.find_all("link", type=re.compile("rss|atom")):
                if link.get("href"):
                    found.append(urljoin(base, link["href"]))
        return found or candidates[:2]


class WebsiteDiffCollector(Collector):
    """Watches a page and reports links that were not there last time.

    This is how you catch a new tour date appearing on breakawayfestival.com
    or a press release landing on grosslabs.com/news before anyone syndicates it.
    Cursor state lives in the `cursors` table keyed by page URL.
    """

    name = "website_diff"

    def __init__(self, settings: dict, conn):
        super().__init__(settings)
        self.conn = conn

    def collect(self, entity: Entity):
        from ..db import get_cursor, set_cursor

        for page in entity.watch_pages:
            resp = get(page)
            if not resp:
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            links = self._meaningful_links(soup, page)

            cursor_key = f"pagelinks:{page}"
            seen = set(filter(None, get_cursor(self.conn, cursor_key).split("\n")))
            fresh = {h: t for h, t in links.items() if h not in seen}

            # First sight of a page establishes a baseline instead of firing
            # one alert per link already on it.
            if not seen:
                set_cursor(self.conn, cursor_key, "\n".join(links))
                continue

            for href, text in list(fresh.items())[:15]:
                yield Item(
                    entity_key=entity.key,
                    source="website",
                    source_id=hashlib.sha256(href.encode()).hexdigest()[:20],
                    title=text or f"New page on {urlparse(page).netloc}",
                    url=href,
                    body=f"New link detected on {page}",
                    author=urlparse(page).netloc,
                    published_at=datetime.now(timezone.utc).isoformat(),
                    raw={"watch_page": page},
                )
            set_cursor(self.conn, cursor_key, "\n".join(set(links) | seen))

    @staticmethod
    def _meaningful_links(soup: BeautifulSoup, page: str) -> dict[str, str]:
        host = urlparse(page).netloc
        out: dict[str, str] = {}
        skip = re.compile(r"(privacy|terms|cookie|login|cart|#|mailto:|tel:|\.(png|jpg|svg|css|js)$)", re.I)
        for a in soup.find_all("a", href=True):
            href = urljoin(page, a["href"]).split("?")[0].rstrip("/")
            text = re.sub(r"\s+", " ", a.get_text(" ", strip=True))[:200]
            if urlparse(href).netloc != host or skip.search(href) or len(text) < 8:
                continue
            out[href] = text
        return out
