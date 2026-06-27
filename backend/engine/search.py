"""F1 semantic search — rules path (M3). LLM fallback wires in at M4.

parse → predicates → popularity-led track_search scan (early-terminating) → two-tier hydrate.
Below the coverage threshold we flag that an LLM fallback is warranted (not yet wired).
"""
from __future__ import annotations

import random
from typing import Any

from . import db, filters, hydrate, llm, rules
from .config import CONFIG

COVERAGE_THRESHOLD = 0.5
_MAX_LIMIT = 200


def _weighted_sample(rows: list[tuple[int, int]], k: int, seed: int) -> list[int]:
    """Pick k track_ids from (track_id, popularity) rows, favouring popular tracks but not
    locking to them — Efraimidis–Spirakis weighted reservoir keyed by a seeded PRNG so the
    draw is random per seed yet fully reproducible for a given (pool, seed)."""
    rng = random.Random(seed)
    keyed = ((rng.random() ** (1.0 / (pop + 1)), tid) for tid, pop in rows)
    return [tid for _, tid in sorted(keyed, reverse=True)[:k]]


def _run(predicates: list[rules.Predicate], limit: int, offset: int = 0,
         seed: int = 0) -> list[dict[str, Any]]:
    where, params = filters.build_where(predicates)
    if seed:
        # Seeded "different songs": draw an index-fast pool of the most popular matches,
        # then weighted-sample within it. Bounds work; offset is irrelevant to a sampled draw.
        pool = max(limit, CONFIG.reshuffle_pool)
        sql = (f"SELECT track_id, popularity FROM track_search WHERE {where} "
               f"ORDER BY popularity DESC LIMIT ?")
        rows = [(r["track_id"], r["popularity"]) for r in db.query(sql, [*params, pool])]
        ids = _weighted_sample(rows, limit, seed)
    else:
        sql = (f"SELECT track_id FROM track_search WHERE {where} "
               f"ORDER BY popularity DESC LIMIT ? OFFSET ?")
        ids = [r["track_id"] for r in db.query(sql, [*params, limit, offset])]
    return hydrate.dedupe(hydrate.hydrate(ids))   # within-page dedupe


def semantic_search(query: str, limit: int = 50, offset: int = 0, seed: int = 0) -> dict[str, Any]:
    limit = max(1, min(limit, _MAX_LIMIT))
    offset = max(0, offset)
    seed = max(0, seed)
    pr = rules.parse(query)

    base: dict[str, Any] = {
        "query": query,
        "coverage": round(pr.coverage, 3),
        "matched": pr.matched,
        "unmatched": pr.unmatched,
        "seed": seed,
    }

    # Below the coverage threshold, ask the LLM — but KEEP the rules' confident predicates
    # (e.g. a genre lock from "hip-hop") by merging them with the LLM's, rather than replacing.
    if pr.coverage < COVERAGE_THRESHOLD:
        llm_preds = llm.extract_filters(query)
        if llm_preds:
            merged = rules.merge(pr.predicates + llm_preds)
            return {**base, "source": "rules+llm", "filters": merged, "offset": offset,
                    "count": len(r := _run(merged, limit, offset, seed)), "results": r}

    if not pr.predicates:
        return {**base, "source": "rules", "filters": [], "offset": offset,
                "llm_fallback_recommended": not llm.available(),
                "count": 0, "results": []}

    results = _run(pr.predicates, limit, offset, seed)
    return {**base, "source": "rules", "filters": pr.predicates, "offset": offset,
            "llm_fallback_recommended": pr.coverage < COVERAGE_THRESHOLD and not llm.available(),
            "count": len(results), "results": results}
