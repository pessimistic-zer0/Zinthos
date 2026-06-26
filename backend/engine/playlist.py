"""F2 — playlist generator: F1 retrieve → 70/30 popular/deep pool → transition-ordered.

Ordering minimizes the PRD transition score between consecutive tracks:
    3·tempo_diff + 2·energy_diff + 1.5·key_penalty + 1·valence_diff   (each normalized to ~[0,1])
key_penalty uses Camelot codes from track_audio_features (harmonic mixing); a greedy
nearest-neighbour walk orders the small pool (O(n²), n≈30 → trivial).
"""
from __future__ import annotations

import random
from typing import Any

from . import db, filters, hydrate, llm, rules

W_TEMPO, W_ENERGY, W_KEY, W_VALENCE = 3.0, 2.0, 1.5, 1.0
TEMPO_NORM = 250.0   # BPM span
_POOL_MULT = 5       # over-fetch factor before the 70/30 split


def _predicates(query: str) -> tuple[list[rules.Predicate], str]:
    pr = rules.parse(query)
    if pr.coverage < 0.5:
        lp = llm.extract_filters(query)
        if lp:  # keep the rules' confident predicates (e.g. genre) alongside the LLM's
            return rules.merge(pr.predicates + lp), "rules+llm"
    return pr.predicates, "rules"


def _pool(predicates: list[rules.Predicate], size: int) -> list[int]:
    where, params = filters.build_where(predicates)
    rows = db.query(
        f"SELECT track_id FROM track_search WHERE {where} ORDER BY popularity DESC LIMIT ?",
        [*params, size * _POOL_MULT])
    ids = [r["track_id"] for r in rows]
    n_pop = int(round(size * 0.7))
    popular, rest = ids[:n_pop], ids[n_pop:]
    deep = random.sample(rest, min(size - len(popular), len(rest))) if rest else []
    return popular + deep


def _camelot(code: str | None) -> tuple[int, str] | None:
    if not code or len(code) < 2 or not code[:-1].isdigit():
        return None
    return int(code[:-1]), code[-1].upper()


def _key_penalty(a: tuple[int, str] | None, b: tuple[int, str] | None) -> float:
    if a is None or b is None:
        return 0.5
    (na, la), (nb, lb) = a, b
    if na == nb and la == lb:
        return 0.0
    if na == nb:                      # relative major/minor — harmonically compatible
        return 0.25
    dist = min(abs(na - nb), 12 - abs(na - nb)) / 6.0   # distance around the Camelot wheel
    return min(dist + (0.2 if la != lb else 0.0), 1.0)


def _transition(a: dict[str, Any], b: dict[str, Any]) -> float:
    return (W_TEMPO * abs(a["tempo"] - b["tempo"]) / TEMPO_NORM
            + W_ENERGY * abs(a["energy"] - b["energy"]) / 1000
            + W_KEY * _key_penalty(a["cam"], b["cam"])
            + W_VALENCE * abs(a["valence"] - b["valence"]) / 1000)


def _order(ids: list[int]) -> list[int]:
    rows = db.query(
        "SELECT ts.track_id, ts.tempo, ts.energy, ts.valence, taf.camelot_code "
        "FROM track_search ts JOIN track_audio_features taf ON taf.track_id = ts.track_id "
        f"WHERE ts.track_id IN ({db.placeholders(len(ids))})", ids)
    feats = {r["track_id"]: {"tempo": r["tempo"] or 0, "energy": r["energy"] or 0,
                             "valence": r["valence"] or 0, "cam": _camelot(r["camelot_code"])}
             for r in rows}
    pool = [t for t in ids if t in feats]
    if not pool:
        return []
    order = [pool[0]]                 # seed with the most-popular candidate
    remaining = set(pool[1:])
    while remaining:
        cur = feats[order[-1]]
        nxt = min(remaining, key=lambda t: _transition(cur, feats[t]))
        order.append(nxt)
        remaining.discard(nxt)
    return order


def generate(query: str, size: int = 30) -> dict[str, Any]:
    size = max(1, min(size, 100))
    predicates, source = _predicates(query)
    base = {"query": query, "size": size, "source": source, "filters": predicates}
    if not predicates:
        return {**base, "count": 0, "tracks": []}
    ordered = _order(_pool(predicates, size * 2))   # 2× pool for dedupe headroom
    tracks = hydrate.dedupe(hydrate.hydrate(ordered))[:size]
    return {**base, "count": len(tracks), "tracks": tracks}
