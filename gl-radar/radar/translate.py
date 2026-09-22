"""Spanish to English, free.

Club Necaxa is a Mexican club and most of its coverage is in Spanish. Two
problems followed from that. The team could not read the cards, and — worse —
the keyword classifier could not either: "anuncia nuevo patrocinador" contains
none of the English words it looks for, so genuine Spanish announcements were
scored as noise before anyone saw them.

So translation happens BEFORE classification, and the English text is what gets
judged. The original is kept alongside it.

Uses MyMemory (api.mymemory.translated.net): documented, free, no key. The
anonymous allowance is about 5,000 characters a day; set MYMEMORY_EMAIL in the
GitHub secrets to raise it to 50,000. Only short text is sent — a headline and
the opening of the summary — and only for items that already name a tracked
company, so the daily volume stays small. Translations are stored with the item
and never requested twice.

Machine translation of sports and business headlines is usually good enough to
understand what happened, which is the point. It is not good enough to quote.
"""
from __future__ import annotations

import logging
import os
import re

from .collectors.base import get

log = logging.getLogger("radar.translate")

ENDPOINT = "https://api.mymemory.translated.net/get"

# Function words that are common in Spanish and rare in English. Counting them
# is crude, and for a headline it is also enough.
SPANISH = {
    "el", "la", "los", "las", "del", "de", "que", "en", "y", "con", "para",
    "por", "una", "un", "sus", "su", "al", "se", "es", "como", "más", "pero",
    "este", "esta", "tras", "sobre", "ante", "hasta", "nuevo", "nueva",
}
ENGLISH = {
    "the", "and", "of", "to", "in", "for", "with", "on", "at", "from", "by",
    "is", "are", "was", "new", "as", "after", "over", "its", "their",
}


def detect(text: str) -> str:
    """Return 'es' or 'en'. Anything uncertain is treated as English."""
    words = re.findall(r"[a-záéíóúñü]+", (text or "").lower())
    if len(words) < 3:
        return "en"
    es = sum(1 for w in words if w in SPANISH)
    en = sum(1 for w in words if w in ENGLISH)
    accents = len(re.findall(r"[áéíóúñ¿¡]", text.lower()))
    return "es" if (es + accents) >= 3 and es > en else "en"


def translate(text: str, source: str = "es", target: str = "en") -> str:
    """Translate one short string. Returns the original on any failure, so a
    translation outage degrades to Spanish cards rather than missing ones."""
    text = (text or "").strip()
    if not text:
        return text
    params = {"q": text[:480], "langpair": f"{source}|{target}"}
    email = os.getenv("MYMEMORY_EMAIL")
    if email:
        params["de"] = email
    resp = get(ENDPOINT, params=params, retries=1, timeout=20)
    if not resp:
        return text
    try:
        data = resp.json()
        out = (data.get("responseData") or {}).get("translatedText") or ""
        # MyMemory reports quota exhaustion inside a 200 response.
        if not out or "MYMEMORY WARNING" in out.upper() or data.get("quotaFinished"):
            log.warning("translation quota reached; leaving text in %s", source)
            return text
        return out.strip()
    except Exception as exc:  # noqa: BLE001
        log.debug("translation parse failed: %s", exc)
        return text
