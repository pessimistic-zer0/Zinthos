"""
build_track_tags.py — build the `track_tags` hot table inside master.db
═══════════════════════════════════════════════════════════════════════════════
One row per track that has ANY tagged artist, carrying that track's genre-family
membership as two bitmasks plus its raw fine-tag ids. This is the F6 re-rank source
for the region/sonic terms — the third tier alongside `track_search`.

WHY A TABLE AND NOT A JOIN
  The live join `track_artists ⋈ artist_genres` for one 1,499-candidate pool measures
  374–578 ms against a 15–50 ms warm budget for /search/similar — a 10–30x regression,
  and exactly the anti-pattern two-tier hydration exists to prevent. Precomputed, the
  same lookup is a single PRIMARY-KEY probe over the candidate ids, the same shape as
  `track_search`'s re-rank fetch.

WHAT A ROW MEANS
  region_mask / sonic_mask  bitwise OR of every family carried by every credited artist.
                            Bit assignment comes from engine.tagfamily's dict ORDER and is
                            persisted to `track_tag_family` — see BIT STABILITY below.
  tag_ids                   the raw tags, packed little-endian uint16, sorted, decodable
                            via `track_tag_vocab`. Kept for an exact-tag Jaccard tiebreaker
                            and for auditing WHY a track got a family. Rebuilding 170M rows
                            to add a column later is far worse than 2 bytes per tag now.

⚠ BIT STABILITY — READ BEFORE EDITING tagfamily.py
  Bit N is whatever family sits at position N in REGION_RULES / SONIC_RULES. INSERTING a
  family in the middle silently redefines every mask already written to disk: rows keep
  their bits, the code reads them as different families, and nothing errors. Two guards:
    1. This builder writes the assignment to `track_tag_family`.
    2. The engine must verify that table against the live tagfamily.py at startup and
       refuse the tag terms on mismatch (see verify_bits below — call it from app lifespan).
  APPEND new families at the END of the dicts, or rebuild. Never insert or reorder.

MEMORY DISCIPLINE (the box is 15 GB; ~9 GB resident = swap-thrash threshold)
  • The artist→families map is built once in RAM: ~1.57M tagged artists of 15.4M total,
    each holding two small ints and a packed bytes — order ~100 MB, not a concern.
  • track_artists is streamed in PRIMARY KEY order, which IS (track_id, artist_id) order,
    so grouping by track is a single pass with O(1) memory. No sort, no temp spill.
  • Insert in track_id-ordered BATCHES, each its own transaction + WAL truncate — the same
    discipline build_track_search.py learned the hard way (a single-transaction build grew
    an 11 GB WAL and swap-died).

Usage:
  python build_track_tags.py --smoke     # first 8M track_ids, validates logic + prints yield
  python build_track_tags.py             # full build
  python build_track_tags.py --verify    # check on-disk bit assignment vs live tagfamily.py
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from engine.tagfamily import REGION_RULES, SONIC_RULES, families  # noqa: E402

DB_PATH = os.environ.get("SONIC_DB", os.path.join(ROOT, "master.db"))
BATCH = 4_000_000          # track_id range per transaction (mirrors build_track_search)
INSERT_CHUNK = 200_000     # rows per executemany

REGION_BITS = {f: 1 << i for i, f in enumerate(REGION_RULES)}
SONIC_BITS = {f: 1 << i for i, f in enumerate(SONIC_RULES)}

SCHEMA = """
CREATE TABLE IF NOT EXISTS track_tags (
    track_id    INTEGER PRIMARY KEY,
    region_mask INTEGER NOT NULL,
    sonic_mask  INTEGER NOT NULL,
    tag_ids     BLOB
);
CREATE TABLE IF NOT EXISTS track_tag_family (
    axis TEXT    NOT NULL,          -- 'region' | 'sonic'
    bit  INTEGER NOT NULL,          -- bit POSITION, not the mask value
    name TEXT    NOT NULL,
    PRIMARY KEY (axis, bit)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS track_tag_vocab (
    tag_id INTEGER PRIMARY KEY,
    tag    TEXT NOT NULL UNIQUE
);
"""
# NOTE: no secondary index on track_tags. Every read is a PK probe over the candidate ids —
# that is the entire point of the table. An index here would cost GB and buy nothing.


def open_rw(out: str) -> tuple[sqlite3.Connection, str]:
    """Open the OUTPUT db; returns (conn, source-table prefix).

    `--out` lets the whole build run against a scratch file with master.db attached read-only,
    so throughput can be measured honestly without writing to the 145 GB original.
    """
    db = sqlite3.connect(out, uri=True)   # uri=True so the read-only ATTACH below resolves
    c = db.cursor()
    c.execute("PRAGMA busy_timeout=60000")
    c.execute("PRAGMA journal_mode=WAL")
    # NOT synchronous=OFF, despite build_track_search.py using it. Its comment ("table is
    # rebuildable") was true when master.db was itself mid-build; it is not true now. This runs
    # against a finished 162 GiB database, where OFF risks corrupting the WHOLE FILE on a crash
    # or power cut — to save minutes on a ~30 min job. NORMAL in WAL mode can lose the last
    # transaction on power loss but cannot corrupt the file, and each batch commits anyway.
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA temp_store=FILE")        # spill to SQLITE_TMPDIR, never to RAM
    c.execute("PRAGMA cache_size=-524288")     # ~512 MB page cache
    c.execute("PRAGMA mmap_size=4000000000")   # 4 GB of clean, reclaimable read pages
    if os.path.abspath(out) == os.path.abspath(DB_PATH):
        return db, ""
    c.execute("ATTACH DATABASE ? AS src", (f"file:{DB_PATH}?mode=ro",))
    return db, "src."


def build_artist_map(c: sqlite3.Cursor, src: str = "") -> tuple[dict[int, tuple[int, int, bytes]], dict[str, int]]:
    """artist_id → (region_mask, sonic_mask, packed tag ids), plus the tag→id vocabulary.

    Tag ids are assigned in first-seen order over a stable `ORDER BY genre`, so a rebuild
    reproduces the same vocabulary; `track_tag_vocab` records it either way.
    """
    tag_id: dict[str, int] = {}
    art: dict[int, tuple[int, int, set[int]]] = {}
    unmapped: set[str] = set()
    for aid, genre in c.execute(f"SELECT artist_id, genre FROM {src}artist_genres ORDER BY genre"):
        tid = tag_id.setdefault(genre, len(tag_id))
        fams = families(genre)
        if not fams:
            unmapped.add(genre)
        r = s = 0
        for f in fams:
            r |= REGION_BITS.get(f, 0)
            s |= SONIC_BITS.get(f, 0)
        prev = art.get(aid)
        if prev is None:
            art[aid] = (r, s, {tid})
        else:
            art[aid] = (prev[0] | r, prev[1] | s, prev[2] | {tid})
    packed = {a: (r, s, struct.pack(f"<{len(ids)}H", *sorted(ids)))
              for a, (r, s, ids) in art.items()}
    if unmapped:
        # Deliberate (see tagfamily.OVERRIDES) or an oversight — print so it is never silent.
        print(f"  note: {len(unmapped)} tag(s) map to no family: {sorted(unmapped)}")
    return packed, tag_id


def stream_batch(c: sqlite3.Cursor, art: dict[int, tuple[int, int, bytes]],
                 lo: int, hi: int, src: str = ""):
    """Yield (track_id, region_mask, sonic_mask, tag_ids) for one track_id range.

    track_artists' PRIMARY KEY is (track_id, artist_id), so this range scan already arrives
    in track order — the group-by is a single pass holding one track's accumulator.
    """
    cur = None
    rmask = smask = 0
    ids: set[int] = set()
    rows = c.execute(
        f"SELECT track_id, artist_id FROM {src}track_artists WHERE track_id > ? AND track_id <= ?",
        (lo, hi))
    for tid, aid in rows:
        if tid != cur:
            if cur is not None and (rmask or smask or ids):
                yield (cur, rmask, smask, struct.pack(f"<{len(ids)}H", *sorted(ids)))
            cur, rmask, smask, ids = tid, 0, 0, set()
        v = art.get(aid)
        if v is not None:
            rmask |= v[0]
            smask |= v[1]
            ids.update(struct.unpack(f"<{len(v[2]) // 2}H", v[2]))
    if cur is not None and (rmask or smask or ids):
        yield (cur, rmask, smask, struct.pack(f"<{len(ids)}H", *sorted(ids)))


def build(smoke: bool, out: str) -> None:
    db, src = open_rw(out)
    c = db.cursor()
    c.executescript(SCHEMA)
    c.execute("DELETE FROM track_tags")
    c.execute("DELETE FROM track_tag_family")
    c.execute("DELETE FROM track_tag_vocab")

    c.executemany("INSERT INTO track_tag_family VALUES ('region',?,?)",
                  list(enumerate(REGION_RULES)))
    c.executemany("INSERT INTO track_tag_family VALUES ('sonic',?,?)",
                  list(enumerate(SONIC_RULES)))

    t0 = time.time()
    print("  building artist→family map …")
    art, tag_id = build_artist_map(c, src)
    c.executemany("INSERT INTO track_tag_vocab VALUES (?,?)",
                  [(i, t) for t, i in tag_id.items()])
    db.commit()
    print(f"  ✓ {len(art):,} tagged artists, {len(tag_id)} tags "
          f"({len(REGION_RULES)} region + {len(SONIC_RULES)} sonic families) "
          f"in {time.time()-t0:.0f}s")

    max_id = c.execute(f"SELECT max(track_id) FROM {src}tracks").fetchone()[0]
    if smoke:
        max_id = min(max_id, 8_000_000)
    print(f"  streaming track_artists over track_id ≤ {max_id:,} in {BATCH:,}-id batches …")

    ins = "INSERT OR REPLACE INTO track_tags VALUES (?,?,?,?)"
    # SEPARATE cursors: `rc` streams the read, `c` does the inserts. Sharing one cursor silently
    # truncates the stream — executemany() resets it mid-iteration, so the generator stops at the
    # first flush and every batch yields exactly INSERT_CHUNK+1 rows. Caught by the smoke yield
    # reading 5.0% against a measured 57.1%.
    rc = db.cursor()
    t0, total, lo = time.time(), 0, 0
    while lo < max_id:
        hi = min(lo + BATCH, max_id)
        buf = []
        for row in stream_batch(rc, art, lo, hi, src):
            buf.append(row)
            if len(buf) >= INSERT_CHUNK:
                c.executemany(ins, buf)
                total += len(buf)
                buf.clear()
        if buf:
            c.executemany(ins, buf)
            total += len(buf)
        db.commit()
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")     # keep the WAL tiny
        el = time.time() - t0
        pct = hi / max_id * 100
        print(f"    id≤{hi:>12,} ({pct:5.1f}%)  rows={total:>13,}  "
              f"[{el:5.0f}s, eta {el/pct*(100-pct) if pct else 0:5.0f}s]", flush=True)
        lo = hi

    print(f"  ✓ inserted {total:,} rows in {time.time()-t0:.0f}s")
    if smoke:
        n = c.execute(f"SELECT COUNT(*) FROM {src}tracks WHERE track_id <= ?", (max_id,)).fetchone()[0]
        print(f"  SMOKE yield: {total:,} of {n:,} tracks carry a tag ({100*total/max(n,1):.1f}%)")
    db.close()


def verify_bits() -> int:
    """Fail loudly if the on-disk bit assignment no longer matches tagfamily.py.

    The engine must run this at startup before trusting any mask — a reordered dict makes
    every stored mask mean something different WITHOUT any error surfacing.
    """
    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    live = [("region", i, f) for i, f in enumerate(REGION_RULES)] + \
           [("sonic", i, f) for i, f in enumerate(SONIC_RULES)]
    try:
        disk = [tuple(r) for r in db.execute(
            "SELECT axis, bit, name FROM track_tag_family ORDER BY axis, bit")]
    except sqlite3.OperationalError:
        print("track_tag_family missing — track_tags has not been built."); return 2
    if disk != sorted(live):
        print("MISMATCH between on-disk bit assignment and engine.tagfamily:")
        for d, l in zip(disk, sorted(live)):
            if d != l:
                print(f"  disk {d}  !=  live {l}")
        print("track_tags must be REBUILT, or the family dicts restored to their built order.")
        return 1
    print(f"✓ bit assignment matches ({len(disk)} families)")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--smoke", action="store_true", help="first 8M track_ids only")
    ap.add_argument("--verify", action="store_true", help="check bit assignment, build nothing")
    ap.add_argument("--out", default=DB_PATH,
                    help="write to this db instead of master.db (dry run; master is attached ro)")
    a = ap.parse_args()
    sys.exit(verify_bits() if a.verify else (build(a.smoke, a.out) or 0))
