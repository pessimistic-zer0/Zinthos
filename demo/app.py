"""
app.py — the Hugging Face Space entrypoint for the Zinthos demo.
═══════════════════════════════════════════════════════════════════════════════
Serves three things from one process on port 7860:

    /         the real React portal (frontend/, built with VITE_DEMO=1)
    /api/*    the real FastAPI engine (backend/engine), unmodified
    /gradio   a small Gradio panel: status, and a plain search box as a fallback UI

WHY IT IS SHAPED LIKE THIS
  A free Space must be `sdk: gradio` — Gradio and Docker Spaces need a paid plan, and
  ZeroGPU (the free tier this runs on) is Gradio-only. But the project's front end is a
  React app, and a Gradio re-implementation of it would be a second, worse UI to maintain.
  So Gradio is mounted INSIDE a FastAPI app rather than being the app: `gr.mount_gradio_app`
  composes onto the existing lifespan instead of replacing it, so the engine's own startup
  (FAISS mmap + warmup, tag-table drift checks) still runs exactly as it does locally.

  If that composition ever breaks on a platform change, set ZINTHOS_UI=gradio in the Space
  variables: the app falls back to a bare `demo.launch()`, which is as vanilla as a Gradio
  Space gets. The portal is then unavailable but the demo still works.

  Nothing here uses the GPU. ZeroGPU is simply the free tier a personal account may use; the
  workload is SQLite and a brute-force FAISS scan, both CPU. `spaces` is still imported,
  because the ZeroGPU runtime expects it at startup — without it the Space was killed about
  45 seconds in on every cold start, while the app was alive, bound and answering 200. No
  @spaces.GPU function is ever called, so no GPU is allocated and no subprocess is forked.

ENGINE CONFIGURATION IS ALL ENV
  Every artifact path in backend/engine/config.py is env-overridable, so the demo runs the
  same code as the local engine and only the files under it change. The defaults below are
  set BEFORE the engine is imported, because config.py reads os.environ at import time.

  SONIC_IDLE_SECS=0 is not optional. backend/engine/main.py ships a watchdog that SIGTERMs
  the process after 15 idle minutes — the right behaviour for an on-demand local engine, and
  fatal for a hosted one, which would kill itself between visitors.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
import threading
from typing import Any

# BEFORE any huggingface_hub import: the Xet transport is selected from this at IMPORT time,
# so setting it beside the download call silently does nothing (learned the hard way). Xet
# reassembles chunks in memory and pulled 16 GB in 11 s on the Space, which OOM-killed the
# container; plain HTTP straight into local_dir is slower and survives.
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

# ── where the data lives ────────────────────────────────────────────────────────
# On the Space the slice is pulled from a Hub dataset repo at boot (the Space's own disk is
# ephemeral, so this runs on every cold start). Locally, point ZINTHOS_ARTIFACTS at the
# directory demo/build_demo_slice.py wrote and nothing is downloaded.
ARTIFACTS = os.environ.get("ZINTHOS_ARTIFACTS", os.path.join(HERE, "dist"))
STATIC = os.environ.get("ZINTHOS_STATIC", os.path.join(HERE, "static"))
DATASET_REPO = os.environ.get("ZINTHOS_DATASET", "")     # e.g. "you/zinthos-demo-slice"
UI_MODE = os.environ.get("ZINTHOS_UI", "portal")          # "portal" | "gradio"
PORT = int(os.environ.get("PORT") or os.environ.get("GRADIO_SERVER_PORT") or 7860)


def fetch_artifacts() -> None:
    """Download the slice from its Hub dataset repo unless it is already on disk.

    A Space's 50 GB disk is NOT persistent, so this is the cold-start path, not a one-off.
    snapshot_download resumes and de-duplicates against its cache, so a restart that kept
    the cache is nearly instant and one that did not re-pulls at Hub speed.
    """
    if os.path.exists(os.path.join(ARTIFACTS, "demo.db")):
        print(f"artifacts already present at {ARTIFACTS}")
        return
    if not DATASET_REPO:
        raise SystemExit(
            f"no demo.db under {ARTIFACTS} and ZINTHOS_DATASET is unset.\n"
            "Build a slice (demo/build_demo_slice.py) and either point ZINTHOS_ARTIFACTS at "
            "it or push it with demo/upload_artifacts.py."
        )
    from huggingface_hub import snapshot_download

    du = shutil.disk_usage(os.path.dirname(ARTIFACTS) or "/")
    print(f"downloading {DATASET_REPO} → {ARTIFACTS}")
    print(f"  disk before: {du.free / 1e9:.1f} GB free of {du.total / 1e9:.1f} GB"
          f" | xet={'off' if os.environ.get('HF_HUB_DISABLE_XET') else 'ON'}"
          f" | workers={os.environ.get('ZINTHOS_DL_WORKERS', '2')}", flush=True)
    # WHY THIS IS THROTTLED
    #   The Hub serves a Space at absurd speed — measured 2.4 GB/s, 16 GB in 11 seconds —
    #   and that is the problem, not the prize. hf_xet buffers chunks in memory while it
    #   reassembles files, and at that rate with 4 workers the container was OOM-killed
    #   mid-download: the process vanished with no traceback, having bound the port and
    #   correctly served 503s right up to the moment it died.
    #   Two workers, and Xet off so the transfer is plain HTTP straight into local_dir with
    #   no chunk cache to hold in RAM or to duplicate on a 50 GB ephemeral disk. A 27 GB
    #   slice at even 300 MB/s is ~90 s, which the deferred boot covers comfortably.
    snapshot_download(
        repo_id=DATASET_REPO,
        repo_type="dataset",
        local_dir=ARTIFACTS,
        token=os.environ.get("HF_TOKEN") or None,
        max_workers=int(os.environ.get("ZINTHOS_DL_WORKERS", "2")),
    )
    du = shutil.disk_usage(ARTIFACTS)
    print(f"artifacts ready — disk after: {du.free / 1e9:.1f} GB free", flush=True)


def slice_total_mb() -> int | None:
    """How big the slice is, in MB, read from the dataset repo on the Hub.

    WHY ASK THE HUB RATHER THAN HARDCODE IT
      The number is only useful if it is right, and a constant in this file would be wrong the
      first time build_demo_slice.py produces a different slice — silently, and in the
      direction that makes a progress bar lie. The repo already knows its own size; asking
      costs one request, once, on a path that is about to spend minutes transferring 27 GB.

    NOTHING HERE MAY RAISE. This runs on the boot path, and a progress bar is not worth a
    dead Space: every failure returns None and the portal falls back to an un-anchored
    "N MB so far", which is exactly what it showed before this existed.
    """
    if not DATASET_REPO:
        return None
    try:
        from huggingface_hub import HfApi

        info = HfApi(token=os.environ.get("HF_TOKEN") or None).repo_info(
            DATASET_REPO, repo_type="dataset", files_metadata=True)
        total = sum(f.size or 0 for f in (info.siblings or []))
        return round(total / 1e6) or None
    except Exception as e:  # noqa: BLE001 — a missing denominator is not a boot failure
        print(f"  ! could not size {DATASET_REPO} ({type(e).__name__}: {e})"
              " — booting without a progress total", flush=True)
        return None


def configure_engine() -> None:
    """Point backend/engine at the slice. setdefault throughout, so a Space variable wins."""
    defaults = {
        "SONIC_DB": os.path.join(ARTIFACTS, "demo.db"),
        "SONIC_INDEX": os.path.join(ARTIFACTS, "demo.faiss"),
        "SONIC_MATCH_DB": os.path.join(ARTIFACTS, "demo_match.db"),
        "SONIC_BITMAP_DIR": os.path.join(ARTIFACTS, "tag_bitmaps"),
        # Kill the idle watchdog — see the module docstring.
        "SONIC_IDLE_SECS": "0",
        # A free Space is 2 vCPU / 16 GB. Each worker thread opens its own SQLite connection
        # with its own page cache, so the product is what matters: 4 × 64 MB ≈ 256 MB.
        "SONIC_THREADS": "4",
        "SONIC_SQLITE_CACHE_KB": "65536",
        # Map the whole slice: mmap is file-backed and reclaimable, so this is page cache the
        # kernel can drop, not heap. Sized for a large slice; a small one simply maps less.
        "SONIC_SQLITE_MMAP": "34000000000",
        # The slice is built once and served read-only, and when it arrives as a mounted
        # dataset volume the mount cannot do POSIX locking — without this every query fails
        # with "disk I/O error". Safe here by construction; see engine/db.py::_uri.
        "SONIC_SQLITE_IMMUTABLE": "1",
        # Same-origin: the portal is served by this very app, so CORS is a safety net only.
        "SONIC_CORS": "",
    }
    for k, v in defaults.items():
        os.environ.setdefault(k, v)


def load_manifest() -> dict[str, Any]:
    path = os.path.join(ARTIFACTS, "manifest.json")
    if os.path.exists(path):
        with open(path) as fh:
            return json.load(fh)
    return {}


# ── boot order: artifacts → env → engine import ─────────────────────────────────
# DEFERRED BOOT
#   The engine cannot be imported until the slice is on disk (config.py reads os.environ at
#   import time, and the index is mmap'd in the lifespan). With a small slice, downloading
#   before binding the port is fine — it is over in a minute. With a 20-30 GB slice it is not:
#   the platform sees a process that has not answered on 7860 for several minutes, and every
#   visitor who arrives during the pull gets a connection error rather than a page.
#
#   So when the artifacts are absent, the port binds FIRST: the whole app is built as usual —
#   importing the engine touches no files, it is only the LIFESPAN that opens the database and
#   mmaps the index — and that lifespan runs on a background task while `gate` answers /api
#   with 503 and a progress line. The portal is static, so it serves throughout. One process,
#   one socket, no restart. When the artifacts are already there (a mounted volume, or a local
#   run) none of this engages and the app boots straight through.
configure_engine()
# True when the slice still has to be downloaded; the boot is deferred EITHER WAY (see below).
NEEDS_DOWNLOAD = not os.path.exists(os.path.join(ARTIFACTS, "demo.db"))

# The Space ships backend/engine as a top-level `engine` package next to this file; a local
# checkout has it under backend/. Accept either so `python demo/app.py` works in both.
for candidate in (HERE, os.path.join(REPO, "backend")):
    if os.path.isdir(os.path.join(candidate, "engine")):
        sys.path.insert(0, candidate)
        break
else:
    raise SystemExit("cannot find the `engine` package (expected demo/engine or backend/engine)")

import anyio.to_thread  # noqa: E402
import gradio as gr  # noqa: E402


class _NoSpaces:
    """Stand-in for `spaces` off a ZeroGPU host, so the decorator below can be written in its
    literal form. The Space always has the real package — the platform installs it itself."""

    @staticmethod
    def GPU(*_a: Any, **_k: Any):  # noqa: N802 — mirrors spaces.GPU
        def deco(fn):
            return fn
        return deco


try:
    import spaces  # noqa: E402
    print("spaces imported (ZeroGPU runtime)")
except ImportError:
    spaces = _NoSpaces()  # type: ignore[assignment]
    print("spaces not installed — fine off ZeroGPU")


@spaces.GPU(duration=10)
def _gpu_probe() -> str:
    """Report whether ZeroGPU granted a device. Never called on any hot path.

    THIS FUNCTION IS LOAD-BEARING AND DOES NOTHING.
      ZeroGPU refuses to start a Space that registers no GPU work. The runtime reports it as
      `errorMessage: "No @spaces.GPU function detected during startup"` — which lives in the
      Hub API's runtime object and NOT in the container logs, where the app looks perfectly
      healthy right until it is killed ~45 s in. That one field is the whole diagnosis, and
      a bare `gr.Interface` with no engine at all failed the same way, which is how it was
      finally isolated.

      The decorator is written in its LITERAL form on purpose: applying it programmatically
      (`_gpu_probe = spaces.GPU(...)(_gpu_probe)`) executes at the same moment and the
      runtime still did not count it. Hence the _NoSpaces shim above, so the literal syntax
      also works on a laptop.

      Nothing calls this automatically, so no GPU is allocated and the daily ZeroGPU quota is
      never touched. The engine is pure CPU: SQLite plus a FAISS scan.
    """
    try:
        import torch

        if torch.cuda.is_available():
            return f"GPU granted: {torch.cuda.get_device_name(0)}"
        return "no CUDA device in this call"
    except ImportError:
        return "torch is not installed — this demo never needs a GPU"


from fastapi.middleware.gzip import GZipMiddleware  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from starlette.routing import Mount, Route  # noqa: E402

from engine import search  # noqa: E402
from engine.app import app as engine_app  # noqa: E402
from engine.config import CONFIG  # noqa: E402

MANIFEST = load_manifest()


# ── the Gradio panel ────────────────────────────────────────────────────────────
EXAMPLES = [
    "dark moody instrumental electronic",
    "energetic upbeat happy dance pop",
    "slow sad acoustic rainy night",
    "fast aggressive angry metal",
]


def gradio_search(query: str, limit: int) -> list[list[str]]:
    """Vibe search, called in-process — no HTTP hop to our own /api."""
    if not Boot.ready:
        st = Boot.status()
        return [[f"Warming up — {st['stage']}", f"{st['elapsed_s']:.0f}s elapsed", "", "", ""]]
    if not query.strip():
        return []
    res = search.semantic_search(query, int(limit))
    return [[r["title"] or "", r["artists"] or "", r.get("album_title") or "",
             (r["release_date"] or "")[:4], str(r["popularity"] or 0)]
            for r in res["results"]]


def status_markdown() -> str:
    tracks = MANIFEST.get("tracks")
    idx = MANIFEST.get("index", {})
    rows = [
        f"**Index** · {idx.get('type', 'unknown')} over "
        f"{idx.get('vectors', 0):,} vectors{' (exact)' if idx.get('exact') else ''}",
        f"**Slice** · {tracks:,} tracks" if tracks else "**Slice** · unknown",
        f"**Built from** · popularity ≥ {MANIFEST.get('min_popularity', '?')} of the "
        f"255M-track catalogue, catalog duplicates collapsed",
        f"**LLM fallback** · {CONFIG.llm_provider or 'off (rules parser only)'}",
    ]
    return "\n\n".join(rows)


def build_blocks() -> gr.Blocks:
    with gr.Blocks(title="Zinthos", analytics_enabled=False) as blocks:
        gr.Markdown("## Zinthos\nSearch ~255M tracks by how they *sound* — "
                    "this demo runs the full engine over a slice of that catalogue.\n\n"
                    "**The real interface is at [/](/)** — this panel is a plain fallback.")
        gr.Markdown(status_markdown())
        with gr.Row():
            q = gr.Textbox(label="Describe a vibe", scale=4,
                           placeholder="rainy 3am drive, warm bass, nothing cheerful")
            n = gr.Slider(5, 50, value=20, step=5, label="Results", scale=1)
        out = gr.Dataframe(headers=["Title", "Artists", "Album", "Year", "Popularity"],
                           wrap=True, label="Results")
        gr.Examples(EXAMPLES, inputs=q)
        q.submit(gradio_search, [q, n], out)
        n.release(gradio_search, [q, n], out)

        # Registers the @spaces.GPU function with Gradio. ZeroGPU scans the app's handlers at
        # startup and kills the Space if it finds none — see _gpu_probe. Nothing here calls it
        # unless a human presses the button.
        with gr.Accordion("Runtime", open=False):
            gr.Markdown("This demo runs on CPU. The button only reports what ZeroGPU would "
                        "hand out, and is what satisfies the runtime's startup check.")
            gpu_out = gr.Textbox(label="ZeroGPU", interactive=False)
            gr.Button("Probe ZeroGPU").click(_gpu_probe, outputs=gpu_out)
    return blocks


# ── composition ─────────────────────────────────────────────────────────────────
class Boot:
    """Where the deferred boot has got to. Read by the gate below and by /manifest.json."""

    ready = False
    stage = "starting"
    detail = ""
    started = time.time()
    # Total bytes the slice will occupy, in MB, so the portal can draw a real progress bar
    # instead of an unanchored "860 MB so far". None whenever the Hub could not be asked —
    # see slice_total_mb; every consumer treats its absence as "no denominator", never as 0.
    total_mb: int | None = None

    @classmethod
    def bytes_on_disk(cls) -> int:
        try:
            return sum(os.path.getsize(os.path.join(r, f))
                       for r, _, fs in os.walk(ARTIFACTS) for f in fs)
        except OSError:
            return 0

    @classmethod
    def status(cls) -> dict[str, Any]:
        out: dict[str, Any] = {"ready": cls.ready, "stage": cls.stage,
                               "elapsed_s": round(time.time() - cls.started, 1)}
        if cls.detail:
            out["detail"] = cls.detail
        if cls.stage == "downloading":
            out["downloaded_mb"] = round(cls.bytes_on_disk() / 1e6)
            if cls.total_mb:
                out["total_mb"] = cls.total_mb
        return out


def diagnose_artifacts() -> None:
    """Report what the artifact directory can actually do, after a boot failure.

    Exists because "disk I/O error" from SQLite says nothing about WHICH thing failed, and a
    Space is not a place you can poke at interactively. Two guesses have already been spent on
    this; the point of the matrix below is that the next change is informed by a measurement.
    """
    import sqlite3

    print("─── artifact diagnostics ───")
    try:
        for name in sorted(os.listdir(ARTIFACTS)):
            path = os.path.join(ARTIFACTS, name)
            kind = f"{os.path.getsize(path):,} B" if os.path.isfile(path) else "dir"
            print(f"  {name:24} {kind}")
    except OSError as e:
        print(f"  listdir failed: {e}")

    for small in ("manifest.json", "README.md"):
        sp = os.path.join(ARTIFACTS, small)
        try:
            with open(sp, "rb") as fh:
                print(f"  small file  {small}: read {len(fh.read(64))} B ok")
        except OSError as e:
            print(f"  small file  {small}: {type(e).__name__}: {e}")

    db = os.path.join(ARTIFACTS, "demo.db")
    try:
        size = os.path.getsize(db)
        with open(db, "rb") as fh:
            head = fh.read(16)
            # A random read deep into the file: an LFS pointer or a lazily-streamed mount
            # fails here while the first page reads fine.
            fh.seek(size // 2)
            mid = fh.read(4096)
            fh.seek(max(0, size - 4096))
            tail = fh.read(4096)
        print(f"  header      {head!r}  (expect b'SQLite format 3\\x00')")
        print(f"  mid read    {len(mid)} B at offset {size // 2:,}")
        print(f"  tail read   {len(tail)} B")
    except OSError as e:
        print(f"  raw read failed: {type(e).__name__}: {e}")

    probe = "SELECT count(*) FROM sqlite_master"
    matrix = [
        ("mode=ro", f"file:{db}?mode=ro", []),
        ("mode=ro immutable=1", f"file:{db}?mode=ro&immutable=1", []),
        ("mode=ro immutable=1 mmap=0", f"file:{db}?mode=ro&immutable=1",
         ["PRAGMA mmap_size=0"]),
        ("mode=ro mmap=0", f"file:{db}?mode=ro", ["PRAGMA mmap_size=0"]),
        ("immutable=1 mmap=0 + real query", f"file:{db}?mode=ro&immutable=1",
         ["PRAGMA mmap_size=0"]),
    ]
    for label, uri, pragmas in matrix:
        try:
            c = sqlite3.connect(uri, uri=True)
            for pr in pragmas:
                c.execute(pr)
            n = c.execute(probe).fetchone()[0]
            extra = ""
            if "real query" in label:
                extra = f", track_search row → {c.execute('SELECT track_id FROM track_search LIMIT 1').fetchone()}"
            print(f"  ✓ {label:32} {n} objects{extra}")
            c.close()
        except Exception as e:  # noqa: BLE001 — the whole point is to print it
            print(f"  ✗ {label:32} {type(e).__name__}: {e}")
    print("─── end diagnostics ───", flush=True)


class ApiGate:
    """ASGI wrapper that answers for /api until the engine is ready.

    A wrapper, not `@app.middleware`, because middleware can only be added BEFORE an app
    starts and Gradio's `launch()` starts the app itself (see `serve`). Mounting is a route,
    which can be added afterwards; so the gate and the compression both become ASGI layers
    around the engine rather than middleware on the parent.
    """

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or Boot.ready:
            await self.inner(scope, receive, send)
            return
        st = Boot.status()
        if "total_mb" in st:
            got = f", {st['downloaded_mb'] / 1000:.1f} of {st['total_mb'] / 1000:.1f} GB"
        elif "downloaded_mb" in st:
            got = f", {st['downloaded_mb']} MB so far"
        else:
            got = ""
        detail = f"warming up — {st['stage']}{got} ({st['elapsed_s']:.0f}s)"
        # /api/health answers 200 EVEN WHILE WARMING, with status != "ok". Liveness and
        # readiness are different questions: the process IS alive and serving, it just has no
        # index yet. Callers check `status`, which is what frontend/src/lib/demo.ts does.
        # Starlette strips the mount prefix, but not identically across versions, so match
        # the suffix rather than assuming "/health" or "/api/health".
        path = scope.get("path", "").rstrip("/")
        if path.endswith("/health") or path in ("", "/health"):
            body = {"status": st["stage"], "vectors": 0, "detail": detail, **st}
            code = 200
        else:
            body, code = {"detail": detail, **st}, 503
        await JSONResponse(body, status_code=code)(scope, receive, send)


def boot_in_background() -> None:
    """Download if needed, then hold the engine's lifespan open for the process lifetime.

    Runs on its own thread with its own event loop, because Gradio's `launch()` owns the
    serving loop and we attach to it rather than creating it. The engine's lifespan is an
    async context manager whose exit tears the index down, so the coroutine parks inside it
    instead of returning.

    One consequence worth knowing: engine/app.py's lifespan caps the anyio threadpool, and
    that limiter is bound to the loop it runs on — so the cap does not apply to the serving
    loop here. It is a memory guard, not correctness, and the Space's SQLite cache is sized
    small enough that it does not bite.
    """

    async def run() -> None:
        try:
            if NEEDS_DOWNLOAD:
                # Stage first, total second: the stage is what the portal needs to start
                # saying something, and the Hub round trip that follows costs a second.
                Boot.stage = "downloading"
                Boot.total_mb = await anyio.to_thread.run_sync(slice_total_mb)
                await anyio.to_thread.run_sync(fetch_artifacts)
            Boot.stage = "loading index"
            async with engine_app.router.lifespan_context(engine_app):
                MANIFEST.update(load_manifest())
                Boot.stage, Boot.ready = "ready", True
                print(f"demo ready in {time.time() - Boot.started:.0f}s", flush=True)
                while True:
                    await asyncio.sleep(3600)
        except Exception as exc:  # noqa: BLE001 — reported, not swallowed
            Boot.stage, Boot.detail = "failed", f"{type(exc).__name__}: {exc}"
            print(f"boot FAILED: {Boot.detail}", flush=True)
            diagnose_artifacts()

    asyncio.run(run())


def serve() -> None:
    """Launch the Gradio app, attach the engine to it, and block.

    WHY GRADIO LAUNCHES INSTEAD OF BEING MOUNTED
      ZeroGPU decides whether a Space is legitimate by looking for a @spaces.GPU function, and
      it performs that check through `Blocks.launch()`. Built the other way round — a FastAPI
      app with `gr.mount_gradio_app` under `uvicorn.run` — `launch()` never runs, the check
      never fires, and the Space dies about 45 s into every cold start with
      `errorMessage: "No @spaces.GPU function detected during startup"` in the Hub API (and
      nothing at all in the container logs). Registering the function is not enough; the
      literal decorator at module level is not enough. Gradio has to launch.

      So Gradio owns "/" and the server, and everything else is mounted onto the app it hands
      back. That inverts the previous structure, and it is not a style preference — it is the
      only arrangement the platform accepts.

      Consequence: on ZeroGPU the portal CANNOT be served from this Space at "/", because
      Gradio is there. Use --split (demo/push_space.py), which puts the portal on a free
      static Space. A bundled portal falls back to /portal.
    """
    blocks = build_blocks()
    app, _local, _share = blocks.launch(
        server_name="0.0.0.0", server_port=PORT, prevent_thread_lock=True, ssr_mode=False,
        show_api=False,
    )
    print(f"gradio launched on :{PORT}; attaching the engine", flush=True)

    # GZipMiddleware is a plain ASGI app, so it can wrap rather than be installed — the
    # portal's 204 KB bundle and every ~13 KB result set compress 3-4x.
    def manifest(_request: Any) -> JSONResponse:
        return JSONResponse({**MANIFEST, "boot": Boot.status()})

    # INSERTED AT THE FRONT, not appended: Gradio's launch has already registered its own
    # catch-all, and Starlette matches routes in order — appending puts these behind it and
    # every one of them 404s. (Measured: /manifest.json 404'd until this became insert(0).)
    app.router.routes.insert(0, Route("/manifest.json", manifest, methods=["GET"]))
    app.router.routes.insert(
        0, Mount("/api", app=GZipMiddleware(ApiGate(engine_app), minimum_size=1000)))

    if os.path.isdir(STATIC):
        # Gradio owns "/", so a bundled portal lives beside it. --split is the better answer.
        app.router.routes.insert(
            0, Mount("/portal", app=StaticFiles(directory=STATIC, html=True), name="portal"))
        print("portal bundled at /portal (Gradio owns / on ZeroGPU — prefer --split)")

    threading.Thread(target=boot_in_background, daemon=True, name="zinthos-boot").start()
    threading.Event().wait()      # Gradio serves on its own thread; park this one.


def main() -> None:
    if UI_MODE == "gradio":
        # Escape hatch: the panel alone, with no engine attached.
        print("ZINTHOS_UI=gradio — serving the Gradio panel only")
        build_blocks().launch(server_name="0.0.0.0", server_port=PORT, ssr_mode=False)
        return
    serve()


if __name__ == "__main__":
    main()
