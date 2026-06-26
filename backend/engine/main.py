"""`sonic serve` entrypoint — runs the engine on-demand (no daemon, no systemd).

Foreground process; Ctrl-C quits. Optional idle auto-shutdown exits the process after
SONIC_IDLE_SECS of zero requests so a forgotten engine never lingers.
"""
from __future__ import annotations

import os
import signal
import threading
import time

import uvicorn

from .app import app
from .config import CONFIG


def _idle_watchdog() -> None:
    if CONFIG.idle_shutdown_secs <= 0:
        return
    while True:
        time.sleep(30)
        last = getattr(app.state, "last_request", None)
        if last is not None and time.time() - last > CONFIG.idle_shutdown_secs:
            print(f"idle for >{CONFIG.idle_shutdown_secs}s — shutting down.")
            os.kill(os.getpid(), signal.SIGTERM)
            return


def main() -> None:
    threading.Thread(target=_idle_watchdog, daemon=True).start()
    # single worker: one mmap of the index, one set of state; threadpool handles concurrency
    uvicorn.run(app, host=CONFIG.host, port=CONFIG.port, workers=1, log_level="info")


if __name__ == "__main__":
    main()
