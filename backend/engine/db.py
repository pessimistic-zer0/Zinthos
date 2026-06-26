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
HAS_MATCH = os.path.exists(CONFIG.match_db_path)


def has_match() -> bool:
    return HAS_MATCH


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{CONFIG.db_path}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute(f"PRAGMA mmap_size={CONFIG.sqlite_mmap_bytes}")
    conn.execute(f"PRAGMA cache_size=-{CONFIG.sqlite_cache_kb}")
    conn.execute("PRAGMA busy_timeout=5000")
    if HAS_MATCH:
        conn.execute(f"ATTACH DATABASE 'file:{CONFIG.match_db_path}?mode=ro' AS m")
    return conn


def conn() -> sqlite3.Connection:
    """The current thread's read-only connection (lazily opened)."""
    c: sqlite3.Connection | None = getattr(_local, "conn", None)
    if c is None:
        c = _local.conn = _connect()
    return c


def query(sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
    return conn().execute(sql, params).fetchall()


def placeholders(n: int) -> str:
    """`?,?,?` for an IN-clause of n ids."""
    return ",".join("?" * n)
