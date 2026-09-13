"""Turn a raw item into a judgement.

Keyword matching alone gives you a feed full of James Bond retrospectives and
Norwegian Breakaway cruise reviews. This step hands Claude the entity's context
paragraph from companies.yaml and asks for a structured verdict, which is what
makes disambiguation and importance scoring work.

Cost: roughly USD 0.002 per item on Sonnet. At ~150 items a day that is a few
cents. Items are classified once and cached forever in the signals table.
"""
from __future__ import annotations

import json
import logging
import re

from anthropic import Anthropic

from ..config import Entity, env

log = logging.getLogger("radar.classify")

CATEGORIES = [
    "investment", "acquisition", "partnership", "collaboration", "product_launch",
    "retail_expansion", "event_announcement", "event_reminder", "music_release",
    "tour_date", "press_feature", "award", "milestone", "hiring", "noise",
]

SYSTEM = """You are the editorial filter for Gross Labs' social media team.

They run the @grosslabs Instagram account and repost news about Nick Gross's
portfolio companies. Your job is to read one item found on the internet and
decide two things: is it actually about the company in question, and is it
worth reposting.

Be ruthless about false positives. Several of these brands have names that
collide with far more famous things. If an item does not clearly concern the
entity described in the context block, mark it irrelevant and stop.

Importance is 0-100:
  90-100  Major: new investment, acquisition, a headline partnership, a launch
          with a household name attached, a chart-topping release.
  70-89   Strong: retail expansion, festival lineup drop, notable press feature,
          new signing, a show happening within a week.
  50-69   Solid: routine tour dates, product news, smaller collaborations,
          trade press mentions.
  25-49   Marginal: roundups where the brand is one of many, minor updates.
  0-24    Noise: syndicated duplicates, listicles, unrelated coincidence.

A caption should sound like the Gross Labs account: confident, short, no
hashtag spam, no emoji walls, no corporate throat-clearing. Credit the source
account when the item came from Instagram.

Respond with JSON only. No markdown fences, no preamble."""

TEMPLATE = """<entity>
Name: {name}
Type: {kind}
What this is: {context}
What we like to repost: {bias}
Names that are NOT this entity: {exclude}
Must also mention at least one of these to count: {requires}
</entity>

<item>
Source: {source}
Author or outlet: {author}
Published: {published}
Title: {title}
Body: {body}
URL: {url}
</item>

Return this exact JSON shape:
{{
  "relevant": true or false,
  "reasoning": "one sentence on why this is or is not the entity",
  "category": one of {categories},
  "importance": 0-100 integer,
  "repostable": true or false,
  "summary": "one sentence a social manager can read in two seconds",
  "repost_angle": "what the post should lead with, or empty if not repostable",
  "caption": "a ready-to-paste Instagram caption under 300 characters, or empty"
}}"""


class Classifier:
    def __init__(self, settings: dict):
        cfg = settings.get("classifier", {})
        self.model = cfg.get("model", "claude-sonnet-4-6")
        self.max_tokens = cfg.get("max_tokens", 1200)
        self.client = Anthropic(api_key=env("ANTHROPIC_API_KEY"))

    def classify(self, item: dict, entity: Entity) -> dict:
        prompt = TEMPLATE.format(
            name=entity.name,
            kind=entity.kind,
            context=entity.context.strip(),
            bias=entity.repost_bias or "Anything that makes the portfolio look active.",
            exclude=", ".join(entity.exclude) or "none",
            requires=(", ".join(entity.requires)
                      if getattr(entity, "generic", False) and entity.requires
                      else "no extra requirement, the name is distinctive enough"),
            source=item.get("source", ""),
            author=item.get("author", ""),
            published=item.get("published_at", ""),
            title=item.get("title", ""),
            body=(item.get("body") or "")[:2500],
            url=item.get("url", ""),
            categories=CATEGORIES,
        )
        try:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(b.text for b in resp.content if b.type == "text")
            return self._parse(text)
        except Exception as exc:  # noqa: BLE001
            log.error("classify failed: %s", exc)
            # Fail closed: an unclassified item should not become a ticket.
            return {"relevant": False, "importance": 0, "category": "noise",
                    "reasoning": f"classifier error: {exc}"}

    @staticmethod
    def _parse(text: str) -> dict:
        cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", cleaned, re.S)
            if not match:
                return {"relevant": False, "importance": 0, "category": "noise",
                        "reasoning": "unparseable response"}
            data = json.loads(match.group(0))
        data["importance"] = max(0, min(100, int(data.get("importance", 0))))
        return data


def apply_priority(importance: int, priority: int, settings: dict) -> int:
    """Nudge the score by entity priority so Gross Labs itself outranks a
    third-tier watchlist holding with identical news."""
    boosts = settings.get("scoring", {}).get("priority_boost", {})
    return max(0, min(100, importance + int(boosts.get(priority, 0))))


def build(settings: dict):
    """Return whichever classifier the environment can actually run.

    `mode: auto` (the default) uses Claude when ANTHROPIC_API_KEY is present and
    falls back to free keyword rules when it is not, so the system works with no
    paid account and quietly upgrades itself the day a key is added.
    """
    import os
    from .rules import RuleClassifier

    mode = (settings.get("classifier", {}) or {}).get("mode", "auto")
    if mode == "rules":
        return RuleClassifier(settings)
    if mode == "claude" or (mode == "auto" and os.getenv("ANTHROPIC_API_KEY")):
        return Classifier(settings)
    log.info("No ANTHROPIC_API_KEY; scoring with free keyword rules")
    return RuleClassifier(settings)
