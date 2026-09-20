"""Read-only SQLite access — one connection per thread (connections aren't shareable).

The engine never writes master.db; every connection is opened read-only so concurrent
requests in FastAPI's threadpool are safe and the native sqlite calls release the GIL.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Sequence
from typing import Any

from .config import CONFIG

_local = threading.local()

# The fuzzy-match sidecar is optional: present it as `m.track_match` only when built.
#
# LAZY, NOT A MODULE CONSTANT. It used to be evaluated at import. That is correct when the
# artifacts are already on disk, and wrong the moment they are not: the demo Space imports the
# engine and only THEN downloads the slice in the background, so the constant was computed
# against a directory that did not have the sidecar yet and stayed False for the process
# lifetime — /search/by-name answered "name resolution unavailable" against a sidecar sitting
# right there. init_match() re-checks once the files have landed.
_has_match: bool | None = None


def has_match() -> bool:
    global _has_match
    if _has_match is None:
        _has_match = os.path.exists(CONFIG.match_db_path)
    return _has_match


def init_match() -> str:
    """Re-check for the sidecar now the artifacts are in place. Log line for startup."""
    global _has_match
    _has_match = os.path.exists(CONFIG.match_db_path)
    if _has_match:
        return f"track_match sidecar attached ({os.path.basename(CONFIG.match_db_path)})"
    return (f"track_match absent ({CONFIG.match_db_path}) — /search/by-name and the fuzzy half "
            f"of /library/scan are off")


def _uri(path: str) -> str:
    """The read-only URI for a database file, immutable when the medium demands it.

    `mode=ro` still takes POSIX advisory locks and re-reads the change counter. On a normal
    filesystem that is free; on a read-only NETWORK mount that cannot do file locking it is
    fatal — SQLite returns SQLITE_IOERR and the engine dies with "disk I/O error" before
    serving a single request. Measured on a Hugging Face Space with the slice mounted as a
    read-only dataset volume: faiss mmap'd its 3.4 GB index off the same mount without
    complaint, and the first SQLite query failed.

    `immutable=1` promises SQLite the file cannot change underneath it, so it skips locking
    and the change counter entirely. That promise is true here by construction — the slice is
    built once and mounted read-only — but it is a PROMISE, not a check: point this at a
    database something else is writing and you get silent corruption, not an error. Hence the
    opt-in flag rather than always-on.
    """
    return f"file:{path}?mode=ro" + ("&immutable=1" if CONFIG.sqlite_immutable else "")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_uri(CONFIG.db_path), uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute(f"PRAGMA mmap_size={CONFIG.sqlite_mmap_bytes}")
    conn.execute(f"PRAGMA cache_size=-{CONFIG.sqlite_cache_kb}")
    conn.execute("PRAGMA busy_timeout=5000")
    if has_match():
        conn.execute(f"ATTACH DATABASE '{_uri(CONFIG.match_db_path)}' AS m")
    return conn


def conn() -> sqlite3.Connection:
    """The current thread's read-only connection (lazily opened)."""
    c: sqlite3.Connection | None = getattr(_local, "conn", None)
    if c is None:
        c = _local.conn = _connect()
    return c


def query(sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
    return conn().execute(sql, params).fetchall()


def has_table(name: str) -> bool:
    """Whether a table exists in master.db — for optional, separately-built tables (track_tags)."""
    return bool(query("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)))


def placeholders(n: int) -> str:
    """`?,?,?` for an IN-clause of n ids."""
    return ",".join("?" * n)
