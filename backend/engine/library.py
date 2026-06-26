"""F7 — local-library scan: identify local files in the catalog, then leverage what we
already built (embeddings, genres) to recommend and profile.

Local audio features (Source A-proprietary) are NOT reproducible from a raw file, so a "scan"
is IDENTITY matching, not acoustic analysis. The client (TUI) extracts light tags locally and
sends only small JSON; the engine resolves each to a track_id, never touching the audio.

M7.1 matches on ISRC only (exact, uses idx_tracks_isrc — no new index). Fuzzy title/artist
matching is M7.2 (a sidecar normalized-key table); the `match()` seam is shaped for it.

  scan() = match() → recommend(matched) → breakdown(matched)
"""
from __future__ import annotations

import re
from typing import Any

import numpy as np

from . import db, hydrate
from .config import CONFIG
from .index import VectorIndex
from .textnorm import candidate_keys

# Canonical 20 macro genres; list index == genre_id. Mirrors
# model_training/genre_label_encoder.joblib (authority) and build_track_search.py.
MACRO_GENRES = [
    "african", "alternative", "asian", "christian", "classical", "country", "dance",
    "electronic", "folk", "hip-hop", "jazz", "kids", "latin", "metal", "other",
    "pop", "r&b", "reggae", "rock", "soundtrack",
]

_CHUNK = 900  # stay well under SQLite's host-parameter limit for IN-clauses


def _chunks(seq: list[Any], n: int = _CHUNK):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def normalize_isrc(raw: str | None) -> str | None:
    """ISRCs are written many ways (US-UG1-25-01598); the catalog stores them bare. Fold to
    12 uppercase alphanumerics so a tag matches regardless of punctuation/case."""
    if not raw:
        return None
    s = re.sub(r"[^A-Za-z0-9]", "", raw).upper()
    return s if len(s) == 12 else None


# ── matching ──────────────────────────────────────────────────────────────────────
def _match_isrc(isrcs: set[str]) -> dict[str, int]:
    """isrc → best (most-popular) track_id, for the given set of normalized ISRCs."""
    best: dict[str, tuple[int, int]] = {}  # isrc → (popularity, track_id)
    for chunk in _chunks(list(isrcs)):
        rows = db.query(
            f"SELECT track_id, isrc, COALESCE(popularity, 0) AS pop "
            f"FROM tracks WHERE isrc IN ({db.placeholders(len(chunk))})",
            chunk,
        )
        for r in rows:
            cur = best.get(r["isrc"])
            if cur is None or r["pop"] > cur[0]:
                best[r["isrc"]] = (r["pop"], r["track_id"])
    return {k: v[1] for k, v in best.items()}


def _match_fuzzy(tracks: list[dict[str, Any]], results: list[dict[str, Any]]) -> None:
    """Fill still-unmatched results in place via the normalized-key sidecar (M7.2).

    Each unmatched file contributes several candidate keys (every credited artist × title
    variants — see textnorm.candidate_keys). We look them all up at once and, per file, take
    the most popular catalog hit across that file's candidates.
    """
    cands: list[tuple[dict[str, Any], set[str]]] = []
    all_keys: set[str] = set()
    for r in results:
        if r["track_id"] is not None:
            continue
        t = tracks[r["input"]]
        if keys := candidate_keys(t.get("title"), t.get("artist")):
            cands.append((r, keys))
            all_keys |= keys
    if not all_keys:
        return

    best: dict[str, tuple[int, int]] = {}  # key → (popularity, track_id)
    for chunk in _chunks(list(all_keys)):
        rows = db.query(
            f"SELECT norm_key, track_id, popularity FROM m.track_match "
            f"WHERE norm_key IN ({db.placeholders(len(chunk))})",
            chunk,
        )
        for row in rows:
            cur = best.get(row["norm_key"])
            if cur is None or row["popularity"] > cur[0]:
                best[row["norm_key"]] = (row["popularity"], row["track_id"])

    for r, keys in cands:
        if hit := max((best[k] for k in keys if k in best), default=None):
            r["track_id"] = hit[1]
            r["method"] = "fuzzy"


def match(tracks: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[int]]:
    """Resolve each local track to a catalog track_id. Returns (per-input results, owned ids).

    Each result: {input, track_id|None, method}. method ∈ {"isrc","fuzzy","none"}. ISRC first
    (exact); the fuzzy title/artist sidecar (if built) backfills the rest.
    """
    isrcs = {n for t in tracks if (n := normalize_isrc(t.get("isrc")))}
    by_isrc = _match_isrc(isrcs) if isrcs else {}

    results: list[dict[str, Any]] = []
    for i, t in enumerate(tracks):
        n = normalize_isrc(t.get("isrc"))
        tid = by_isrc.get(n) if n else None
        results.append({"input": i, "track_id": tid,
                        "method": "isrc" if tid is not None else "none"})

    if db.has_match():
        _match_fuzzy(tracks, results)

    owned = [r["track_id"] for r in results if r["track_id"] is not None]
    return results, owned


# ── taste-profile recommendations ───────────────────────────────────────────────────
def _embeddings(track_ids: list[int]) -> np.ndarray:
    vecs: list[np.ndarray] = []
    for chunk in _chunks(track_ids):
        rows = db.query(
            f"SELECT vector_blob FROM ml_10d_embeddings "
            f"WHERE track_id IN ({db.placeholders(len(chunk))})",
            chunk,
        )
        vecs.extend(np.frombuffer(r["vector_blob"], dtype="<f4") for r in rows)
    if not vecs:
        return np.empty((0, CONFIG.embed_dim), dtype=np.float32)
    return np.vstack(vecs)


def recommend(index: VectorIndex, owned: list[int], size: int) -> list[dict[str, Any]]:
    """Mean-pool the owned tracks' embeddings into a taste centroid, FAISS-search the catalog,
    drop tracks the user already owns, dedupe catalog copies, hydrate the top `size`."""
    embs = _embeddings(owned)
    if embs.shape[0] == 0:
        return []
    centroid = embs.mean(axis=0)
    owned_set = set(owned)
    # Over-fetch: we discard owned hits and many catalog duplicates before keeping `size`.
    k = max(CONFIG.faiss_topk, size * 4) + len(owned_set)
    hits = [(tid, s) for tid, s in index.search(centroid, k) if tid not in owned_set]
    if not hits:
        return []
    score_by = {tid: s for tid, s in hits}
    records = hydrate.dedupe(hydrate.hydrate([tid for tid, _ in hits]))[:size]
    for rec in records:
        rec["score"] = round((score_by[rec["track_id"]] + 1.0) / 2.0, 4)  # cosine → [0,1]
    return records


# ── taste breakdown ─────────────────────────────────────────────────────────────────
def _decade(year: int | None) -> str | None:
    return f"{(year // 10) * 10}s" if year else None


def breakdown(owned: list[int]) -> dict[str, list[dict[str, Any]]]:
    """Genre + era histograms over the matched library (cheap numeric pass on track_search)."""
    genres: dict[str, int] = {}
    eras: dict[str, int] = {}
    for chunk in _chunks(owned):
        rows = db.query(
            f"SELECT genre_id, release_year FROM track_search "
            f"WHERE track_id IN ({db.placeholders(len(chunk))})",
            chunk,
        )
        for r in rows:
            gid = r["genre_id"]
            if gid is not None and 0 <= gid < len(MACRO_GENRES):
                name = MACRO_GENRES[gid]
                genres[name] = genres.get(name, 0) + 1
            if (d := _decade(r["release_year"])) is not None:
                eras[d] = eras.get(d, 0) + 1
    return {
        "genres": [{"genre": g, "count": c}
                   for g, c in sorted(genres.items(), key=lambda kv: -kv[1])],
        "eras": [{"decade": d, "count": c} for d, c in sorted(eras.items())],
    }


# ── diagnostics ──────────────────────────────────────────────────────────────────────
def diagnose(tracks: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-input match report (no recommendations): how each file resolved and to what.

    For matched files we hydrate the catalog title/artists so the caller can eyeball false
    positives (e.g. a fuzzy key that landed on the wrong release)."""
    results, owned = match(tracks)
    catalog = {r["track_id"]: r for r in hydrate.hydrate(list(set(owned)))}
    methods = {"isrc": 0, "fuzzy": 0, "none": 0}
    rows: list[dict[str, Any]] = []
    for r in results:
        methods[r["method"]] = methods.get(r["method"], 0) + 1
        rec = catalog.get(r["track_id"]) if r["track_id"] is not None else None
        rows.append({
            "input": r["input"],
            "method": r["method"],
            "track_id": r["track_id"],
            "catalog_title": rec["title"] if rec else None,
            "catalog_artists": rec["artists"] if rec else None,
        })
    return {"total": len(tracks), "methods": methods, "results": rows}


# ── orchestrator ─────────────────────────────────────────────────────────────────────
def scan(index: VectorIndex, tracks: list[dict[str, Any]], size: int) -> dict[str, Any]:
    results, owned = match(tracks)
    unmatched = [
        {"title": tracks[r["input"]].get("title", ""),
         "artist": tracks[r["input"]].get("artist", "")}
        for r in results if r["track_id"] is None
    ]
    methods = {"isrc": 0, "fuzzy": 0}
    for r in results:
        if r["method"] in methods:
            methods[r["method"]] += 1
    return {
        "total": len(tracks),
        "matched": len(owned),
        "unmatched": len(unmatched),
        "methods": methods,
        "unmatched_samples": unmatched[:50],
        "breakdown": breakdown(owned),
        "recommendations": recommend(index, owned, size),
    }
