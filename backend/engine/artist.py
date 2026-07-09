"""F3 — artist detail: aggregate a name's catalog into avg features, dominant genre, top tracks."""
from __future__ import annotations

import heapq
import threading
from typing import Any

from . import db, hydrate

_genre_names: dict[int, str] = {}
_genre_lock = threading.Lock()


def _genre_name(gid: int | None) -> str | None:
    if gid is None:
        return None
    if not _genre_names:
        with _genre_lock:
            if not _genre_names:
                for r in db.query("SELECT genre_id, name FROM genres"):
                    _genre_names[r["genre_id"]] = r["name"]
    return _genre_names.get(gid)


def _find_artist(name: str) -> dict[str, Any] | None:
    cols = "artist_id, name, popularity, followers_total"
    rows = db.query(f"SELECT {cols} FROM artists WHERE name = ? ORDER BY followers_total DESC LIMIT 1",
                    (name,))
    if not rows:  # case-insensitive fallback (rare; no index, but bounded by LIMIT)
        rows = db.query(f"SELECT {cols} FROM artists WHERE name = ? COLLATE NOCASE LIMIT 1", (name,))
    return dict(rows[0]) if rows else None


_AVG_COLS = ("energy", "valence", "danceability", "acousticness", "tempo")


def artist_detail(name: str, top_k: int = 10) -> dict[str, Any] | None:
    a = _find_artist(name)
    if a is None:
        return None
    aid = a["artist_id"]

    # ONE pass over the artist's catalog. Averages / dominant genre / top tracks used to be
    # three separate track_artists ⋈ track_search traversals — 3× the random PK probes for
    # the same row set; the per-row probe dominates, so fetch once and aggregate in Python.
    # Per-column NULL counts mirror SQL AVG() semantics (NULLs excluded, not zeroed).
    rows = db.query(
        "SELECT ts.track_id, ts.popularity, ts.genre_id, ts.energy, ts.valence, "
        "ts.danceability, ts.acousticness, ts.tempo "
        "FROM track_search ts JOIN track_artists ta ON ta.track_id = ts.track_id "
        "WHERE ta.artist_id = ?", (aid,))

    n = len(rows)
    feats = None
    dom_gid: int | None = None
    top_ids: list[int] = []
    if n:
        sums = dict.fromkeys(_AVG_COLS, 0.0)
        nnull = dict.fromkeys(_AVG_COLS, 0)
        genre_counts: dict[int, int] = {}
        for r in rows:
            for c in _AVG_COLS:
                v = r[c]
                if v is not None:
                    sums[c] += v
                    nnull[c] += 1
            gid = r["genre_id"]
            if gid is not None:
                genre_counts[gid] = genre_counts.get(gid, 0) + 1

        def avg(c: str) -> float:
            return sums[c] / nnull[c] if nnull[c] else 0.0

        feats = {
            "energy": round(avg("energy") / 1000, 3),
            "valence": round(avg("valence") / 1000, 3),
            "danceability": round(avg("danceability") / 1000, 3),
            "acousticness": round(avg("acousticness") / 1000, 3),
            "tempo": round(avg("tempo")),
        }
        if genre_counts:
            dom_gid = max(genre_counts, key=lambda g: genre_counts[g])
        top_ids = [r["track_id"] for r in
                   heapq.nlargest(top_k, rows, key=lambda r: r["popularity"] or 0)]

    return {
        "artist": a["name"],
        "popularity": a["popularity"],
        "followers": a["followers_total"],
        "track_count": n,
        "dominant_genre": _genre_name(dom_gid),
        "avg_features": feats,
        "top_tracks": hydrate.hydrate(top_ids),
    }
