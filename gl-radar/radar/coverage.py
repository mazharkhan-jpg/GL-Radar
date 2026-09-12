"""What is this system actually watching, and what is it blind to?

The failure that prompted this module: Los Angeles Golf Club had no website
source configured at all. Google News barely indexes it under "LAGC", so the
club could publish news on its own site and nothing here would ever see it.
Nothing was broken — no collector errored, no check failed — the dashboard just
quietly had nothing to say about that company, which looks identical to "no
news happened".

Those two states must never look the same again. This module separates them:

    ok       at least one working source, checked recently
    thin     sources configured, but only weak ones for this kind of company
    blocked  sources configured but unusable, normally a missing API key
    blind    nothing configured that could ever find anything

A dashboard that cannot tell you which of those applies to each company does
not deserve to be trusted, and this one is built to be trusted or ignored on
the evidence rather than on vibes.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import db
from .config import Entity

# A source needs these environment variables to do anything.
REQUIREMENTS = {
    "google_news": [],
    "rss": [],
    "website_diff": [],
    "instagram": ["IG_USER_ID", "IG_ACCESS_TOKEN"],
    "linkedin": ["APIFY_TOKEN"],
    "events": ["TICKETMASTER_API_KEY"],
}

# Instagram has a documented fallback, so treat either credential set as enough.
ALTERNATIVES = {"instagram": ["APIFY_TOKEN"]}

# Google News alone is not coverage. It indexes trade press well and brand
# announcements poorly, and for a generically named entity it returns noise.
WEAK_ALONE = {"google_news"}


def _have(keys: list[str]) -> bool:
    return all(os.getenv(k) for k in keys) if keys else True


@dataclass
class SourceState:
    name: str
    configured: bool
    enabled: bool
    credentialed: bool
    reason: str = ""

    @property
    def working(self) -> bool:
        return self.configured and self.enabled and self.credentialed


@dataclass
class EntityCoverage:
    key: str
    name: str
    priority: int
    sources: list[SourceState] = field(default_factory=list)
    last_item_at: str = ""
    items_in_window: int = 0

    @property
    def working(self) -> list[str]:
        return [s.name for s in self.sources if s.working]

    @property
    def blocked(self) -> list[SourceState]:
        return [s for s in self.sources if s.configured and not s.working]

    @property
    def status(self) -> str:
        working = set(self.working)
        if not working:
            return "blocked" if self.blocked else "blind"
        if working <= WEAK_ALONE:
            return "thin"
        return "ok"

    @property
    def headline(self) -> str:
        if self.status == "blind":
            return "No source configured — this company cannot be monitored"
        if self.status == "blocked":
            reasons = {s.reason for s in self.blocked if s.reason}
            return "Sources configured but unusable: " + "; ".join(sorted(reasons))
        if self.status == "thin":
            return "Google News only — brand announcements will be missed"
        return f"{len(self.working)} source(s): {', '.join(sorted(self.working))}"


def system_readiness(settings: dict) -> dict[str, SourceState]:
    """Which collectors could run right now, regardless of any one company."""
    enabled = settings.get("collectors", {}) or {}
    states: dict[str, SourceState] = {}
    for name, keys in REQUIREMENTS.items():
        ok = _have(keys) or _have(ALTERNATIVES.get(name, []) or ["__never__"])
        missing = [k for k in keys if not os.getenv(k)]
        states[name] = SourceState(
            name=name,
            configured=True,
            enabled=bool(enabled.get(name)),
            credentialed=ok,
            reason=("" if ok else f"{name} needs {', '.join(missing)}"),
        )
    return states


def entity_sources(entity: Entity, settings: dict) -> list[SourceState]:
    """Per-company view: a collector being switched on is not the same as this
    company having anything for it to read."""
    system = system_readiness(settings)
    configured = {
        "google_news": bool(entity.aliases or entity.name),
        "rss": bool(entity.rss or entity.domains),
        "website_diff": bool(entity.watch_pages),
        "instagram": bool(entity.instagram),
        "linkedin": bool(entity.linkedin),
        "events": bool(entity.ticketmaster),
    }
    out = []
    for name, is_configured in configured.items():
        base = system[name]
        out.append(SourceState(
            name=name,
            configured=is_configured,
            enabled=base.enabled,
            credentialed=base.credentialed,
            reason=("" if is_configured else f"nothing configured for {name}") or base.reason,
        ))
    return out


def report(conn, settings: dict, entities: list[Entity], since: str = "") -> list[EntityCoverage]:
    out = []
    for entity in entities:
        row = conn.execute(
            "SELECT MAX(first_seen) AS last FROM items WHERE entity_key = ?",
            (entity.key,),
        ).fetchone()
        count = 0
        if since:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM items WHERE entity_key = ? AND first_seen >= ?",
                (entity.key, since),
            ).fetchone()["n"]
        out.append(EntityCoverage(
            key=entity.key, name=entity.name, priority=entity.priority,
            sources=entity_sources(entity, settings),
            last_item_at=row["last"] or "",
            items_in_window=count,
        ))
    return out


def gaps(coverage: list[EntityCoverage], max_priority: int = 2) -> list[EntityCoverage]:
    """Companies that matter and cannot currently be watched properly."""
    return [c for c in coverage
            if c.priority <= max_priority and c.status in ("blind", "blocked", "thin")]


def pipeline_ready(settings: dict | None = None) -> tuple[bool, str]:
    """Can anything be scored at all, and how well?

    Returns (ok, note). `ok` is False only when scoring is genuinely impossible,
    which now means someone forced `mode: claude` without a key. Running on free
    keyword rules is a real degradation and says so, but it is not an outage.
    """
    mode = ((settings or {}).get("classifier", {}) or {}).get("mode", "auto")
    has_key = bool(os.getenv("ANTHROPIC_API_KEY"))
    if mode == "claude" and not has_key:
        return False, ("classifier.mode is 'claude' but ANTHROPIC_API_KEY is not set, "
                       "so nothing will be scored. Use mode 'auto' or add the key.")
    if mode == "rules" or not has_key:
        return True, ("Scoring with free keyword rules. Works with no API key; "
                      "expect more false positives and no written captions. "
                      "Add ANTHROPIC_API_KEY for better filtering.")
    return True, ""
