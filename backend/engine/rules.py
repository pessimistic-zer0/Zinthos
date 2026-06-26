"""F1 rules parser — natural-language words → track_search feature predicates.

A closed, hardcoded vocabulary (the fast path). Words map to predicates on the scaled-int
columns (features 0–1000, tempo BPM, loudness dB, genre_id 0–19). Output is the SAME
Predicate format the LLM fallback (M4) will emit, so both feed one SQL builder.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# (column, op, value) — op ∈ {"<", ">", "="}; value already on the track_search scale.
Predicate = tuple[str, str, int]

# ── mood / descriptor vocabulary ─────────────────────────────────────────────────
RULES: dict[str, list[Predicate]] = {
    "sad": [("valence", "<", 300), ("energy", "<", 500)],
    "melancholy": [("valence", "<", 300), ("energy", "<", 500)],
    "depressing": [("valence", "<", 250), ("energy", "<", 450)],
    "happy": [("valence", ">", 600)],
    "cheerful": [("valence", ">", 600)],
    "joyful": [("valence", ">", 650)],
    "energetic": [("energy", ">", 700)],
    "hype": [("energy", ">", 750)],
    "chill": [("energy", "<", 400)],
    "relaxing": [("energy", "<", 400)],
    "calm": [("energy", "<", 350)],
    "aggressive": [("energy", ">", 800), ("loudness", ">", -5)],
    "intense": [("energy", ">", 800), ("loudness", ">", -5)],
    "dance": [("danceability", ">", 700), ("energy", ">", 600)],
    "party": [("danceability", ">", 700), ("energy", ">", 600)],
    "danceable": [("danceability", ">", 700)],
    "upbeat": [("valence", ">", 600), ("energy", ">", 600), ("danceability", ">", 600)],
    "dark": [("valence", "<", 400), ("energy", "<", 600)],
    "moody": [("valence", "<", 400), ("energy", "<", 600)],
    "mellow": [("energy", "<", 400), ("loudness", "<", -8)],
    "soft": [("energy", "<", 400), ("loudness", "<", -8)],
    "acoustic": [("acousticness", ">", 600)],
    "lofi": [("acousticness", ">", 500), ("energy", "<", 400)],
    "instrumental": [("instrumentalness", ">", 500)],
    "studying": [("instrumentalness", ">", 500), ("energy", "<", 500), ("speechiness", "<", 100)],
    "study": [("instrumentalness", ">", 500), ("energy", "<", 500), ("speechiness", "<", 100)],
    "focus": [("instrumentalness", ">", 500), ("energy", "<", 500), ("speechiness", "<", 100)],
    "workout": [("energy", ">", 800), ("tempo", ">", 120), ("danceability", ">", 600)],
    "gym": [("energy", ">", 800), ("tempo", ">", 120), ("danceability", ">", 600)],
    "sleep": [("energy", "<", 200), ("instrumentalness", ">", 600), ("acousticness", ">", 500)],
    "ambient": [("energy", "<", 250), ("instrumentalness", ">", 600)],
    "live": [("liveness", ">", 800)],
    "fast": [("tempo", ">", 140)],
    "slow": [("tempo", "<", 80)],
    "loud": [("loudness", ">", -5)],
    "quiet": [("loudness", "<", -15)],
}

# ── genre vocabulary (name + aliases → genre_id 0–19; mirrors MACRO_GENRES) ───────
GENRE_WORDS: dict[str, int] = {
    "african": 0, "alternative": 1, "indie": 1, "asian": 2,
    "christian": 3, "gospel": 3, "classical": 4, "country": 5,
    "electronic": 7, "edm": 7, "electro": 7, "folk": 8,
    "hiphop": 9, "rap": 9, "jazz": 10, "kids": 11, "latin": 12,
    "metal": 13, "pop": 15, "rnb": 16, "rb": 16, "soul": 16,
    "reggae": 17, "rock": 18, "soundtrack": 19, "score": 19,
}  # note: "dance" intentionally omitted (handled as a mood above, not a genre)

STOPWORDS = {
    "a", "an", "the", "for", "to", "of", "and", "or", "with", "in", "on", "some",
    "me", "my", "i", "song", "songs", "music", "track", "tracks", "tune", "tunes",
    "playlist", "something", "stuff", "vibe", "vibes", "feel", "like", "want",
}


@dataclass
class ParseResult:
    predicates: list[Predicate]
    coverage: float
    matched: list[str] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)


def tokenize(q: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", q.lower())


def _lookup(term: str) -> list[Predicate] | None:
    if term in RULES:
        return RULES[term]
    if term in GENRE_WORDS:
        return [("genre_id", "=", GENRE_WORDS[term])]
    return None


def merge(preds: list[Predicate]) -> list[Predicate]:
    """Combine same-column predicates: tightest bound wins; first genre/year wins."""
    lt: dict[str, int] = {}
    gt: dict[str, int] = {}
    eq: dict[str, int] = {}
    for col, op, val in preds:
        if op == "<":
            lt[col] = min(lt.get(col, val), val)
        elif op == ">":
            gt[col] = max(gt.get(col, val), val)
        else:
            eq.setdefault(col, val)
    return ([(c, "<", v) for c, v in lt.items()]
            + [(c, ">", v) for c, v in gt.items()]
            + [(c, "=", v) for c, v in eq.items()])


def parse(query: str) -> ParseResult:
    toks = tokenize(query)
    preds: list[Predicate] = []
    matched: list[str] = []

    i = 0
    while i < len(toks):
        bigram = toks[i] + toks[i + 1] if i + 1 < len(toks) else None
        hit = _lookup(bigram) if bigram else None
        if hit is not None:
            preds += hit
            matched += [toks[i], toks[i + 1]]
            i += 2
            continue
        hit = _lookup(toks[i])
        if hit is not None:
            preds += hit
            matched.append(toks[i])
        i += 1

    meaningful = [t for t in toks if t not in STOPWORDS]
    matched_meaningful = [t for t in matched if t not in STOPWORDS]
    coverage = len(matched_meaningful) / len(meaningful) if meaningful else 0.0
    unmatched = [t for t in meaningful if t not in matched]
    return ParseResult(merge(preds), coverage, matched_meaningful, unmatched)
