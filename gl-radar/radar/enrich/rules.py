"""A classifier that costs nothing.

The Claude classifier is better: it understands that "Goldfinger at 60" is a
Bond retrospective rather than the band, and it writes usable captions. But it
needs a paid API key, and a system nobody turns on because of a bill is worth
less than a noisier one that runs.

So this exists. It uses the same signals the registry already carries — aliases,
exclusions, source, priority — plus a keyword table, and produces the same
verdict shape. No network, no key, no cost.

What you give up, honestly:
  * more false positives, because it matches words rather than reading meaning
  * no written captions, only a prompt to write one
  * category guessed from keywords, so it will sometimes be wrong

What it still gets right: the source-based trust that matters most here. An item
from a company's own website or Instagram is about that company by definition,
and those are the highest-value reposts anyway.

Switch to Claude any time by setting ANTHROPIC_API_KEY; `mode: auto` picks it
up automatically.
"""
from __future__ import annotations

import re

# Entity is passed in duck-typed; no import needed.

# Sources we trust without argument. A post on the brand's own Instagram is
# about the brand; there is nothing to disambiguate.
OWNED_SOURCES = {"instagram", "website", "events", "linkedin"}

SOURCE_FLOOR = {
    "events": 70,      # a dated show is concrete and actionable
    "website": 58,     # the brand said it themselves
    "instagram": 55,
    "linkedin": 52,
    "rss": 48,
    "google_news": 42,
}

# Weighted by how much a Gross Labs repost would want it.
KEYWORDS = [
    (32, "investment", r"\b(invest(s|ed|ment|ing)?|funding|raise[sd]?|series [a-d]\b|"
                       r"backs?|backed|stake|acquir(e|es|ed|ition)|buyout)\b"),
    (28, "partnership", r"\b(partner(s|ship|ed|ing)?|team(s|ed) up|joins? forces|"
                        r"collaborat(e|es|ed|ion)|official (partner|sponsor))\b"),
    (26, "product_launch", r"\b(launch(es|ed|ing)?|debut(s|ed)?|unveil(s|ed|ing)?|"
                           r"introduc(e|es|ed|ing)|new (flavou?r|product|line|drop))\b"),
    (24, "retail_expansion", r"\b(nationwide|rolls? out|now (at|in)|hits? shelves|"
                             r"expand(s|ed|ing)?|stores? across)\b"),
    (24, "music_release", r"\b(new (single|album|ep|song|video)|releases?|drops?|"
                          r"out now|premiere)\b"),
    (22, "event_announcement", r"\b(lineup|line-up|festival|tour|announce(s|d|ment)?|"
                               r"on sale|tickets?|plays?|live at|headlin(e|es|ing))\b"),
    (22, "milestone", r"\b(wins?|won|champion(s|ship)?|record|first ever|milestone|"
                      r"title|trophy|cup)\b"),
    (18, "hiring", r"\b(appoints?|names? .{0,20}(ceo|president|head of)|hires?|"
                   r"joins? as)\b"),
    (16, "press_feature", r"\b(interview|profile|featured in|sits down|q&a|podcast)\b"),
]

# Signals that this is filler even when the name matches.
PENALTIES = [
    (-30, r"\b(\d+ best|top \d+|roundup|listicle|everything we know|here'?s what)\b"),
    (-25, r"\b(rumou?r|speculation|reportedly|could|might|allegedly)\b"),
    (-20, r"\b(sponsored|advertisement|promoted|affiliate)\b"),
]


def _words(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower())


def _matches_any(text: str, phrases: list[str]) -> bool:
    """Word-boundary match, so 'LAGC' does not fire inside 'flagship'."""
    for phrase in phrases:
        cleaned = phrase.strip().lstrip("@").lower()
        if not cleaned:
            continue
        if re.search(rf"(?<!\w){re.escape(cleaned)}(?!\w)", text):
            return True
    return False


class RuleClassifier:
    """Same interface as the Claude classifier, no key required."""

    model = "keyword-rules"

    def __init__(self, settings: dict):
        self.settings = settings

    def classify(self, item: dict, entity) -> dict:
        haystack = _words(f"{item.get('title', '')} {item.get('body', '')}")
        source = item.get("source", "")

        # 1. Relevance.
        owned = source in OWNED_SOURCES
        hit = _matches_any(haystack, list(entity.aliases) + [entity.name])
        blocked = _matches_any(haystack, list(entity.exclude))

        if blocked and not owned:
            return {"relevant": False, "importance": 0, "category": "noise",
                    "reasoning": "matched an exclusion term for this company"}
        if not (owned or hit):
            return {"relevant": False, "importance": 0, "category": "noise",
                    "reasoning": "no alias match and not from an owned source"}

        # 2. Score from source, keywords and penalties.
        score = SOURCE_FLOOR.get(source, 40)
        category = "update"
        best = 0
        for weight, name, pattern in KEYWORDS:
            if re.search(pattern, haystack):
                score += weight // 2
                if weight > best:
                    best, category = weight, name
        for weight, pattern in PENALTIES:
            if re.search(pattern, haystack):
                score += weight

        # An event item already carries its own urgency in the body.
        if source == "events":
            category = "event_reminder"
            if re.search(r"\b(today|tomorrow|happening now)\b", haystack):
                score += 20

        score = max(0, min(100, score))
        title = (item.get("title") or "")[:120]

        return {
            "relevant": True,
            "reasoning": ("owned source, no disambiguation needed" if owned
                          else "matched an alias with no exclusion hit"),
            "category": category,
            "importance": score,
            "repostable": score >= 50,
            "summary": title,
            "repost_angle": "",
            # Deliberately blank. A templated caption would read like a bot and
            # get posted by accident; an empty field asks for thirty seconds of
            # human writing instead.
            "caption": "",
        }
