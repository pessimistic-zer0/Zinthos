"""F6 — perceptually-similar tracks: FAISS top-N → two-stage re-rank → hydrate top-k.

Re-rank weights (tuned from the PRD's originals — see the W_* constants below):
    0.50·embedding_sim + 0.10·tempo_prox + 0.15·sonic_family + 0.05·genre_match
  + 0.05·popularity + 0.05·era_prox + 0.10·region_family
  where retrieval runs INSIDE the seed's family bitmap when one exists (engine.bitmaps,
  CONFIG.tag_filter), then (a) catalog copies collapse (equal-score runs) and (b) the pool is
  GATED to the seed's region/sonic family when enough candidates share it (CONFIG.tag_gate).
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

from . import bitmaps, db, hydrate, tagfamily
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
# RE-TUNED again (2026-09) after measuring the pool: on Chura Ke Dil Mera the 1500 candidates
# span cosine 0.991-1.000 and their region purity is FLAT across rank (37/30/29/26% by quartile),
# so sim's 0.60 was mostly amplifying noise while the one term with real signal — region, a
# stated fact about the artist — sat at 0.05. 0.10 moves from sim to the two tag terms. With
# the tag GATE on (CONFIG.tag_gate) region is constant inside a gated pool and its weight only
# matters for seeds whose gate did not fire (no tags, or a thin match).
W_SIM, W_GENRE, W_TEMPO, W_POP, W_ERA, W_REGION, W_SONIC = 0.50, 0.05, 0.10, 0.05, 0.05, 0.10, 0.15
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


def _collapse_copies(hits: list[tuple[int, float]]) -> list[tuple[int, float]]:
    """One candidate per RECORDING before anything is scored — from the scores alone.

    Catalog copies of one recording carry byte-identical audio features, hence identical
    vectors, hence bit-identical inner products with the seed: they arrive as runs of equal
    scores. Agúzate held 78 of Chura Ke Dil Mera's 1500 slots that way, the seed's own copies
    17 more, and only 1147 distinct songs were left to compete. Keeping the first of each equal-
    score run hands those slots back — with NO database probe. (An ISRC probe on `tracks` did
    the same job at 300-500 ms per cold pool, because that table's rows are wide and its pages
    are never warm for a fresh pool; the scores are already in hand.)

    Two DIFFERENT recordings can tie on a float32 score by coincidence — a few per 1500 — and
    one of them is then dropped. That loses a random 0.5% of the pool, which is harmless;
    the ISRC/title dedupe at hydration is the ground-truth pass.
    """
    out: list[tuple[int, float]] = []
    last: float | None = None
    for tid, cos in hits:
        if cos == last:
            continue
        last = cos
        out.append((tid, cos))
    return out


def _gate(cand: list[tuple[int, float, Any]], masks: dict[int, tuple[int, int]],
          seed_r: int, seed_s: int, k: int) -> tuple[list[tuple[int, float, Any]], str]:
    """Cut the pool to candidates that share the seed's region family, then sonic family — each
    axis independently, each only when the seed has one and >= max(tag_gate_min, 2k) agree.

    A bonus can't do this job: the embedding leaves the intruders interleaved with the real
    neighbours at cosine gaps of 1e-4, and a 0.05-0.15 additive term only reorders within that
    noise. Removing them is the only move that makes the top-10 mostly Hindi film music for a
    Hindi film seed. The floor keeps a seed whose family is thin in its neighbourhood (a Bollywood
    track surrounded by 30 South Asian tracks and 1470 others) from being cut to 30 and then
    deduped to 5. Returns the pool and a short tag for the log/response ("region+sonic", "", …).
    """
    floor = max(CONFIG.tag_gate_min, 2 * k)
    applied: list[str] = []
    for axis, seed_bits in (("region", seed_r), ("sonic", seed_s)):
        if not seed_bits:
            continue
        i = 0 if axis == "region" else 1
        kept = [c for c in cand if masks.get(c[0], (0, 0))[i] & seed_bits]
        if len(kept) >= floor:
            cand = kept
            applied.append(axis)
    return cand, "+".join(applied)


def _retrieve(index: VectorIndex, emb: np.ndarray, track_id: int,
              sel: Any | None = None) -> list[tuple[int, float]]:
    """FAISS top-N (optionally inside a family bitmap), collapsed to one hit per recording —
    widened once if the collapse ate it.

    A mega-hit's own copies fill the pool: Shape of You has 902 catalog copies, so a 1500-wide
    search collapses to a few hundred distinct recordings and the tag gate has nothing to hold
    on to. When fewer than half the slots survive, search again 4x wider (still single-digit ms
    at 10-D), collapse that, and keep the first n survivors. One retry, not a loop: the
    second pool is bounded and this path only fires for seeds with hundreds of copies.
    """
    n = CONFIG.faiss_topk
    hits = _collapse_copies([(t, s) for t, s in index.search(emb, n, sel) if t != track_id])
    if len(hits) < n // 2:
        hits = _collapse_copies([(t, s) for t, s in index.search(emb, 4 * n, sel) if t != track_id])
    # Cap back to n: the point of widening is n DISTINCT recordings, and every id past this
    # line costs a cold track_search/track_tags probe (6000 ids measured ~1.2 s on this box).
    return hits[:n]


def _seed_selector(track_id: int) -> tuple[Any | None, str]:
    """The bitmap selector for the seed's own family — region if it has one, else sonic.

    Region first because it is the axis the embedding cannot see at all (the 13 features put
    Tu Jo Mila among Thai pop; they do put Raining Blood among metal). One axis, not the AND:
    the intersection of two bitmaps can be thin, and the post-gate still applies the other axis
    when enough candidates carry it. Returns (selector, "region"|"sonic"|"").
    """
    if not (bitmaps.ENABLED and _TAGS_ENABLED):
        return None, ""
    row = db.query("SELECT region_mask, sonic_mask FROM track_tags WHERE track_id = ?", (track_id,))
    if not row:
        return None, ""
    for axis, mask in (("region", row[0]["region_mask"]), ("sonic", row[0]["sonic_mask"])):
        sel = bitmaps.selector(axis, mask)
        if sel is not None:
            return sel, axis
    return None, ""


def find_similar(index: VectorIndex, track_id: int, k: int,
                 info: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Top-k similar records. `info`, if given, receives {"filter": "region"|"sonic"|"" (which
    bitmap retrieval ran inside), "gate": "region+sonic"|"region"|"sonic"|"", "pool": <candidates
    after collapse+gate>} for the response/log."""
    emb = get_embedding(track_id)
    if emb is None:
        return []
    sel, filtered = _seed_selector(track_id)
    hits = _retrieve(index, emb, track_id, sel)
    if sel is not None and len(hits) < max(CONFIG.tag_gate_min, 2 * k):
        # Family too thin around this seed for the filtered scan to fill a pool (a tag carried
        # by a handful of artists): fall back to the plain neighbourhood.
        hits, filtered = _retrieve(index, emb, track_id), ""
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

    # Normalizing sim is POOL-WIDE (percentiles), so the candidates have to be gathered — and
    # GATED — before any of them can be scored: the percentile ruler must be the gated pool's.
    cand = [(tid, cos, feats[tid]) for tid, cos in hits if tid in feats]
    gated = ""
    if _TAGS_ENABLED and CONFIG.tag_gate:
        cand, gated = _gate(cand, masks, seed_r, seed_s, k)
    if info is not None:
        info.update(filter=filtered, gate=gated, pool=len(cand))
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
    # The score collapse above catches identical-vector copies; the ISRC / folded-title /
    # artist-overlap dedupe here is the ground-truth pass on the rows being hydrated anyway. hydrate_top hydrates in ranked
    # chunks and stops at k unique records instead of paying the join for k*4 up front.
    # Pre-seed the dedupe with the SEED's own keys so copies of the seed never come back.
    seed_rec = hydrate.hydrate_one(track_id)
    exclude = [seed_rec] if seed_rec else None
    score_by = {tid: s for s, tid in scored}
    records = hydrate.hydrate_top([tid for _, tid in scored], k,
                                  chunk=max(2 * k, 40), exclude=exclude)
    for rec in records:
        rec["score"] = round(score_by[rec["track_id"]], 4)
    return records
