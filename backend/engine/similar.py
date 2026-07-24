"""F6 — perceptually-similar tracks: FAISS top-N → two-stage re-rank → hydrate top-k.

Re-rank weights (tuned from the PRD's originals — see the W_* constants below):
    0.45·embedding_sim + 0.20·genre_match + 0.10·tempo_prox + 0.20·popularity + 0.05·era_prox
The seed's own row + the N candidates are fetched in ONE track_search query (cheap numeric
work); only the final k are hydrated (expensive joins). N (CONFIG.faiss_topk) is fetched WIDE
so a mega-hit's own catalog copies don't crowd out genuinely-distinct neighbours.
"""
from __future__ import annotations

from typing import Any

import numpy as np

from . import db, hydrate
from .config import CONFIG
from .index import VectorIndex

# Re-rank weights (sum to 1.0). Popularity is 0.20, not the PRD's 0.10: at 0.10 the pool's
# many pop=0 obscurities out-ranked recognisable tracks. Genre stays a SOFT 0.20 — that bonus
# already floats same-genre neighbours to the top, so a HARD genre filter proved redundant for
# small k and would have discarded the ~46% NULL-genre (unlabeled) neighbours. See F6 notes.
W_SIM, W_GENRE, W_TEMPO, W_POP, W_ERA = 0.45, 0.20, 0.10, 0.20, 0.05
TEMPO_SCALE = 60.0   # BPM gap at which tempo_prox → 0
ERA_SCALE = 40.0     # year gap at which era_prox → 0

_FEAT_SQL = ("SELECT track_id, popularity, genre_id, tempo, release_year "
             "FROM track_search WHERE track_id IN ({ph})")


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

    scored: list[tuple[float, int]] = []
    for tid, cos in hits:
        f = feats.get(tid)
        if f is None:
            continue
        sim = (cos + 1.0) / 2.0                                   # cosine [-1,1] → [0,1]
        genre = 1.0 if (seed and f["genre_id"] is not None
                        and f["genre_id"] == seed["genre_id"]) else 0.0
        tempo = _prox(f["tempo"], seed["tempo"] if seed else None, TEMPO_SCALE)
        pop = (f["popularity"] or 0) / 100.0
        era = _prox(f["release_year"], seed["release_year"] if seed else None, ERA_SCALE)
        scored.append((W_SIM*sim + W_GENRE*genre + W_TEMPO*tempo + W_POP*pop + W_ERA*era, tid))

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
