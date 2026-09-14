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

# Bump this whenever the matching logic below changes. It is part of the
# fingerprint that triggers automatic re-scoring of everything already stored.
RULES_VERSION = "2026-09-13.5"

# The question every item has to answer is "what does this have to do with
# Gross Labs?". When a company has no corroboration list of its own, these
# stand in: any of them ties an article to the umbrella.
UMBRELLA_TERMS = [
    "Gross Labs", "Nick Gross", "Brekky Golf", "Big Noise", "Find Your Grind",
    "girlfriends", "Goldfinger", "Breakaway", "BEEUP", "Stableford",
    "Los Angeles Golf Club", "LAGC", "TMRW Sports", "TGL", "OKC United",
    "John Feldmann", "Shaun Neff", "Travis Mills", "Sean Maher",
    "Necaxa", "X Games", "Kewl Ventures", "Meriwether", "h.wood",
    "portfolio company", "family office", "investment firm",
]

# Sources we trust without argument. A post on the brand's own Instagram is
# about the brand; there is nothing to disambiguate.
OWNED_SOURCES = {"instagram", "website", "events", "linkedin"}

# Domains that structurally cannot carry news about this portfolio. IMDb will
# never report a Big Noise signing; it will only ever surface a 1944 Laurel and
# Hardy picture called The Big Noise. Blocking the domain is cheaper and more
# reliable than trying to out-word a film database.
BLOCKED_DOMAINS = {
    "imdb.com", "rottentomatoes.com", "letterboxd.com", "themoviedb.org",
    "wikipedia.org", "fandom.com", "discogs.com", "allmovie.com",
    "tvguide.com", "justwatch.com", "moviefone.com",
}

SOURCE_FLOOR = {
    "events": 70,      # a dated show is concrete and actionable
    "website": 58,     # the brand said it themselves
    "instagram": 55,
    "linkedin": 52,
    "rss": 50,
    "google_news": 46,
}

# Weighted by how much a Gross Labs repost would want it.
KEYWORDS = [
    (32, "investment", r"\b(invest(s|ed|ment|ing)?|funding|raise[sd]?|series [a-d]\b|"
                       r"backs?|backed|stake|acquir(e|es|ed|ition)|buyout|"
                       r"valuation|valued at|worth \$|\$\d+ ?(m|b|million|billion)|"
                       r"unicorn|round)\b"),
    (28, "partnership", r"\b(partner(s|ship|ed|ing)?|team(s|ed) up|joins? forces|"
                        r"collaborat(e|es|ed|ion)|official (partner|sponsor))\b"),
    (26, "product_launch", r"\b(launch(es|ed|ing)?|debut(s|ed)?|unveil(s|ed|ing)?|"
                           r"introduc(e|es|ed|ing)|"
                           r"new ([\w-]+ )?"
                           r"(flavou?r|product|line|drop|menu|collection|range))\b"),
    (24, "retail_expansion", r"\b(nationwide|rolls? out|now (at|in)|hits? shelves|"
                             r"expand(s|ed|ing)?|stores? across|"
                             r"opens?|opening|reopens?|"                             r"new (location|venue|site|clubhouse|flagship)|"
                             r"second location)\b"),
    # Property and venue projects move in milestones, not launches.
    (24, "milestone", r"\b(breaks? ground|groundbreaking|topping out|"
                      r"under construction|completes?|phase (one|two|1|2))\b"),
    (24, "music_release", r"\b(new (single|album|ep|song|video)|releases?|drops?|"
                          r"out now|premiere)\b"),
    (22, "event_announcement", r"\b(lineup|line-up|festival|tour|announce(s|d|ment)?|"
                               r"on sale|tickets?|plays?|live at|headlin(e|es|ing)|"
                               r"returns?|comeback|new (format|season|edition))\b"),
    (22, "milestone", r"\b(wins?|won|champion(s|ship)?|record|first ever|milestone|"
                      r"title|trophy|cup)\b"),
    # Signings are core business for a label, and were slipping under the floor.
    # Kept narrow: a bare "signs" would match "signs of trouble".
    (26, "signing", r"\b(sign(s|ed|ing)? (a |an |the )?(new )?"
                    r"(artist|act|band|deal|record deal|contract)|"
                    r"roster|inks? (a |an )?deal|joins the (label|roster)|"
                    r"sign(s|ed|ing)? (a |an |the )?(new )?"
                    r"(striker|player|midfielder|forward|goalkeeper|coach|manager)|"
                    r"adds? .{0,15} to (its|the) (roster|label))\b"),
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
        url = (item.get("url") or "").lower()

        # 1. Relevance, in four tests, cheapest first.
        owned = source in OWNED_SOURCES

        # Google News hands back a news.google.com redirect rather than the
        # publisher's own URL, so checking the link alone misses everything.
        # The publisher name comes through separately and is what actually
        # identifies IMDb as the source.
        publisher = _words(item.get("author", ""))
        if not owned:
            hit_domain = any(d in url for d in BLOCKED_DOMAINS)
            hit_publisher = any(d.split(".")[0] in publisher for d in BLOCKED_DOMAINS)
            if hit_domain or hit_publisher:
                return {"relevant": False, "importance": 0, "category": "noise",
                        "reasoning": "from a source that cannot carry this company's news"}

        hit = _matches_any(haystack, list(entity.aliases) + [entity.name])
        blocked = _matches_any(haystack, list(entity.exclude))

        if blocked and not owned:
            return {"relevant": False, "importance": 0, "category": "noise",
                    "reasoning": "matched an exclusion term for this company"}
        if not (owned or hit):
            return {"relevant": False, "importance": 0, "category": "noise",
                    "reasoning": "no alias match and not from an owned source"}

        # The important one, and it applies to every company by default.
        # A brand name proves nothing on its own: "breakaway group" describes a
        # political splinter, "big noise" is an idiom, "Stableford" is a scoring
        # format. The item has to say something that ties it to this company or
        # to the Gross Labs umbrella, or it does not belong here — whatever
        # word it happens to share with a portfolio brand.
        if getattr(entity, "generic", True) and not owned:
            corroborating = list(entity.requires) or UMBRELLA_TERMS
            if not _matches_any(haystack, corroborating):
                return {"relevant": False, "importance": 0, "category": "noise",
                        "reasoning": ("name matched but nothing in the item connects it "
                                      "to this company or to Gross Labs")}

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
