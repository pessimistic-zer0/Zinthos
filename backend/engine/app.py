"""FastAPI application — the Engine ("Global Brain").

Loads the FAISS index (mmap) and warms it once at startup; holds it in app.state for the
process lifetime. Read-only SQLite is per-thread (see db.py). Includes the empty auth seam
and CORS allowlist so going hosted later is config, not a rewrite.

CONCURRENCY CONTRACT: every data route below is a plain `def` (NOT `async def`) on purpose.
FastAPI runs sync handlers in the anyio threadpool, so the blocking work inside them —
SQLite reads, faiss.search(), the urllib LLM call — never blocks the event loop. An
`async def` handler here would run that blocking work ON the loop and serialize the whole
engine (one stuck LLM call = even /health frozen). Only /health stays async: it touches
nothing blocking, so liveness answers instantly even when all worker threads are busy.
"""
from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio.to_thread
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from starlette.requests import Request

from . import artist, hydrate, library, playlist, search, similar
from .config import CONFIG
from .index import VectorIndex


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Cap the sync-handler threadpool: each worker thread lazily opens its own SQLite
    # connection with a ~48 MB page cache (db.py), so the anyio default of 40 threads
    # could pin ~2 GB of swappable heap on the 15 GB box. See CONFIG.worker_threads.
    anyio.to_thread.current_default_thread_limiter().total_tokens = CONFIG.worker_threads
    t = time.time()
    idx = VectorIndex()
    idx.warmup(CONFIG.warmup_queries())
    app.state.index = idx
    app.state.last_request = time.time()
    print(f"engine ready: {idx.ntotal:,} vectors mmap'd, warmed in {time.time()-t:.1f}s "
          f"({CONFIG.worker_threads} worker threads)")
    yield


app = FastAPI(title="Zinthos Engine", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(CONFIG.cors_origins),
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.middleware("http")
async def touch_activity(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Track last activity for the idle-shutdown watchdog."""
    request.app.state.last_request = time.time()
    return await call_next(request)


async def require_auth() -> None:
    """Auth seam — pass-through for local mode. Fill this in when going public."""
    return None


def get_index(request: Request) -> VectorIndex:
    return request.app.state.index


# ── routes ──────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health(idx: VectorIndex = Depends(get_index)) -> dict[str, object]:
    return {"status": "ok", "vectors": idx.ntotal, "nprobe": CONFIG.nprobe}


@app.get("/track/{track_id}", dependencies=[Depends(require_auth)])
def track_detail(track_id: int) -> dict[str, object]:
    rec = hydrate.hydrate_one(track_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"track {track_id} not found")
    return rec


@app.get("/search", dependencies=[Depends(require_auth)])
def semantic_search(q: str, limit: int = 50, offset: int = 0, seed: int = 0) -> dict[str, object]:
    return search.semantic_search(q, limit, offset, seed)


class PlaylistRequest(BaseModel):
    q: str
    size: int = 30


@app.post("/playlist", dependencies=[Depends(require_auth)])
def make_playlist(body: PlaylistRequest) -> dict[str, object]:
    return playlist.generate(body.q, body.size)


class LocalTrack(BaseModel):
    title: str = ""
    artist: str = ""
    album: str = ""
    duration_ms: int | None = None
    isrc: str | None = None


class ScanRequest(BaseModel):
    tracks: list[LocalTrack]
    size: int = 30


@app.post("/library/scan", dependencies=[Depends(require_auth)])
def library_scan(
    body: ScanRequest, idx: VectorIndex = Depends(get_index)
) -> dict[str, object]:
    size = max(1, min(body.size, 100))
    return library.scan(idx, [t.model_dump() for t in body.tracks], size)


@app.post("/library/diagnose", dependencies=[Depends(require_auth)])
def library_diagnose(body: ScanRequest) -> dict[str, object]:
    """Per-file match report (isrc / fuzzy / none) — for diagnosing coverage. No recs."""
    return library.diagnose([t.model_dump() for t in body.tracks])


@app.get("/artist/{name}", dependencies=[Depends(require_auth)])
def artist_detail(name: str) -> dict[str, object]:
    rec = artist.artist_detail(name)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"artist {name!r} not found")
    return rec


@app.get("/search/similar/{track_id}", dependencies=[Depends(require_auth)])
def search_similar(
    track_id: int, idx: VectorIndex = Depends(get_index), k: int = CONFIG.default_k
) -> dict[str, object]:
    results = similar.find_similar(idx, track_id, k)
    if not results:
        raise HTTPException(status_code=404, detail=f"no embedding or similars for track {track_id}")
    return {"seed": track_id, "count": len(results), "results": results}
