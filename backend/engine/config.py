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
    faiss_topk: int = 100                                 # candidates before re-rank
    default_k: int = 20                                   # results returned
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
