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

from . import db, hydrate, similar
from .config import CONFIG
from .index import VectorIndex
from .textnorm import candidate_keys, fold

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
def _embeddings(index: VectorIndex, track_ids: list[int]) -> np.ndarray:
    """Vectors for the matched library, from wherever this deployment keeps them.

    Goes through similar.get_embedding rather than querying ml_10d_embeddings directly, so a
    compacted demo slice — which drops that table and leans on the exact index instead — takes
    the same path here as it does for F6. Missing vectors are skipped, not zero-filled: a zero
    row would drag the taste centroid toward the origin.
    """
    if similar.embeddings_from_table():
        vecs: list[np.ndarray] = []
        for chunk in _chunks(track_ids):
            rows = db.query(
                f"SELECT vector_blob FROM ml_10d_embeddings "
                f"WHERE track_id IN ({db.placeholders(len(chunk))})",
                chunk,
            )
            vecs.extend(np.frombuffer(r["vector_blob"], dtype="<f4") for r in rows)
    else:
        vecs = [v for t in track_ids if (v := index.reconstruct(t)) is not None]
    if not vecs:
        return np.empty((0, CONFIG.embed_dim), dtype=np.float32)
    return np.vstack(vecs)


# F7 re-rank. Two terms, not F6's seven: there is no single seed track here, so tempo/era
# proximity and a seed's region/sonic family have nothing to be measured against.
#
# Measured on a five-track library (Teardrop / Blinding Lights / Midnight City / Tum Hi Ho /
# Raining Blood) against the full 254.8M index. W_POP is 0.15, not more, because the pool it
# now scores is already made of recognisable music — the earlier need for a heavy popularity
# thumb was a symptom of querying the wrong point, not of the ranking.
W_SIM, W_POP = 0.85, 0.15

# Per-seed retrieval, bounded. A 726-file library would otherwise be 726 FAISS calls.
_MAX_SEEDS = 24
_PER_SEED_K = 300
# Extra records hydrated so the owned-title filter below can drop some and still fill `size`.
_TITLE_SLACK = 10

_POP_SQL = "SELECT track_id, popularity FROM track_search WHERE track_id IN ({ph})"


def _seeds(owned: list[int], limit: int) -> list[int]:
    """Up to `limit` owned tracks, evenly spaced through the library. Deterministic."""
    if len(owned) <= limit:
        return owned
    step = len(owned) / limit
    return [owned[int(i * step)] for i in range(limit)]


def recommend(index: VectorIndex, owned: list[int], size: int) -> list[dict[str, Any]]:
    """Neighbours of the tracks you own, merged and re-ranked.

    WHY NOT A CENTROID (it was one, and the centroid was the bug)
      The old version mean-pooled every owned embedding into one vector and searched from
      that. Averaging a diverse library produces a point that represents none of it —
      measured on the five-track library above, the centroid sat at cosine 0.32 from
      Teardrop and 0.47 from Tum Hi Ho. Its neighbourhood was correspondingly nowhere:
      1,500 candidates of which 74% had popularity 0 and eight cleared 40, so the returned
      "recommendations" were things like "Christ the Almighty vs. Diablo the Perverse".
      No ranking weight fixes that, because what is being ranked never contained anything
      worth surfacing — at W_POP 0.75 the median popularity of the top 10 still only reached
      38, and the similarity term had been spent buying it.

      Searching from each owned track instead asks the question the feature actually
      promises: what sits next to the things you already have. Same pool size, more than
      twice the recognisable candidates (18 at popularity >= 40 against 8).

      The catch that comes with it: a track's nearest neighbours are its own catalogue
      copies, so the first attempt handed back the user's own library. `owned` holds exact
      track_ids, which does not cover the other 400 pressings of Blinding Lights — the
      hydrate-time dedupe does, which is why every owned record is passed as `exclude`.
    """
    if not owned:
        return []
    owned_set = set(owned)
    pool: dict[int, float] = {}
    for tid in _seeds(owned, _MAX_SEEDS):
        vec = similar.get_embedding(index, tid)
        if vec is None:
            continue
        # Retrieve INSIDE each seed's region/sonic family, exactly as F6 does. Without it a
        # Bollywood library came back Japanese and Korean: the 13 audio features do not
        # separate those, and no amount of re-ranking recovers what retrieval never returned.
        # Falls through to an unfiltered search when the seed carries no family.
        sel, _axis = similar.seed_selector(tid)
        hits = index.search(vec, _PER_SEED_K, sel)
        if sel is not None and len(hits) < _PER_SEED_K // 4:
            hits = index.search(vec, _PER_SEED_K)   # family too thin around this seed
        for t, s in hits:
            if t not in owned_set and s > pool.get(t, -1.0):
                pool[t] = s
    if not pool:
        return []

    # One cheap numeric probe over the whole pool before anything is hydrated — the same
    # two-tier discipline F6 follows, and the reason this costs ~6-8 ms rather than a join.
    items = list(pool.items())
    pops: dict[int, int] = {}
    for chunk in _chunks([t for t, _ in items]):
        for r in db.query(_POP_SQL.format(ph=db.placeholders(len(chunk))), chunk):
            pops[r["track_id"]] = r["popularity"] or 0

    # Percentile-clipped against the POOL's own range, never (cos+1)/2 against the theoretical
    # [-1,1]: that map gave the similarity term a swing of ~0.003 and made any blend a
    # popularity sort in disguise. See similar.normalize_sim.
    sims = similar.normalize_sim(np.array([s for _, s in items], dtype=np.float64))
    scored = sorted(
        ((float(W_SIM * sim + W_POP * (pops.get(t, 0) / 100.0)), t)
         for (t, _), sim in zip(items, sims)),
        reverse=True,
    )
    score_by = {t: sc for sc, t in scored}

    # hydrate_top stops as soon as `size` deduped records exist, and `exclude` pre-seeds that
    # dedupe with the user's own records so no copy of anything they own comes back.
    owned_recs = hydrate.hydrate(owned)
    records = hydrate.hydrate_top([t for _, t in scored], size + _TITLE_SLACK,
                                  exclude=owned_recs)

    # Then a STRICTER pass than hydrate's, on title alone. hydrate._Seen needs the credits to
    # overlap before it calls two records the same song, which is right for a result list and
    # wrong here: "Tum He Ho" (a misspelling the fold cannot equate) and "Kal Ho Naa Ho"
    # credited to the actor rather than the singer both came back as recommendations to
    # someone who owns those songs. Recommending a track you already have is a worse failure
    # than dropping a genuine namesake, so for F7 the title is enough.
    owned_titles = {fold(str(r.get("title") or "")) for r in owned_recs}
    records = [r for r in records
               if fold(str(r.get("title") or "")) not in owned_titles][:size]
    for rec in records:
        rec["score"] = round(score_by[rec["track_id"]], 4)
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
