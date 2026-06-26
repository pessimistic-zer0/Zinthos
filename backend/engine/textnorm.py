"""Shared title/artist normalization for fuzzy library matching (M7.2).

LOAD-BEARING: this exact code computes the `track_match.norm_key` at BUILD time
(build_track_match.py) AND at QUERY time (library.py). If the two ever diverge, nothing
matches. Keep this module dependency-free so both can import it cheaply.

The key folds away the real-world variance in tags — case, "(Remastered)", "feat. X",
"- Live", punctuation — while PRESERVING non-ASCII letters (the catalog has Chinese,
Cyrillic, accented titles), so equality on the normalized key means "same song".
"""
from __future__ import annotations

import re

# Bracketed asides: "(feat. X)", "[Live]", "{Remix}".
_BRACKETS = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
# Featured-artist tail: "feat. X", "ft X", "featuring X" (NOT "with" — it eats real titles).
_FEAT = re.compile(r"\b(feat|ft|featuring)\b.*$", re.IGNORECASE)
# Decorative " - <suffix>" tails: "- Remastered 2011", "- Radio Edit", "- Live", etc.
_DASH_TAIL = re.compile(
    r"\s-\s.*\b(remaster(ed)?|live|radio edit|single version|album version|mono|stereo|"
    r"version|edit|deluxe|bonus|acoustic|instrumental|demo|mix|remix|re-?recorded|"
    r"anniversary|expanded|original)\b.*$",
    re.IGNORECASE,
)
# Drop punctuation/symbols but keep unicode letters/digits and spaces.
_NONWORD = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")

# Separators between credited artists in a tag's artist field. Used ONLY by the first-artist
# fallback (M7.2.1): a multi-artist tag ("ROSÉ & Bruno Mars", "A; B; C") whose catalog
# PRIMARY artist is one name. Applied to the RAW string before _norm eats the punctuation.
_ARTIST_SPLIT = re.compile(
    r"\s*(?:;|/|&|,|\+|×|\bx\b|\bfeat\b|\bft\b|\bfeaturing\b|\bvs\b|\bwith\b)\s*",
    re.IGNORECASE,
)
# Edition markers some taggers jam into the TITLE ("Play Date Explicit"); the catalog title
# rarely carries them, so an edition-stripped title variant is worth probing too.
_EDITION = re.compile(r"\b(explicit|clean)\b", re.IGNORECASE)


def _norm(text: str) -> str:
    t = text.casefold()
    t = _BRACKETS.sub(" ", t)
    t = _FEAT.sub(" ", t)
    t = _DASH_TAIL.sub(" ", t)
    t = _NONWORD.sub(" ", t).replace("_", " ")
    return _WS.sub(" ", t).strip()


def match_key(title: str | None, artist: str | None) -> str | None:
    """`"<norm title>|<norm artist>"`, or None if either side is empty after folding."""
    if not title or not artist:
        return None
    nt, na = _norm(title), _norm(artist)
    if not nt or not na:
        return None
    return f"{nt}|{na}"


def split_artists(artist: str) -> list[str]:
    """Every credited artist in a multi-artist tag ("A & B feat. C" → [A, B, C])."""
    return [p.strip() for p in _ARTIST_SPLIT.split(artist) if p and p.strip()]


def candidate_keys(title: str | None, artist: str | None) -> set[str]:
    """All `"<title>|<artist>"` keys worth trying for one local file (M7.2.1).

    The catalog stores ONE (non-authoritative-order) artist per track, and tags credit
    collaborators in any order, so we probe EVERY artist the tag lists — full string and each
    split part — against a title plus an edition-stripped variant ("Play Date Explicit" →
    "play date"). Purely additive: more keys only ever turn a miss into a hit."""
    if not title or not artist:
        return set()
    nt = _norm(title)
    titles = {nt} if nt else set()
    if nt and (te := _WS.sub(" ", _EDITION.sub(" ", nt)).strip()):
        titles.add(te)
    artists = {a for a in (_norm(artist), *map(_norm, split_artists(artist))) if a}
    return {f"{t}|{a}" for t in titles for a in artists}
