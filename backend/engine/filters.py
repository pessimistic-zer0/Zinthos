"""Safe SQL builder for feature predicates — the single trust boundary for F1/F2.

Both the rules parser and the LLM fallback (M4) produce Predicates; everything funnels
through here, where columns and operators are whitelisted and values bound as parameters.
The LLM (or any caller) can never inject SQL — it only proposes (column, op, value) triples.
"""
from __future__ import annotations

from .rules import Predicate

ALLOWED_COLS = frozenset({
    "energy", "valence", "danceability", "acousticness", "instrumentalness",
    "speechiness", "liveness", "tempo", "loudness", "genre_id", "release_year",
    "popularity",
})
ALLOWED_OPS = frozenset({"<", ">", "<=", ">=", "="})


class InvalidFilter(ValueError):
    pass


def build_where(preds: list[Predicate]) -> tuple[str, list[int]]:
    """Return ('col op ? AND …', [params]); raises InvalidFilter on anything off-whitelist."""
    if not preds:
        raise InvalidFilter("no predicates")
    clauses: list[str] = []
    params: list[int] = []
    for col, op, val in preds:
        if col not in ALLOWED_COLS:
            raise InvalidFilter(f"column not allowed: {col!r}")
        if op not in ALLOWED_OPS:
            raise InvalidFilter(f"operator not allowed: {op!r}")
        if not isinstance(val, int):
            raise InvalidFilter(f"value must be int: {val!r}")
        clauses.append(f"{col} {op} ?")
        params.append(val)
    return " AND ".join(clauses), params
