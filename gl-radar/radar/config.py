"""Load and normalise configuration."""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"

load_dotenv(ROOT / ".env")

_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _interpolate(value: Any) -> Any:
    """Replace ${VAR} with the environment value, recursively."""
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: os.getenv(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    return value


@dataclass
class Entity:
    """A company or person we track. Collectors read this; the classifier reads context."""

    key: str
    name: str
    kind: str = "company"
    aliases: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    # Terms that must ALSO appear for a match to count. Only used when
    # `generic` is true. "Breakaway" and "Big Noise" are ordinary English
    # phrases; the alias alone proves nothing.
    requires: list[str] = field(default_factory=list)
    # Defaults to True deliberately. A company added to the registry without
    # anyone thinking about name collisions gets the strict treatment, rather
    # than quietly matching every article that happens to share a word with it.
    # Setting this to False is a claim that the name is unmistakable.
    generic: bool = True
    # For companies that appear in constant routine coverage (sports clubs,
    # leagues). Requires an actual announcement, not just a mention.
    news_only: bool = False
    # Used to group the dashboard rail, and a decent predictor of what kind of
    # noise a company generates: Sport produces daily fixture coverage,
    # Consumer produces retail news, Hospitality produces openings.
    sector: str = "Other"
    context: str = ""
    domains: list[str] = field(default_factory=list)
    rss: list[str] = field(default_factory=list)
    watch_pages: list[str] = field(default_factory=list)
    # Pages that need JavaScript executed before there is anything to read.
    # Handled by the Jina collector rather than the plain fetcher.
    rendered_pages: list[str] = field(default_factory=list)
    instagram: list[str] = field(default_factory=list)
    linkedin: list[str] = field(default_factory=list)
    twitter: list[str] = field(default_factory=list)
    youtube: list[str] = field(default_factory=list)
    ticketmaster: str = ""
    priority: int = 2
    repost_bias: str = ""

    @property
    def search_query(self) -> str:
        """A Google News query specific enough to survive generic names.

        For a generic brand, the query demands a corroborating term up front so
        the junk is filtered at the source rather than downstream. That is the
        difference between "Breakaway" returning a festival and returning a
        Kashmiri political splinter group.
        """
        quoted = [f'"{a}"' for a in self.aliases[:4]]
        query = " OR ".join(quoted) if quoted else f'"{self.name}"'
        if self.generic and self.requires:
            corroborate = " OR ".join(f'"{r}"' for r in self.requires[:6])
            query = f"({query}) AND ({corroborate})"
        if self.exclude:
            query += " " + " ".join(f'-"{x}"' for x in self.exclude[:8])
        return query


def load_entities() -> list[Entity]:
    raw = yaml.safe_load((CONFIG_DIR / "companies.yaml").read_text())
    entities: list[Entity] = []
    for person in raw.get("people", []) or []:
        fields = dict(person)
        fields["key"] = fields.get("key") or fields["name"].lower().replace(" ", "_")
        entities.append(Entity(kind="person", **fields))
    for company in raw.get("companies", []) or []:
        entities.append(Entity(kind="company", **company))
    return entities


def registry_fingerprint() -> str:
    """Identifies the rules that produced a verdict.

    Verdicts are cached forever, which is right for cost but wrong the moment
    the rules change: tightening an exclusion list did nothing to items already
    scored, so false positives sat in the queue looking permanent. Hashing the
    registry plus the classifier version means a config edit automatically
    invalidates every verdict it could have affected, with no manual purge.
    """
    from .enrich.rules import RULES_VERSION

    raw = (CONFIG_DIR / "companies.yaml").read_bytes()
    return hashlib.sha256(raw + RULES_VERSION.encode()).hexdigest()[:16]


def load_settings() -> dict:
    raw = yaml.safe_load((CONFIG_DIR / "settings.yaml").read_text())
    return _interpolate(raw)


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default)
