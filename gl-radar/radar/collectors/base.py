"""Shared plumbing for every collector."""
from __future__ import annotations

import logging
import time
from typing import Iterable

import httpx

from ..config import Entity
from ..db import Item

log = logging.getLogger("radar.collect")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 GLRadar/1.0"
)


def client(timeout: float = 20.0) -> httpx.Client:
    return httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
    )


def get(url: str, retries: int = 2, **kwargs) -> httpx.Response | None:
    """GET with linear backoff. Returns None rather than raising, so one dead
    site never takes down a polling cycle."""
    for attempt in range(retries + 1):
        try:
            with client() as c:
                resp = c.get(url, **kwargs)
            if resp.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp
        except Exception as exc:  # noqa: BLE001 - collectors must not crash the loop
            if attempt == retries:
                log.warning("GET failed %s: %s", url, exc)
                return None
            time.sleep(2 * (attempt + 1))
    return None


class Collector:
    """Subclasses yield Items. They never write to the database themselves."""

    name = "base"

    def __init__(self, settings: dict):
        self.settings = settings

    def collect(self, entity: Entity) -> Iterable[Item]:
        raise NotImplementedError
