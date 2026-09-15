"""Engine configuration — all paths/knobs in one place, env-overridable.

Local-first but host-agnostic: nothing here hardcodes localhost into client-facing
behavior beyond defaults; override via env to point at a hosted box later.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_dotenv() -> None:
    """Load backend/.env (then repo .env) into os.environ — without overriding real env vars.

    Must run BEFORE Config's fields read env. Keeps LLM keys out of the codebase while letting
    `python -m engine.main` pick them up automatically (no manual `source` needed).
    """
    for path in (os.path.join(_ROOT, "backend", ".env"), os.path.join(_ROOT, ".env")):
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Config:
    # ── data artifacts ──────────────────────────────────────────────────────────
    db_path: str = _env("SONIC_DB", os.path.join(_ROOT, "master.db"))
    index_path: str = _env("SONIC_INDEX", os.path.join(_ROOT, "model_training", "embeddings.faiss"))
    # Optional fuzzy-match sidecar (M7.2). Attached read-only iff the file exists.
    match_db_path: str = _env("SONIC_MATCH_DB", os.path.join(_ROOT, "track_match.db"))
    embed_dim: int = 10

    # ── retrieval knobs ─────────────────────────────────────────────────────────
    nprobe: int = int(_env("SONIC_NPROBE", "64"))        # recall@10 plateaus at 64
    # Over-fetch WIDE: a mega-hit (e.g. Shape of You — 902 catalog copies) fills the nearest
    # neighbours entirely with its OWN copies, so a narrow pool dedupes down to ~1 result. A
    # ~1500-wide pool lets genuinely-distinct neighbours survive past the copies; at 10-D FAISS
    # search over this many is still single-digit ms. Env-overridable for tuning.
    faiss_topk: int = int(_env("SONIC_FAISS_TOPK", "1500"))  # candidates before re-rank
    default_k: int = 20                                   # results returned
    # F6 re-rank: how a candidate's cosine is mapped onto the 0–1 scale the other four terms
    # already use. "clipped" scales against the POOL's own percentile range; "legacy" is the
    # old (cos+1)/2 map of the THEORETICAL [-1,1] range, kept only for A/B-ing by ear. See
    # similar._normalize_sim for why legacy left the similarity term inert.
    sim_norm: str = _env("SONIC_SIM_NORM", "clipped")     # "clipped" | "legacy"
    # Percentile trimmed off EACH end before scaling (clipped only). Duplicate catalog copies
    # arrive at cos≈1.0 and with a plain min/max would set the ruler's top for the whole pool.
    sim_clip_pct: float = float(_env("SONIC_SIM_CLIP_PCT", "1.0"))
    # F6 region/sonic terms, sourced from the separately-built track_tags table. Set 0 to A/B the
    # re-rank without them; they also switch themselves off if the table is missing or its bit
    # assignment has drifted from engine.tagfamily (see similar.init_tags).
    tag_terms: bool = _env("SONIC_TAG_TERMS", "1") not in ("0", "false", "False", "")
    # F6 tag GATE: when the seed carries a region (or sonic) family and at least tag_gate_min
    # candidates share it, the pool is CUT to those candidates before scoring, instead of merely
    # giving them a bonus. Measured on Chura Ke Dil Mera: the top-1500 pool spans cosine
    # 0.991-1.000 with region purity flat across rank (37/30/29/26%), so the embedding cannot
    # push the jazz/salsa intruders out by itself — the artist-genre fact has to. Each axis gates
    # independently and only when it applies (metal has no region; the floor keeps a thin match
    # from collapsing the pool to a handful). The floor is LOW on purpose: a modern Bollywood
    # ballad (Tum Hi Ho) has only 44 South Asian candidates in its 1300-wide pool and a rock
    # seed (Bohemian Rhapsody) 60 rock ones — at 100 neither gated and the top-10 stayed mixed.
    # similar._gate also never lets it drop below 2·k. Set 0 to A/B the additive-only blend.
    tag_gate: bool = _env("SONIC_TAG_GATE", "1") not in ("0", "false", "False", "")
    tag_gate_min: int = int(_env("SONIC_TAG_GATE_MIN", "40"))
    # F6 tag FILTER: retrieve INSIDE the seed's family via a FAISS ID bitmap (engine.bitmaps,
    # built by backend/build_tag_bitmaps.py). The gate above can only keep what retrieval
    # returned, and for many Bollywood seeds that is nothing: Tu Jo Mila's pool held 7 South
    # Asian tracks in 1,195. Filtering the IVF scan draws all 1,500 from the family instead.
    # Region wins when the seed has one, else sonic. Off (or files absent) → plain search.
    tag_filter: bool = _env("SONIC_TAG_FILTER", "1") not in ("0", "false", "False", "")
    bitmap_dir: str = _env("SONIC_BITMAP_DIR", os.path.join(_ROOT, "model_training", "tag_bitmaps"))
    # F1 reshuffle: a seeded "different songs" draw samples within the top-N most popular
    # matches (index-fast pool) — bounds the work so a broad mood query doesn't full-scan/sort.
    reshuffle_pool: int = int(_env("SONIC_RESHUFFLE_POOL", "600"))

    # ── sqlite tuning (per read-only connection) ────────────────────────────────
    # mmap is file-backed/reclaimable (won't swap). The page cache is HEAP (anonymous →
    # swappable) and is opened PER threadpool thread, so keep it small — the big mmap window
    # already serves reads. On the 15 GB box this is the main lever against swap pressure.
    sqlite_mmap_bytes: int = int(_env("SONIC_SQLITE_MMAP", "30_000_000_000".replace("_", "")))
    sqlite_cache_kb: int = int(_env("SONIC_SQLITE_CACHE_KB", "49152"))  # ~48 MB / connection

    # ── server ──────────────────────────────────────────────────────────────────
    host: str = _env("SONIC_HOST", "127.0.0.1")
    port: int = int(_env("SONIC_PORT", "3000"))
    # Sync-handler threadpool size. Each worker thread opens its own read-only SQLite
    # connection (~sqlite_cache_kb of heap), so anyio's default of 40 threads could pin
    # ~2 GB against the ~9 GB swap-thrash ceiling; 8 × 48 MB ≈ 384 MB worst case.
    worker_threads: int = int(_env("SONIC_THREADS", "8"))
    cors_origins: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            o for o in _env("SONIC_CORS", "http://localhost:5173,http://127.0.0.1:5173").split(",") if o
        )
    )
    idle_shutdown_secs: int = int(_env("SONIC_IDLE_SECS", "900"))  # 0 disables; default 15 min

    # ── F1 LLM fallback (provider-agnostic; empty provider = disabled) ───────────
    llm_provider: str = _env("SONIC_LLM_PROVIDER", "")     # "anthropic" | "gemini" | "openai" | ""
    llm_model: str = _env("SONIC_LLM_MODEL", "")           # per-provider default if empty
    llm_max_tokens: int = int(_env("SONIC_LLM_MAX_TOKENS", "400"))

    def warmup_queries(self) -> int:
        return int(_env("SONIC_WARMUP", "8"))


CONFIG = Config()
