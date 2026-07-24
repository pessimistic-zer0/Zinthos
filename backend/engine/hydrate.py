"""Two-tier hydration — the expensive string/joins, done ONLY for the final result set.

Cheap numeric work (filter / re-rank) happens upstream on the compact track_search table;
this turns a small list of track_ids into display-ready records (titles, artists, art).
"""
from __future__ import annotations

from typing import Any

from . import db

# group_concat with an explicit ORDER BY needs SQLite ≥ 3.44; artist_position gives credited-ish order.
_HYDRATE_SQL = """
    SELECT t.track_id, t.title, t.popularity, t.release_date, t.preview_url,
           t.duration_ms, t.is_explicit,
           al.title AS album_title, al.cover_art_url,
           group_concat(ar.name, ', ') AS artists
    FROM tracks t
    LEFT JOIN albums al        ON al.album_id = t.album_id
    LEFT JOIN track_artists ta ON ta.track_id = t.track_id
    LEFT JOIN artists ar       ON ar.artist_id = ta.artist_id
    WHERE t.track_id IN ({ph})
    GROUP BY t.track_id
"""


def hydrate(track_ids: list[int]) -> list[dict[str, Any]]:
    """Display records for the given ids, returned in the SAME order as requested."""
    if not track_ids:
        return []
    rows = db.query(_HYDRATE_SQL.format(ph=db.placeholders(len(track_ids))), track_ids)
    by_id = {r["track_id"]: dict(r) for r in rows}
    return [by_id[tid] for tid in track_ids if tid in by_id]


def hydrate_one(track_id: int) -> dict[str, Any] | None:
    out = hydrate([track_id])
    return out[0] if out else None


def dupe_key(r: dict[str, Any]) -> tuple[str, str]:
    return (str(r.get("title", "")).strip().lower(),
            str(r.get("artists", "")).strip().lower())


def dedupe(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop catalog duplicates by (title, artists), keeping the first (highest-ranked)."""
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for r in records:
        key = dupe_key(r)
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def hydrate_top(track_ids: list[int], size: int, chunk: int = 150,
                exclude: set[tuple[str, str]] | None = None) -> list[dict[str, Any]]:
    """First `size` deduped display records from a ranked candidate list, hydrating lazily.

    Dedupe needs the title/artist strings, so SOME over-hydration past `size` is inherent —
    but candidates must be hydrated in ranked chunks with an early stop, not all at once:
    the callers pass hundreds of FAISS hits (F7 recommend ≈ 800 for a real library) of
    which only `size` survive, and each hydrated row costs a 4-table join on the 162 GB DB.

    `exclude` pre-seeds the dedupe with (title, artists) keys to suppress — F6 passes the
    seed track's own key so copies of the seed don't come back as "similar".
    """
    seen: set[tuple[str, str]] = set(exclude) if exclude else set()
    out: list[dict[str, Any]] = []
    for i in range(0, len(track_ids), chunk):
        for r in hydrate(track_ids[i:i + chunk]):
            key = dupe_key(r)
            if key in seen:
                continue
            seen.add(key)
            out.append(r)
            if len(out) >= size:
                return out
    return out
