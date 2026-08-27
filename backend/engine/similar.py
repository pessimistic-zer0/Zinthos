"""F6 — perceptually-similar tracks: FAISS top-N → two-stage re-rank → hydrate top-k.

Re-rank weights (tuned from the PRD's originals — see the W_* constants below):
    0.60·embedding_sim + 0.10·tempo_prox + 0.10·sonic_family + 0.05·genre_match
  + 0.05·popularity + 0.05·era_prox + 0.05·region_family
The seed's own row + the N candidates are fetched in ONE track_search query (cheap numeric
work); only the final k are hydrated (expensive joins). N (CONFIG.faiss_topk) is fetched WIDE
so a mega-hit's own catalog copies don't crowd out genuinely-distinct neighbours.

embedding_sim is scaled against the POOL's percentile range, not cosine's theoretical [-1,1] —
see _normalize_sim. SONIC_SIM_NORM=legacy restores the old map for A/B comparison.

region_family / sonic_family come from track_tags (build_track_tags.py) and are OFF until
init_tags() confirms the table exists and its bit assignment still matches engine.tagfamily.
SONIC_TAG_TERMS=0 disables them for an A/B.
"""
from __future__ import annotations

from typing import Any

import numpy as np

from . import db, hydrate, tagfamily
from .config import CONFIG
from .index import VectorIndex

# Re-rank weights (sum to 1.0). Popularity is 0.20, not the PRD's 0.10: at 0.10 the pool's
# many pop=0 obscurities out-ranked recognisable tracks. Genre stays a SOFT 0.20 — that bonus
# already floats same-genre neighbours to the top, so a HARD genre filter proved redundant for
# small k and would have discarded the ~46% NULL-genre (unlabeled) neighbours. See F6 notes.
# RE-TUNED after the _normalize_sim fix: pop 0.20 → 0.05, the freed 0.15 going to sim. The old
# 0.20 was set while sim was inert (swing 0.003 vs pop's 0.124) — popularity was the only lever
# that moved, so it absorbed work that wasn't its own. With sim now swinging the full 0.45 it no
# longer needs the help, and 0.20 was leaving a visible fame-tilt in the top rows.
# GENRE 0.20 → 0.05, the freed 0.15 going to the two track_tags terms. genre_id is a GBDT
# PREDICTION (it labels Raining Blood `rock` while 1004 of its 1499 neighbours are metal, so the
# term actively fought the embedding there); region/sonic come from artist_genres, which are
# STATED FACTS about the artist. Where both exist the fact should outrank the guess. These two
# split 0.15 rather than stacking a 6th term on top, so the blend still sums to 1.0.
# ⚠ UNTUNED BY EAR — this split is reasoned, not heard. See the F6 notes before trusting it.
W_SIM, W_GENRE, W_TEMPO, W_POP, W_ERA, W_REGION, W_SONIC = 0.60, 0.05, 0.10, 0.05, 0.05, 0.05, 0.10
TEMPO_SCALE = 60.0   # BPM gap at which tempo_prox → 0
ERA_SCALE = 40.0     # year gap at which era_prox → 0

_FEAT_SQL = ("SELECT track_id, popularity, genre_id, tempo, release_year "
             "FROM track_search WHERE track_id IN ({ph})")
_TAG_SQL = ("SELECT track_id, region_mask, sonic_mask "
            "FROM track_tags WHERE track_id IN ({ph})")

# Set by init_tags() at startup. track_tags is built separately (build_track_tags.py), so the
# engine must run without it — and must REFUSE it when its bit assignment has drifted.
_TAGS_ENABLED = False


def init_tags() -> str:
    """Enable the region/sonic terms iff track_tags exists and its bits still mean what we think.

    Bit N is whatever family sits at position N in tagfamily's dicts. Reordering or inserting a
    family silently redefines all 93.7M stored masks — no error, just quietly wrong neighbours —
    so the builder persists the assignment and this refuses the terms on any mismatch. Returns a
    line for the startup log; callers should print it.
    """
    global _TAGS_ENABLED
    _TAGS_ENABLED = False
    if not CONFIG.tag_terms:
        return "track_tags terms disabled (SONIC_TAG_TERMS=0)"
    if not db.has_table("track_tags"):
        return "track_tags absent — region/sonic terms off (run backend/build_track_tags.py)"
    live = sorted([("region", i, f) for i, f in enumerate(tagfamily.REGION_RULES)]
                  + [("sonic", i, f) for i, f in enumerate(tagfamily.SONIC_RULES)])
    disk = [(r["axis"], r["bit"], r["name"])
            for r in db.query("SELECT axis, bit, name FROM track_tag_family ORDER BY axis, bit")]
    if disk != live:
        return ("track_tag_family DISAGREES with engine.tagfamily — region/sonic terms OFF. "
                "Rebuild track_tags, or restore the family dicts to their built order.")
    _TAGS_ENABLED = True
    return f"track_tags ready ({len(disk)} families, region/sonic terms on)"


def get_embedding(track_id: int) -> np.ndarray | None:
    """The seed's lossless 10-D vector from master.db (NOT the lossy fp16 index)."""
    rows = db.query("SELECT vector_blob FROM ml_10d_embeddings WHERE track_id = ?", (track_id,))
    if not rows:
        return None
    return np.frombuffer(rows[0]["vector_blob"], dtype="<f4")


def _prox(a: int | None, b: int | None, scale: float) -> float:
    if a is None or b is None:
        return 0.0
    return max(0.0, 1.0 - abs(a - b) / scale)


def _normalize_sim(cos: np.ndarray) -> np.ndarray:
    """Map the pool's cosines onto [0,1] — the scale the other four re-rank terms already use.

    A weight is not an influence: influence is weight × the term's SPREAD across the pool.
    "legacy" ((cos+1)/2) scales against cosine's THEORETICAL [-1,1], but a top-1500 pool in
    this 10-D space spans only ~0.983–1.0, so W_SIM's total swing was ~0.003 against
    popularity's ~0.124 — a 40× disadvantage. Measured over three seeds, the final ranking
    correlated with the embedding order at rho ≈ +0.05 (i.e. noise) and with popularity at
    rho ≈ +0.16…+0.53: results were effectively ordered by fame.

    "clipped" scales against the pool's OWN range, restoring rho(score, cosine) to ≈ +0.7…+0.97.
    Percentiles, not min/max: the catalog holds many copies of one song (Shape of You: 902) and
    those arrive at cos≈1.0 (fp16 rounding even puts some fractionally above it), so a plain
    min/max ruler lets one soon-to-be-deduped copy stretch the top of the scale and squeeze the
    real neighbours into its bottom ~40%. Trimmed ends clamp to 0.0/1.0 instead.
    """
    if CONFIG.sim_norm == "legacy":
        return (cos + 1.0) / 2.0
    pct = min(max(CONFIG.sim_clip_pct, 0.0), 49.0)
    lo, hi = np.percentile(cos, pct), np.percentile(cos, 100.0 - pct)
    if hi <= lo:
        # Degenerate pool (one candidate, or every copy identical): similarity carries no
        # ordering information here, so hand every candidate the same neutral score.
        return np.full(cos.shape, 0.5)
    return np.clip((cos - lo) / (hi - lo), 0.0, 1.0)


def find_similar(index: VectorIndex, track_id: int, k: int) -> list[dict[str, Any]]:
    emb = get_embedding(track_id)
    if emb is None:
        return []
    hits = [(tid, s) for tid, s in index.search(emb, CONFIG.faiss_topk) if tid != track_id]
    if not hits:
        return []

    ids = [track_id, *(tid for tid, _ in hits)]
    feats = {r["track_id"]: r
             for r in db.query(_FEAT_SQL.format(ph=db.placeholders(len(ids))), ids)}
    seed = feats.get(track_id)

    # One PK probe for the whole pool (~6-8 ms). The live track_artists ⋈ artist_genres join this
    # replaces measured 303-521 ms — see build_track_tags.py. Absent table -> empty dict -> both
    # terms score 0 for EVERY candidate, which shifts all scores by a constant and so leaves the
    # ranking untouched. No renormalization needed.
    masks: dict[int, tuple[int, int]] = {}
    if _TAGS_ENABLED:
        masks = {r["track_id"]: (r["region_mask"], r["sonic_mask"])
                 for r in db.query(_TAG_SQL.format(ph=db.placeholders(len(ids))), ids)}
    seed_r, seed_s = masks.get(track_id, (0, 0))

    # Normalizing sim is POOL-WIDE (percentiles), so the candidates have to be gathered before
    # any of them can be scored — hence the two passes over what used to be one loop.
    cand = [(tid, cos, feats[tid]) for tid, cos in hits if tid in feats]
    if not cand:
        return []
    sims = _normalize_sim(np.array([c for _, c, _ in cand], dtype=np.float64))

    scored: list[tuple[float, int]] = []
    for (tid, _cos, f), sim in zip(cand, sims):
        genre = 1.0 if (seed and f["genre_id"] is not None
                        and f["genre_id"] == seed["genre_id"]) else 0.0
        tempo = _prox(f["tempo"], seed["tempo"] if seed else None, TEMPO_SCALE)
        pop = (f["popularity"] or 0) / 100.0
        era = _prox(f["release_year"], seed["release_year"] if seed else None, ERA_SCALE)
        # ADDITIVE per axis, never conjunctive: requiring BOTH to agree returns 0 candidates for a
        # metal seed, because metal carries no region at all (measured on Metallica and Slayer).
        cr, cs = masks.get(tid, (0, 0))
        region = 1.0 if (cr & seed_r) else 0.0
        sonic = 1.0 if (cs & seed_s) else 0.0
        # float(): sim is a numpy scalar, and json.dumps can't serialize np.float64.
        scored.append((float(W_SIM*sim + W_GENRE*genre + W_TEMPO*tempo + W_POP*pop + W_ERA*era
                             + W_REGION*region + W_SONIC*sonic), tid))

    scored.sort(reverse=True)
    # The catalog has many copies of the same song — all genuine nearest neighbours but
    # useless to show — so dedupe by (title, artists). hydrate_top hydrates in ranked
    # chunks and stops at k unique records instead of paying the join for k*4 up front.
    # Pre-seed the dedupe with the SEED's own (title, artists) so copies of the seed track
    # itself don't come back as "similar" (exact-key; a decoratively-retitled copy can still
    # slip through — a textnorm-key dedupe would catch those, a documented follow-up).
    seed_rec = hydrate.hydrate_one(track_id)
    exclude = {hydrate.dupe_key(seed_rec)} if seed_rec else None
    score_by = {tid: s for s, tid in scored}
    records = hydrate.hydrate_top([tid for _, tid in scored], k,
                                  chunk=max(2 * k, 40), exclude=exclude)
    for rec in records:
        rec["score"] = round(score_by[rec["track_id"]], 4)
    return records
