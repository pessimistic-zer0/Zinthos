"""Resolve a typed song NAME → catalog candidates: the front door to F6 similar.

The similarity engine speaks track_ids; users type names. This bridges the two. It reuses the
SAME fuzzy machinery as the F7 library scan — `textnorm.candidate_keys` + the `track_match.db`
sidecar (ATTACHed as `m`) — but where F7 collapses to the single most-popular match, this
returns a ranked, deduped candidate LIST so the client can show a pick-list and let the user
disambiguate (e.g. Bohemian Rhapsody studio vs. Live Aid).

  candidate_keys(title, artist) → m.track_match lookup → popularity order → cap → hydrate → dedupe

LOAD-BEARING: keys come from textnorm — the exact module build_track_match.py used to write the
sidecar. Reimplementing normalization here would silently match nothing.
"""
from __future__ import annotations

from typing import Any

from . import db, hydrate
from .textnorm import candidate_keys

# Pre-hydrate fetch cap: enough copies to survive dedupe into a short pick-list, but bounded so a
# mega-hit (Shape of You — 902 copies) never hydrates hundreds of near-duplicate rows.
_FETCH_CAP = 60


def resolve_by_name(title: str, artist: str, limit: int = 10) -> dict[str, Any]:
    """Candidate catalog tracks for a typed (title, artist), most-popular first, deduped.

    Each candidate is a full hydrate record carrying `track_id` — the token the client then
    feeds to GET /search/similar/{track_id}. Empty list = no match (or missing artist).
    """
    title, artist = (title or "").strip(), (artist or "").strip()
    keys = candidate_keys(title, artist)
    if not keys:
        # candidate_keys needs BOTH sides (textnorm folds each to a non-empty key). Option-1:
        # we require the artist, so signal the client to ask for it rather than guessing.
        return {"title": title, "artist": artist, "count": 0, "candidates": [],
                "need_artist": not artist}
    if not db.has_match():
        return {"title": title, "artist": artist, "count": 0, "candidates": [],
                "error": "name resolution unavailable (track_match.db not built)"}

    # One indexed lookup across every candidate key; popularity-order + cap bounds the hydration.
    rows = db.query(
        f"SELECT track_id, popularity FROM m.track_match "
        f"WHERE norm_key IN ({db.placeholders(len(keys))}) ORDER BY popularity DESC LIMIT ?",
        [*keys, _FETCH_CAP],
    )
    # hydrate preserves id order (popularity-desc); dedupe collapses reissues/remasters by
    # (title, artists), keeping the highest-popularity copy. Then trim to the pick-list size.
    candidates = hydrate.dedupe(hydrate.hydrate([r["track_id"] for r in rows]))[:limit]
    return {"title": title, "artist": artist, "count": len(candidates), "candidates": candidates}
