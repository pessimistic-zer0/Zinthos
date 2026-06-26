"""
build_track_match.py — M7.2: build the `track_match.db` fuzzy-match sidecar
═══════════════════════════════════════════════════════════════════════════════
A compact `(norm_key, track_id, popularity)` table that lets the engine resolve a local
file to a catalog track by its NORMALIZED title+artist when the file carries no ISRC.

  • norm_key = engine.textnorm.match_key(title, primary_artist)  ← SAME code the engine
    uses at query time (load-bearing: divergence ⇒ zero matches).
  • primary_artist = the artist with the smallest artist_position (SQLite min() bare-column
    trick over the track_artists join).
  • Index idx_track_match_key on norm_key built AFTER the load.

WHY A SIDECAR (not a table in master.db): master.db is read-only/static (we never rebuild
track_search either). A separate file is freely rebuildable/droppable and is ATTACHed
read-only by the engine as `m` only when present (see engine/db.py).

MEMORY DISCIPLINE (15 GB box; ~9 GB resident = swap-thrash): stream the source cursor and
INSERT in small sub-batches, committing per track_id range. journal_mode=OFF (the file is
disposable) so there is no WAL to balloon; the index sort spills to disk, not RAM.

Usage:
  python build_track_match.py --smoke   # small id range, no index — validate logic
  python build_track_match.py           # full build (~255M) + index
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)                       # so `import engine.textnorm` resolves
from engine.textnorm import match_key          # noqa: E402  (shared with the engine)

MASTER = os.environ.get("SONIC_DB", os.path.join(ROOT, "master.db"))
MATCH_DB = os.environ.get("SONIC_MATCH_DB", os.path.join(ROOT, "track_match.db"))
TMPDIR = os.path.join(ROOT, "_build_tmp")      # on-disk temp for the index sort
BATCH = 4_000_000                              # track_id range per transaction
SUBBATCH = 200_000                             # rows per executemany

# One row per track: primary artist via the min(artist_position) bare-column trick.
SELECT_SQL = """
    SELECT t.track_id, t.title, COALESCE(t.popularity, 0) AS pop,
           ar.name AS artist, MIN(ta.artist_position)
    FROM tracks t
    JOIN track_artists ta ON ta.track_id = t.track_id
    JOIN artists ar       ON ar.artist_id = ta.artist_id
    WHERE t.track_id > ? AND t.track_id <= ?
    GROUP BY t.track_id
"""


def build(smoke: bool) -> None:
    os.makedirs(TMPDIR, exist_ok=True)
    os.environ["SQLITE_TMPDIR"] = TMPDIR

    if os.path.exists(MATCH_DB) and not smoke:
        os.remove(MATCH_DB)                    # full builds start clean

    src = sqlite3.connect(f"file:{MASTER}?mode=ro", uri=True)
    src.execute("PRAGMA busy_timeout=60000")
    src.execute("PRAGMA mmap_size=4000000000")

    dst = sqlite3.connect(MATCH_DB)
    dst.execute("PRAGMA journal_mode=OFF")     # disposable file — no WAL to manage
    dst.execute("PRAGMA synchronous=OFF")
    dst.execute("PRAGMA temp_store=FILE")      # index sort spills to SQLITE_TMPDIR
    dst.execute("PRAGMA cache_size=-262144")   # ~256 MB page cache
    dst.execute("DROP TABLE IF EXISTS track_match")
    dst.execute("CREATE TABLE track_match ("
                "norm_key TEXT NOT NULL, track_id INTEGER NOT NULL, popularity INTEGER NOT NULL DEFAULT 0)")
    dst.commit()

    max_id = src.execute("SELECT max(track_id) FROM tracks").fetchone()[0]
    if smoke:
        max_id = min(max_id, 2_000_000)
    print(f"  building track_match{' (SMOKE)' if smoke else ''} over track_id ≤ {max_id:,} "
          f"in {BATCH:,}-id batches …", flush=True)

    insert = "INSERT INTO track_match (norm_key, track_id, popularity) VALUES (?,?,?)"
    t0 = time.time()
    total = scanned = skipped = 0
    lo = 0
    while lo < max_id:
        hi = min(lo + BATCH, max_id)
        cur = src.execute(SELECT_SQL, (lo, hi))
        buf: list[tuple[str, int, int]] = []
        for track_id, title, pop, artist, _pos in cur:
            scanned += 1
            key = match_key(title, artist)
            if key is None:
                skipped += 1
                continue
            buf.append((key, track_id, pop))
            if len(buf) >= SUBBATCH:
                dst.executemany(insert, buf)
                total += len(buf)
                buf.clear()
        if buf:
            dst.executemany(insert, buf)
            total += len(buf)
        dst.commit()
        el = time.time() - t0
        pct = hi / max_id * 100
        eta = el / pct * (100 - pct) if pct else 0
        print(f"    id≤{hi:>12,} ({pct:5.1f}%)  keys={total:>13,}  scanned={scanned:>13,}  "
              f"[{el:5.0f}s, eta {eta:5.0f}s]", flush=True)
        lo = hi
    print(f"  ✓ inserted {total:,} keys ({skipped:,} rows skipped: no title/artist) in {time.time()-t0:.0f}s")

    if not smoke:
        print("  building idx_track_match_key (sort spills to disk) …", flush=True)
        t = time.time()
        dst.execute("CREATE INDEX idx_track_match_key ON track_match(norm_key)")
        dst.commit()
        print(f"  ✓ index in {time.time()-t:.0f}s")

    # Sanity: a sample lookup should use the index.
    plan = dst.execute(
        "EXPLAIN QUERY PLAN SELECT track_id FROM track_match WHERE norm_key = ?", ("x|y",)
    ).fetchall()
    print("  EXPLAIN QUERY PLAN (lookup):")
    for row in plan:
        print("     ", row[-1])

    src.close()
    dst.close()
    print(f"\n══════════════════ M7.2 DONE → {MATCH_DB} ══════════════════")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true", help="small id range, skip the index")
    build(ap.parse_args().smoke)
