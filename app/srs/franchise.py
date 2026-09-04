"""Which cards count as "the same anime" (multi-card words, 2026-09-03).

The user: sibling cards of one word must come "from different animes only". AniList
gives every season its own id and title ("Example Series 4th Season", "EXAMPLE:
Kami no Shiken-hen"), so the anilist_id is too fine: seasons of one show are one
anime. `franchise_key` collapses a title to its franchise — season/part markers and
a colon-separated subtitle are dropped ("Re:Example" keeps its colon: no space
after it).
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional

# Trailing season / part markers, applied repeatedly ("… 2nd Season Part 2").
_SEASON_RE = re.compile(
    r"\s*(?:"
    r"\b\d+(?:st|nd|rd|th)\s+season\b"
    r"|\bseason\s+\d+\b"
    r"|\bpart\s+\d+\b"
    r"|\bcour\s+\d+\b"
    r"|\bfinal\s+season\b"
    r"|\b(?:2nd|3rd|4th|5th)\b"
    r"|\b(?:ii|iii|iv)\b"
    r")\s*$",
    re.IGNORECASE,
)
# A subtitle after "colon + space" ("EXAMPLE: Kami …"); "Re:Example" has no space.
_SUBTITLE_RE = re.compile(r":\s+")


def franchise_key(title: Optional[str]) -> str:
    """Lower-cased franchise identity of a show title ('' for no title)."""
    t = unicodedata.normalize("NFKC", title or "").strip().lower()
    if not t:
        return ""
    t = _SUBTITLE_RE.split(t, maxsplit=1)[0]
    prev = None
    while prev != t:
        prev = t
        t = _SEASON_RE.sub("", t).strip()
    return t.strip(" -–—:").strip()


def same_franchise(a: Optional[str], b: Optional[str]) -> bool:
    """True when two show titles belong to the same anime (both non-empty)."""
    ka, kb = franchise_key(a), franchise_key(b)
    return bool(ka) and ka == kb
