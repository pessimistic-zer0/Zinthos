"""F3 — artist detail: aggregate a name's catalog into avg features, dominant genre, top tracks."""
from __future__ import annotations

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


def artist_detail(name: str, top_k: int = 10) -> dict[str, Any] | None:
    a = _find_artist(name)
    if a is None:
        return None
    aid = a["artist_id"]

    agg = db.query(
        "SELECT COUNT(*) n, AVG(energy) e, AVG(valence) v, AVG(danceability) d, "
        "AVG(acousticness) ac, AVG(tempo) t FROM track_search ts "
        "JOIN track_artists ta ON ta.track_id = ts.track_id WHERE ta.artist_id = ?", (aid,))[0]

    dom = db.query(
        "SELECT genre_id FROM track_search ts JOIN track_artists ta ON ta.track_id = ts.track_id "
        "WHERE ta.artist_id = ? AND genre_id IS NOT NULL "
        "GROUP BY genre_id ORDER BY COUNT(*) DESC LIMIT 1", (aid,))

    top_ids = [r["track_id"] for r in db.query(
        "SELECT ts.track_id FROM track_search ts JOIN track_artists ta ON ta.track_id = ts.track_id "
        "WHERE ta.artist_id = ? ORDER BY ts.popularity DESC LIMIT ?", (aid, top_k))]

    n = agg["n"] or 0
    feats = None
    if n:
        feats = {
            "energy": round((agg["e"] or 0) / 1000, 3),
            "valence": round((agg["v"] or 0) / 1000, 3),
            "danceability": round((agg["d"] or 0) / 1000, 3),
            "acousticness": round((agg["ac"] or 0) / 1000, 3),
            "tempo": round(agg["t"] or 0),
        }
    return {
        "artist": a["name"],
        "popularity": a["popularity"],
        "followers": a["followers_total"],
        "track_count": n,
        "dominant_genre": _genre_name(dom[0]["genre_id"]) if dom else None,
        "avg_features": feats,
        "top_tracks": hydrate.hydrate(top_ids),
    }
