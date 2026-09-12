"""Load and normalise configuration."""
from __future__ import annotations

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
    context: str = ""
    domains: list[str] = field(default_factory=list)
    rss: list[str] = field(default_factory=list)
    watch_pages: list[str] = field(default_factory=list)
    instagram: list[str] = field(default_factory=list)
    linkedin: list[str] = field(default_factory=list)
    twitter: list[str] = field(default_factory=list)
    youtube: list[str] = field(default_factory=list)
    ticketmaster: str = ""
    priority: int = 2
    repost_bias: str = ""

    @property
    def search_query(self) -> str:
        """A Google News query that is specific enough to survive generic names."""
        quoted = [f'"{a}"' for a in self.aliases[:4]]
        query = " OR ".join(quoted) if quoted else f'"{self.name}"'
        if self.exclude:
            query += " " + " ".join(f'-"{x}"' for x in self.exclude[:5])
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


def load_settings() -> dict:
    raw = yaml.safe_load((CONFIG_DIR / "settings.yaml").read_text())
    return _interpolate(raw)


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default)
