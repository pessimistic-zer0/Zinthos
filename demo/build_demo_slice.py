"""
build_demo_slice.py — carve a deployable demo slice out of the 162 GB master.db
═══════════════════════════════════════════════════════════════════════════════
Writes `demo.db`: the tables the engine reads, restricted to a slice of the catalogue and
compacted to ~505 bytes/track, so it fits a free host's disk and RAM. Everything downstream
(engine, FAISS index, match sidecar, tag bitmaps) is then pointed at it by env var —
see demo/README.md. No engine code is forked; `backend/engine/config.py` already made
every artifact path overridable.

WHAT GETS KEPT, AND WHY A POPULARITY CUT IS ENOUGH
  The obvious worry with "take the popular ones" is that it leaves a demo of nothing but
  Western chart pop, which would kill the one result F6 exists for: the region/sonic gate
  that pulls a Bollywood seed back out of Thai pop. Measured against the tag bitmaps, it
  does not. At popularity >= 30 the cut is 2,063,150 tracks and EVERY one of the 22
  families still has tens of thousands of members:

      region/south_asian  70,457   region/latin   222,790   sonic/metal      49,562
      region/mena         56,042   region/african  46,999   sonic/classical  29,284

  The gate needs >= max(40, 2k) same-family candidates inside a 1500-wide pool
  (CONFIG.tag_gate_min) — these counts clear that by three orders of magnitude. The
  --family-floor top-up below is insurance for anyone who raises --min-pop, not something
  the default needs.

  TRACK IDS ARE NOT REMAPPED. They stay the master.db rowids, so every query in the engine,
  every FAISS id, and every tag bitmap keeps meaning the same thing. The slice is a subset,
  not a translation.

DEDUPE
  The catalog holds hundreds of copies of a hit (Shape of You: 902). Carrying them costs
  bundle bytes and hands mega-hits whole FAISS pools of themselves. This collapses them at
  build time on the same keys hydrate.dupe_keys uses — folded (title, artists) plus ISRC —
  keeping the most popular copy. It is the EXACT-key half of the engine's dedupe; the
  artist-subset overlap pass (hydrate._Seen) still runs at query time on top.

MEMORY DISCIPLINE (same 15 GB box as every other builder here)
  • Phase 1/2 hold ids as numpy arrays and dedupe keys as 64-bit HASHES, never as Python
    strings: 2M rows cost ~55 MB instead of ~500 MB.
  • Phase 2 probes master.db in track_id-ASCENDING chunks — measured 0.068 ms/row, ~2 min
    for the whole slice, because sorted PK probes walk the file forward instead of seeking.
  • Phase 3 never probes master.db per kept id — it SCANS each source table once, in rowid
    batches, so the cost is set by master.db's size and not the slice's (see the note above
    _copy_scan). Output opens journal_mode=OFF: it is disposable and rebuildable.

HOW BIG CAN IT GET
  A popularity threshold bottoms out at ~38.6M tracks: 210M of the catalogue's 255M sit at
  popularity 0, so `--min-pop 1` already takes everything anyone has played. At ~505 B/track
  that is a 19.5 GB bundle. To go further — or to keep the long tail's character rather than
  only its famous end — `--sample-tail N` draws N more uniformly from below the floor.

Usage:
  python demo/build_demo_slice.py --smoke          # ~30k tracks — validates the whole path
  python demo/build_demo_slice.py                  # default slice: popularity >= 30
  python demo/build_demo_slice.py --min-pop 1      # everything with any popularity (~19.5 GB)
  python demo/build_demo_slice.py --min-pop 1 --sample-tail 20000000   # ~30 GB
  python demo/build_demo_slice.py --no-compact     # the naive layout, for an A/B
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "backend"))
from engine.textnorm import fold  # noqa: E402  (the SAME fold hydrate.dupe_key uses)

MASTER = os.environ.get("SONIC_DB", os.path.join(ROOT, "master.db"))
OUT_DIR = os.environ.get("ZINTHOS_DEMO_DIR", os.path.join(HERE, "dist"))
BITMAP_DIR = os.environ.get("SONIC_BITMAP_DIR", os.path.join(ROOT, "model_training", "tag_bitmaps"))

PROBE_CHUNK = 5_000        # ids per IN-clause when probing master in phase 2

# Tables copied for the kept ids, each by one sequential scan of the source. ml_10d_embeddings
# is absent on purpose — see EMBEDDINGS below.
TRACK_TABLES = ("tracks", "track_search", "track_audio_features",
                "track_tags", "track_artists")
# Small dimension tables copied whole.
WHOLE_TABLES = ("genres", "track_tag_family", "track_tag_vocab")

# ── COMPACTION: what each copied table actually needs ───────────────────────────
# Measured with dbstat on a built slice, the naive copy spends its bytes like this (per
# track): tracks 145, track_audio_features 106, albums 92, ml_10d_embeddings 66,
# track_search+idx_search 76, everything else ~166 → ~751 B/track all in.
#
# Three of those are pure waste, and cutting them takes ~751 B/track to ~505 without losing
# a single feature. At 38.6M tracks — the whole popularity>=1 catalogue — that is the
# difference between a 29 GB bundle and a 19.5 GB one.
#
#   1. track_audio_features is 17% of the database and ONE column of it is ever read.
#      playlist.py's only query joins it for `camelot_code`; tempo/energy/valence come from
#      track_search in the same SELECT. Keeping 13 REAL columns to use a 2-char string costs
#      94 B/track. Rows are kept even when camelot_code is NULL — the join is what puts a
#      track in a playlist pool, so dropping the row would drop the track.
#
#   2. preview_url is 107 bytes of which 67 never vary: every one is
#      "https://p.scdn.co/mp3-preview/" + 40 hex + one single "?cid=…" tail (verified across
#      the slice: one distinct tail, every URL exactly 107 chars, every middle 40 hex).
#      unhex() packs the middle to a 20-byte blob at BUILD time — so the Space's SQLite
#      version is irrelevant, it only ever reads the blob — and hydrate.py rebuilds the URL
#      from the template in demo_meta. 85 B/track. Anything that does NOT match the template
#      is stored as its original TEXT, so a stray URL shape survives untouched.
#
#   3. ml_10d_embeddings is redundant against an EXACT index. similar.get_embedding reads the
#      seed's vector from it because the production index is IVFSQfp16 and reconstructing
#      from that is lossy. A demo index is flat (or IVFFlat) and reconstruct() returns the
#      stored vector bit-for-bit — measured max|reconstruct - normalize(blob)| = 0.0. So the
#      vectors go to a flat sidecar for build_demo_index.py to consume and never enter
#      demo.db at all. 66 B/track.
#
# Set --no-compact to write the naive layout instead, e.g. to A/B a suspicion.
PREVIEW_PREFIX = "https://p.scdn.co/mp3-preview/"
PREVIEW_HEX_LEN = 40

# dest columns -> the SELECT that fills them, when a table is not copied verbatim.
COMPACT_COLUMNS: dict[str, tuple[str, str]] = {
    "track_audio_features": ("track_id, camelot_code", "s.track_id, s.camelot_code"),
}

# Output DDL. track_audio_features gets a real INTEGER PRIMARY KEY here — in master it is a
# plain column with a unique index, because the ETL appended it in the features file's order
# and could not sort 255M rows by track_id. At this size we can, so the probe is a rowid seek.
SCHEMA = """
CREATE TABLE albums (
    album_id      INTEGER PRIMARY KEY,
    source_a_id   TEXT,
    title         TEXT NOT NULL,
    album_type    TEXT,
    release_date  TEXT,
    cover_art_url TEXT
);
CREATE TABLE tracks (
    track_id     INTEGER PRIMARY KEY,
    album_id     INTEGER,
    isrc         TEXT,
    title        TEXT,
    popularity   INTEGER,
    release_date TEXT,
    is_explicit  INTEGER,
    duration_ms  INTEGER,
    -- BLOB when the URL matched demo_meta.preview_template (20 packed bytes), TEXT when it
    -- did not. hydrate.expand_preview handles both; see the COMPACTION note above.
    preview_url  BLOB
);
CREATE TABLE track_audio_features (
    track_id        INTEGER PRIMARY KEY,
    camelot_code    TEXT
);
CREATE TABLE artists (
    artist_id       INTEGER PRIMARY KEY,
    source_a_id     TEXT,
    name            TEXT,
    popularity      INTEGER,
    followers_total INTEGER
);
CREATE TABLE track_artists (
    track_id        INTEGER,
    artist_id       INTEGER,
    artist_position INTEGER,
    PRIMARY KEY (track_id, artist_id)
);
CREATE TABLE track_search (
    track_id     INTEGER PRIMARY KEY,
    popularity   INTEGER NOT NULL DEFAULT 0,
    genre_id     INTEGER,
    genre_source INTEGER,
    release_year INTEGER,
    danceability INTEGER, energy INTEGER, valence INTEGER,
    acousticness INTEGER, instrumentalness INTEGER, speechiness INTEGER,
    liveness INTEGER, tempo INTEGER, loudness INTEGER
);
-- Self-describing slice metadata. The engine reads it rather than being told about the slice
-- by env var, so a bundle cannot be paired with the wrong configuration.
CREATE TABLE demo_meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE track_tags (
    track_id    INTEGER PRIMARY KEY,
    region_mask INTEGER NOT NULL,
    sonic_mask  INTEGER NOT NULL,
    tag_ids     BLOB
);
CREATE TABLE track_tag_family (
    axis TEXT NOT NULL, bit INTEGER NOT NULL, name TEXT NOT NULL,
    PRIMARY KEY (axis, bit)
) WITHOUT ROWID;
CREATE TABLE track_tag_vocab (tag_id INTEGER PRIMARY KEY, tag TEXT NOT NULL UNIQUE);
CREATE TABLE genres (genre_id INTEGER PRIMARY KEY, name TEXT NOT NULL);
"""

# Built last, one at a time, exactly the set the engine's queries need. idx_search must keep
# its master.db column order: search.py leans on it being covering AND popularity-led so a
# broad mood filter early-terminates instead of sorting the table.
INDEXES = (
    ("idx_tracks_isrc", "CREATE INDEX idx_tracks_isrc ON tracks(isrc)"),
    ("idx_track_artists_artist_id",
     "CREATE INDEX idx_track_artists_artist_id ON track_artists(artist_id)"),
    ("idx_artists_name", "CREATE INDEX idx_artists_name ON artists(name)"),
    ("idx_artists_name_nocase",
     "CREATE INDEX idx_artists_name_nocase ON artists(name COLLATE NOCASE)"),
    ("idx_search", """CREATE INDEX idx_search ON track_search(
         popularity, energy, valence, danceability, acousticness,
         instrumentalness, tempo, loudness, speechiness, liveness,
         genre_id, release_year)"""),
)


# ── phase 1: select ─────────────────────────────────────────────────────────────
def select_ids(src: sqlite3.Connection, min_pop: int, limit: int | None
               ) -> tuple[np.ndarray, np.ndarray]:
    """(track_ids, popularities) for the popularity head, most popular first.

    One reverse scan of the covering idx_search — no table probe, no sorter. Measured 1.0 s
    for the 2.06M rows at popularity >= 30.
    """
    sql = "SELECT track_id, popularity FROM track_search WHERE popularity >= ? ORDER BY popularity DESC"
    params: list[int] = [min_pop]
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    t = time.time()
    # fetchmany, not fetchall: at --min-pop 1 this is 45M rows, and holding them all as
    # Python tuples before converting is ~3 GB that the numpy arrays then duplicate.
    cur = src.execute(sql, params)
    id_parts: list[np.ndarray] = []
    pop_parts: list[np.ndarray] = []
    while rows := cur.fetchmany(1_000_000):
        id_parts.append(np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows)))
        pop_parts.append(np.fromiter((r[1] for r in rows), dtype=np.int32, count=len(rows)))
    ids = np.concatenate(id_parts) if id_parts else np.empty(0, dtype=np.int64)
    pops = np.concatenate(pop_parts) if pop_parts else np.empty(0, dtype=np.int32)
    del id_parts, pop_parts
    print(f"  selected {ids.size:,} candidates at popularity >= {min_pop} "
          f"[{time.time() - t:.1f}s]")
    return ids, pops


def load_families(bitmap_dir: str) -> tuple[list[tuple[str, int, str]], dict]:
    """The (axis, bit, name) list and mmap'd bitmaps built by backend/build_tag_bitmaps.py."""
    man_path = os.path.join(bitmap_dir, "manifest.json")
    if not os.path.exists(man_path):
        return [], {}
    with open(man_path) as fh:
        man = json.load(fh)
    if man.get("format", "bitmap") != "bitmap":
        return [], {}     # already an --ids build; no use to us here
    fams = [(f["axis"], f["bit"], f["name"]) for f in man["families"]]
    arrs = {(ax, b): np.load(os.path.join(bitmap_dir, f"{ax}_{b}_{n}.npy"), mmap_mode="r")
            for ax, b, n in fams}
    return fams, arrs


def family_counts(ids: np.ndarray, fams: list, arrs: dict) -> dict[str, int]:
    """How many of `ids` carry each family — a pure bitmap test, no database probe."""
    byte, bit = ids >> 3, (ids & 7).astype(np.uint8)
    out = {}
    for ax, b, name in fams:
        arr = arrs[(ax, b)]
        out[f"{ax}/{name}"] = int((((arr[byte] >> bit) & 1) != 0).sum())
    return out


def topup(src: sqlite3.Connection, ids: np.ndarray, pops: np.ndarray, min_pop: int,
          floor: int, bitmap_dir: str) -> tuple[np.ndarray, np.ndarray]:
    """Add lower-popularity tracks until every tag family has at least `floor` members.

    Insurance for a raised --min-pop. Walks idx_search DOWN from min_pop and takes only the
    ids a still-short family claims, so the scan stops as soon as the thinnest family is fed
    rather than dragging the whole tail in.
    """
    fams, arrs = load_families(bitmap_dir)
    if not fams:
        print(f"  family top-up skipped: no bitmaps at {bitmap_dir}")
        return ids, pops
    counts = family_counts(ids, fams, arrs)
    short = {k: floor - v for k, v in counts.items() if v < floor}
    if not short:
        print(f"  family top-up: every family already >= {floor:,} "
              f"(thinnest {min(counts.values()):,})")
        return ids, pops
    print(f"  family top-up: {len(short)} families under {floor:,} — "
          + ", ".join(f"{k} {counts[k]:,}" for k in sorted(short)))

    want = {}   # (axis, bit) -> remaining
    for ax, b, name in fams:
        if (key := f"{ax}/{name}") in short:
            want[(ax, b)] = short[key]

    t, extra_ids, extra_pops = time.time(), [], []
    cur = src.execute("SELECT track_id, popularity FROM track_search "
                      "WHERE popularity < ? ORDER BY popularity DESC", (min_pop,))
    while want:
        rows = cur.fetchmany(200_000)
        if not rows:
            break
        cid = np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows))
        cpop = np.fromiter((r[1] for r in rows), dtype=np.int32, count=len(rows))
        byte, bit = cid >> 3, (cid & 7).astype(np.uint8)
        take = np.zeros(cid.size, dtype=bool)
        for key in list(want):
            hit = ((arrs[key][byte] >> bit) & 1) != 0
            idx = np.flatnonzero(hit)[: want[key]]
            take[idx] = True
            want[key] -= idx.size
            if want[key] <= 0:
                del want[key]
        if take.any():
            extra_ids.append(cid[take])
            extra_pops.append(cpop[take])

    if extra_ids:
        add_i = np.concatenate(extra_ids)
        add_p = np.concatenate(extra_pops)
        # A track can be claimed by several short families; keep one copy of each.
        add_i, uniq = np.unique(add_i, return_index=True)
        add_p = add_p[uniq]
        ids = np.concatenate([ids, add_i])
        pops = np.concatenate([pops, add_p])
        print(f"  family top-up: +{add_i.size:,} tracks [{time.time() - t:.0f}s]")
    return ids, pops


def _mix64(x: np.ndarray) -> np.ndarray:
    """splitmix64 finalizer — a cheap, well-distributed hash of a track_id.

    Sampling by hash rather than by stride: track_id is the source catalogue's insertion
    order, so every k-th row is only "random" if that order is uncorrelated with everything
    we care about, which is not something to bet a slice on. A hash is uniform regardless,
    and it is reproducible — the same --sample-tail N draws the same tracks every run.
    """
    with np.errstate(over="ignore"):
        x = x.astype(np.uint64)
        x ^= x >> np.uint64(30)
        x *= np.uint64(0xBF58476D1CE4E5B9)
        x ^= x >> np.uint64(27)
        x *= np.uint64(0x94D049BB133111EB)
        x ^= x >> np.uint64(31)
    return x


def sample_tail(src: sqlite3.Connection, n: int, min_pop: int,
                have: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """`n` tracks drawn uniformly from BELOW the popularity floor.

    WHY THIS EXISTS
      A popularity threshold bottoms out. 210M of the catalogue's 255M featured tracks sit at
      popularity 0, so `--min-pop 1` already takes everything that has ever been played and
      no lower threshold reaches further. To build a slice bigger than that — or to keep the
      long tail's character rather than only its famous end — the rest has to be sampled.

    HOW
      One sequential scan of track_search by rowid (measured ~107 s for the whole table),
      hash-filtering as it goes. NOT a probe per sampled id: the tail is spread over the
      whole 256M id space, and at the measured 4.2 ms per cold random probe, drawing 20M of
      them would take days. The scan cost is fixed no matter how many are kept.

      The rate is set from an estimate of how many rows are below the floor, oversampling by
      15% and trimming; an undershoot is reported rather than silently returned short.
    """
    total = src.execute("SELECT count(*) FROM track_search").fetchone()[0]
    below = max(total - have.size, 1)
    rate = min(1.0, n * 1.15 / below)
    cutoff = np.uint64(rate * float(2 ** 64)) if rate < 1.0 else np.uint64(2 ** 64 - 1)
    print(f"  tail: {below:,} tracks below popularity {min_pop}, sampling {rate * 100:.3f}% "
          f"for {n:,}")

    max_rowid = src.execute("SELECT max(rowid) FROM track_search").fetchone()[0] or 0
    keep_ids: list[np.ndarray] = []
    keep_pops: list[np.ndarray] = []
    t, lo, got = time.time(), 0, 0
    while lo <= max_rowid:
        rows = src.execute(
            "SELECT track_id, popularity FROM track_search "
            "WHERE rowid >= ? AND rowid < ? AND popularity < ?",
            (lo, lo + ROWID_BATCH, min_pop)).fetchall()
        if rows:
            cid = np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows))
            cpop = np.fromiter((r[1] for r in rows), dtype=np.int32, count=len(rows))
            take = _mix64(cid) < cutoff
            if take.any():
                keep_ids.append(cid[take])
                keep_pops.append(cpop[take])
                got += int(take.sum())
        lo += ROWID_BATCH
        el, pct = time.time() - t, min(lo, max_rowid) / max(max_rowid, 1) * 100
        print(f"  {'tail sample':<22} {got:>11,} drawn  {pct:5.1f}%  "
              f"[{el:5.0f}s, eta {el / pct * (100 - pct) if pct else 0:5.0f}s]",
              end="\r", flush=True)
    print(f"  {'tail sample':<22} {got:>11,} drawn  [{time.time() - t:5.0f}s]" + " " * 24)

    if not keep_ids:
        return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.int32)
    add_i, add_p = np.concatenate(keep_ids), np.concatenate(keep_pops)
    if add_i.size > n:
        # Trim by the same hash, so the kept set is a prefix of a deterministic ordering
        # rather than an arbitrary slice of scan order.
        order = np.argsort(_mix64(add_i))[:n]
        add_i, add_p = add_i[order], add_p[order]
    elif add_i.size < n:
        print(f"    note: asked for {n:,}, the tail yielded {add_i.size:,}")
    return add_i, add_p


# ── phase 2: dedupe ─────────────────────────────────────────────────────────────
_DUPE_SQL = """
    SELECT t.track_id, t.title, t.isrc, group_concat(ar.name, ', ') AS artists
    FROM tracks t
    LEFT JOIN track_artists ta ON ta.track_id = t.track_id
    LEFT JOIN artists ar       ON ar.artist_id = ta.artist_id
    WHERE t.track_id IN ({ph})
    GROUP BY t.track_id
"""


_DEDUPE_CHUNK = 1_000_000


def _dedupe_mask(k_title: np.ndarray, k_isrc: np.ndarray) -> np.ndarray:
    """Which rows to keep, given both key columns already in priority order.

    This is EXACTLY hydrate._Seen's rule — walk the rows in priority order, keep one when
    NEITHER of its keys has been seen, then record both — just made cheap enough for 65M rows.

    The obvious implementation holds two Python sets of 64-bit hashes. At 65M candidates that
    is ~6 GB on a 15 GB box. The fix is not to change the rule but to change the container:
    np.unique maps each distinct key to a dense integer, so "have I seen this key" becomes an
    index into a bytearray instead of a hash-set probe. 65M rows then cost ~1.2 GB total.

    ⚠ The rule is ORDER-DEPENDENT and does not survive being vectorized. Two np.unique passes
    (first-per-title, then first-per-ISRC among survivors) look equivalent and are not: a row
    dropped by its ISRC never records its TITLE, so a later row sharing that title must still
    be kept — and the two-pass form drops it. Tested both ways; the two-pass form also KEEPS
    rows the sequential rule drops. Hence the explicit walk, chunked through .tolist() so the
    inner loop works on Python ints rather than numpy scalars (~10x faster per access).
    """
    n = k_title.size
    keep = np.zeros(n, dtype=bool)
    if not n:
        return keep

    # Dense ids. -1 marks "no ISRC", which never blocks and is never recorded.
    ti = np.unique(k_title, return_inverse=True)[1].astype(np.int64, copy=False)
    has = k_isrc != 0
    ii = np.full(n, -1, dtype=np.int64)
    if has.any():
        ii[has] = np.unique(k_isrc[has], return_inverse=True)[1]

    seen_t = bytearray(int(ti.max()) + 1)
    seen_i = bytearray(int(ii.max()) + 1 if has.any() else 0)
    for start in range(0, n, _DEDUPE_CHUNK):
        tl = ti[start:start + _DEDUPE_CHUNK].tolist()
        il = ii[start:start + _DEDUPE_CHUNK].tolist()
        # Flushed per chunk rather than accumulated: at 58M kept rows one list of Python ints
        # is ~2 GB, which is the same mistake the bytearrays above exist to avoid.
        kept: list[int] = []
        for j, (a, b) in enumerate(zip(tl, il)):
            if seen_t[a] or (b >= 0 and seen_i[b]):
                continue
            kept.append(j)
            seen_t[a] = 1
            if b >= 0:
                seen_i[b] = 1
        if kept:
            keep[start + np.asarray(kept, dtype=np.int64)] = True
    return keep


def dedupe(src: sqlite3.Connection, ids: np.ndarray, pops: np.ndarray) -> np.ndarray:
    """Keep one track per recording — the most popular copy.

    Two passes on purpose. The keys have to be READ in track_id order (sorted PK probes are
    ~10x faster than scattered ones on a 162 GB file), but they have to be APPLIED in
    popularity order (whichever copy is seen first is the one kept, and we want that to be
    the recognisable one). So: probe ascending into hash arrays, then walk descending.

    Keys are 64-bit hashes of the folded (title, artists) pair and of the ISRC, not the
    strings themselves — 2M rows cost 33 MB this way. A collision would drop one random
    track out of the slice; at 2M keys in a 64-bit space the odds are ~1e-7.
    """
    # Everything below indexes into SORTED id order, so a track_id maps to its slot with one
    # vectorized searchsorted per chunk. A {track_id: index} dict would be the obvious way and
    # is what this used to do — at 65M candidates that dict is ~6 GB on a 15 GB box.
    sort_idx = np.argsort(ids, kind="stable")
    sorted_ids = ids[sort_idx]
    sorted_pops = pops[sort_idx]

    h_title = np.zeros(sorted_ids.size, dtype=np.int64)
    h_isrc = np.zeros(sorted_ids.size, dtype=np.int64)
    t0, done = time.time(), 0
    for start in range(0, sorted_ids.size, PROBE_CHUNK):
        chunk = sorted_ids[start:start + PROBE_CHUNK]
        rows = src.execute(_DUPE_SQL.format(ph=",".join("?" * chunk.size)),
                           chunk.tolist()).fetchall()
        if rows:
            got = np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows))
            at = start + np.searchsorted(chunk, got)
            h_title[at] = [hash((fold(r[1] or ""), fold(r[3] or ""))) for r in rows]
            h_isrc[at] = [hash(r[2].strip().upper()) if r[2] else 0 for r in rows]
        done += chunk.size
        if start % (PROBE_CHUNK * 40) == 0 or done >= sorted_ids.size:
            el = time.time() - t0
            pct = done / sorted_ids.size * 100
            print(f"    keys {done:>11,} ({pct:5.1f}%)  [{el:5.0f}s, "
                  f"eta {el / pct * (100 - pct) if pct else 0:5.0f}s]", end="\r", flush=True)
    print()

    # Descending popularity, ascending track_id within a tie — deterministic across runs.
    walk = np.lexsort((sorted_ids, -sorted_pops))
    keep = _dedupe_mask(h_title[walk], h_isrc[walk])
    kept = sorted_ids[walk][keep]
    print(f"  deduped {ids.size:,} -> {kept.size:,} distinct recordings "
          f"({(1 - kept.size / ids.size) * 100:.1f}% were catalog copies) "
          f"[{time.time() - t0:.0f}s]")
    return np.sort(kept)


# ── phase 3: write ──────────────────────────────────────────────────────────────
# WHY EVERY COPY IS A SEQUENTIAL SCAN OF THE SOURCE, NOT A PROBE PER KEPT ID
#   The kept ids are scattered across the full 0–256M id space, so probing master.db for
#   each one is a cold random read. Measured on this box: 4.2 ms per probe — 1.4M of them
#   would be ~1.6 HOURS per table. A rowid-range scan of the same table reads it forward
#   instead: 107 s for all of track_search, 52 s for track_audio_features, 55 s for
#   track_artists. The cost is fixed by the SOURCE's size, not the slice's, so it is the
#   same few minutes whether you keep 30k tracks or 3M.
#
#   CROSS JOIN is load-bearing. With a plain JOIN (or `track_id IN (SELECT …)`) SQLite
#   inverts the loop — "SEARCH s USING INTEGER PRIMARY KEY (rowid=?)", i.e. back to one
#   random probe per kept id. CROSS JOIN pins the source as the outer loop, so the plan is
#   "SEARCH s USING INTEGER PRIMARY KEY (rowid>? AND rowid<?)" + a PK hit on the small,
#   fully-cached _keep table. Do not "simplify" it away.
#
#   track_audio_features shows why the scan has to be over ROWID and not track_id: its rows
#   are in the features file's order, not track_id order (see the schema note in
#   database/schema.sql), so a track_id range would drive idx_taf_track_id and seek per row.
ROWID_BATCH = 25_000_000   # source rowids per INSERT…SELECT — ~5 s of scan, i.e. progress


def _copy_scan(dst: sqlite3.Connection, table: str, key: str, keep: str, label: str,
               cols: tuple[str, str] | None = None) -> int:
    """Copy `table`'s rows whose `key` is in the local `keep` table, by scanning the source.

    `cols` is an optional (dest_columns, select_expr) pair for the compacted tables — the
    projection happens SQL-side, so a narrowed table costs the same scan as a verbatim one.
    """
    max_rowid = dst.execute(f"SELECT max(rowid) FROM src.{table}").fetchone()[0] or 0
    into, select = (f" ({cols[0]})", cols[1]) if cols else ("", "s.*")
    sql = (f"INSERT INTO {table}{into} SELECT {select} FROM src.{table} s "
           f"CROSS JOIN {keep} k ON k.{key} = s.{key} "
           f"WHERE s.rowid >= ? AND s.rowid < ?")
    t, total, lo = time.time(), 0, 0
    while lo <= max_rowid:
        cur = dst.execute(sql, (lo, lo + ROWID_BATCH))
        total += cur.rowcount if cur.rowcount > 0 else 0
        dst.commit()
        lo += ROWID_BATCH
        el, pct = time.time() - t, min(lo, max_rowid) / max(max_rowid, 1) * 100
        print(f"  {label:<22} {total:>11,} rows  {pct:5.1f}%  "
              f"[{el:5.0f}s, eta {el / pct * (100 - pct) if pct else 0:5.0f}s]",
              end="\r", flush=True)
    print(f"  {label:<22} {total:>11,} rows  [{time.time() - t:5.0f}s]"
          + " " * 30)
    return total


def _tracks_columns(dst: sqlite3.Connection) -> tuple[str, str] | None:
    """The projection that packs preview_url, or None when it would not pay.

    Checks the ACTUAL slice rather than trusting the constant: the template only holds if
    every URL starts with the known prefix and carries the same tail. If the catalogue ever
    grows a second shape, this returns None and the column is copied verbatim.
    """
    row = dst.execute(
        "SELECT count(*), "
        "  sum(preview_url LIKE ? || '%'), "
        "  count(DISTINCT substr(preview_url, ?)) "
        "FROM src.tracks WHERE preview_url IS NOT NULL AND rowid < 2000000",
        (PREVIEW_PREFIX, len(PREVIEW_PREFIX) + PREVIEW_HEX_LEN + 1),
    ).fetchone()
    n, matching, tails = row[0] or 0, row[1] or 0, row[2] or 0
    if not n or matching != n or tails != 1:
        print(f"  preview_url: {matching}/{n} match the template, {tails} distinct tails "
              f"— storing verbatim")
        return None
    return (
        "track_id, album_id, isrc, title, popularity, release_date, is_explicit, "
        "duration_ms, preview_url",
        "s.track_id, s.album_id, s.isrc, s.title, s.popularity, s.release_date, "
        "s.is_explicit, s.duration_ms, "
        # unhex() returns NULL if the middle is not clean hex, so the CASE falls through to the
        # original string rather than silently dropping a preview.
        f"COALESCE(unhex(substr(s.preview_url, {len(PREVIEW_PREFIX) + 1}, {PREVIEW_HEX_LEN})), "
        f"         s.preview_url)",
    )


def _preview_template(dst: sqlite3.Connection) -> str | None:
    """`prefix{}tail` — what hydrate.py rebuilds a packed preview_url from."""
    row = dst.execute(
        "SELECT substr(preview_url, ?) FROM src.tracks "
        "WHERE preview_url IS NOT NULL AND rowid < 2000000 LIMIT 1",
        (len(PREVIEW_PREFIX) + PREVIEW_HEX_LEN + 1,),
    ).fetchone()
    return f"{PREVIEW_PREFIX}{{}}{row[0]}" if row else None


def write_embeddings_sidecar(dst: sqlite3.Connection, out_dir: str,
                             ids: np.ndarray) -> tuple[int, int]:
    """Stream the slice's 10-D vectors to flat files for build_demo_index.py.

    Mirrors model_training/embed_tracks.py's `embeddings.f32` / `embed_ids.i64` pair: two
    aligned arrays, written in track_id order by one sequential scan of the source. They are
    a BUILD artifact, not a runtime one — upload_artifacts.py does not ship them, and the
    engine reconstructs a seed vector from the exact index instead (see the COMPACTION note).
    """
    vec_path = os.path.join(out_dir, "demo_vectors.f32")
    ids_path = os.path.join(out_dir, "demo_vector_ids.i64")
    max_rowid = dst.execute("SELECT max(rowid) FROM src.ml_10d_embeddings").fetchone()[0] or 0
    sql = ("SELECT s.track_id, s.vector_blob FROM src.ml_10d_embeddings s "
           "CROSS JOIN _keep k ON k.track_id = s.track_id "
           "WHERE s.rowid >= ? AND s.rowid < ?")
    t, n, lo, dim = time.time(), 0, 0, 0
    with open(vec_path, "wb") as vf, open(ids_path, "wb") as idf:
        while lo <= max_rowid:
            # fetchmany, not fetchall: at a dense slice one 25M-rowid batch is millions of
            # rows, and materializing all their BLOBs at once is ~1 GB of Python objects.
            cur = dst.execute(sql, (lo, lo + ROWID_BATCH))
            while rows := cur.fetchmany(200_000):
                idf.write(np.fromiter((r[0] for r in rows), dtype=np.int64,
                                      count=len(rows)).tobytes())
                for _, blob in rows:
                    vf.write(blob)
                dim = dim or len(rows[0][1]) // 4
                n += len(rows)
            lo += ROWID_BATCH
            el, pct = time.time() - t, min(lo, max_rowid) / max(max_rowid, 1) * 100
            print(f"  {'embeddings → sidecar':<22} {n:>11,} vecs  {pct:5.1f}%  [{el:5.0f}s]",
                  end="\r", flush=True)
    print(f"  {'embeddings → sidecar':<22} {n:>11,} vecs  "
          f"({os.path.getsize(vec_path) / 1e6:.0f} MB)  [{time.time() - t:5.0f}s]" + " " * 20)
    if n < ids.size:
        print(f"    note: {ids.size - n:,} tracks have no embedding — searchable, no similars")
    return n, dim


def write_db(out_db: str, ids: np.ndarray, compact: bool, out_dir: str) -> sqlite3.Connection:
    if os.path.exists(out_db):
        os.remove(out_db)
    # uri=True is load-bearing: URI filenames are a per-CONNECTION flag, so without it the
    # `file:…?mode=ro` in the ATTACH below is taken as a literal path and fails to open.
    dst = sqlite3.connect(out_db, uri=True)
    dst.execute("PRAGMA journal_mode=OFF")      # output is disposable; nothing to roll back to
    dst.execute("PRAGMA synchronous=OFF")
    dst.execute("PRAGMA temp_store=FILE")
    dst.execute("PRAGMA cache_size=-262144")    # ~256 MB
    dst.execute("PRAGMA mmap_size=8000000000")
    dst.execute(f"ATTACH DATABASE 'file:{MASTER}?mode=ro' AS src")
    dst.executescript(SCHEMA)
    if not compact:
        # --no-compact: the naive layout, for A/B-ing a suspicion about the compaction.
        dst.executescript("""
            DROP TABLE track_audio_features;
            CREATE TABLE track_audio_features (
                track_id INTEGER PRIMARY KEY,
                danceability REAL, energy REAL, "key" INTEGER, loudness REAL, mode INTEGER,
                speechiness REAL, acousticness REAL, instrumentalness REAL, liveness REAL,
                valence REAL, tempo REAL, time_signature INTEGER, camelot_code TEXT);
            CREATE TABLE ml_10d_embeddings (
                track_id INTEGER PRIMARY KEY, vector_blob BLOB NOT NULL,
                model_version TEXT NOT NULL);
        """)
    dst.execute("CREATE TABLE _keep (track_id INTEGER PRIMARY KEY)")
    dst.executemany("INSERT INTO _keep VALUES (?)", ((int(i),) for i in ids))
    dst.commit()

    meta: dict[str, str] = {}
    tracks_cols = _tracks_columns(dst) if compact else None
    if tracks_cols and (tpl := _preview_template(dst)):
        meta["preview_template"] = tpl
        print(f"  preview_url packed → 20-byte blob, template {tpl[:44]}…")

    tables = list(TRACK_TABLES) + ([] if compact else ["ml_10d_embeddings"])
    for table in tables:
        if not dst.execute("SELECT 1 FROM src.sqlite_master WHERE type='table' AND name=?",
                           (table,)).fetchone():
            print(f"  {table:<22} absent in master.db — skipped")
            continue
        cols = tracks_cols if table == "tracks" else (
            COMPACT_COLUMNS.get(table) if compact else None)
        _copy_scan(dst, table, "track_id", "_keep", table, cols)

    if compact:
        write_embeddings_sidecar(dst, out_dir, ids)

    # The dimension tables are keyed by album_id / artist_id, so their keep-sets come from
    # what was just written locally — then the same scan-the-source rule applies to them.
    dst.execute("CREATE TABLE _keep_album (album_id INTEGER PRIMARY KEY)")
    dst.execute("INSERT INTO _keep_album SELECT DISTINCT album_id FROM tracks "
                "WHERE album_id IS NOT NULL")
    dst.execute("CREATE TABLE _keep_artist (artist_id INTEGER PRIMARY KEY)")
    dst.execute("INSERT INTO _keep_artist SELECT DISTINCT artist_id FROM track_artists")
    dst.commit()
    _copy_scan(dst, "albums", "album_id", "_keep_album", "albums")
    _copy_scan(dst, "artists", "artist_id", "_keep_artist", "artists")

    for table in WHOLE_TABLES:
        if dst.execute("SELECT 1 FROM src.sqlite_master WHERE type='table' AND name=?",
                       (table,)).fetchone():
            dst.execute(f"INSERT INTO {table} SELECT * FROM src.{table}")
    dst.commit()

    meta["compact"] = "1" if compact else "0"
    meta["built_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    dst.executemany("INSERT OR REPLACE INTO demo_meta VALUES (?,?)", list(meta.items()))

    for scratch in ("_keep", "_keep_album", "_keep_artist"):
        dst.execute(f"DROP TABLE {scratch}")
    dst.commit()
    return dst


def build_indexes(dst: sqlite3.Connection) -> None:
    for name, sql in INDEXES:
        t = time.time()
        dst.execute(sql)
        dst.commit()
        print(f"  {name:<28} [{time.time() - t:5.0f}s]")
    t = time.time()
    dst.execute("ANALYZE main")     # `ANALYZE` alone would try to write the read-only src
    dst.commit()
    print(f"  {'ANALYZE':<28} [{time.time() - t:5.0f}s]")


# ── report ──────────────────────────────────────────────────────────────────────
_PLAN_CHECKS = (
    ("F1 mood filter",
     "SELECT track_id FROM track_search WHERE valence < 300 AND energy < 500 "
     "ORDER BY popularity DESC LIMIT 300"),
    ("F6 pool probe",
     "SELECT track_id, popularity, genre_id, tempo, release_year FROM track_search "
     "WHERE track_id IN (1,2,3)"),
    ("hydrate join",
     "SELECT t.track_id, group_concat(ar.name) FROM tracks t "
     "LEFT JOIN track_artists ta ON ta.track_id=t.track_id "
     "LEFT JOIN artists ar ON ar.artist_id=ta.artist_id "
     "WHERE t.track_id IN (1,2,3) GROUP BY t.track_id"),
)


def report(dst: sqlite3.Connection, out_db: str, args: argparse.Namespace,
           fam_counts: dict[str, int]) -> dict:
    # ml_10d_embeddings is only present on a --no-compact build; the existence filter below
    # keeps it out of the counts otherwise (and out of the "in the sidecar" branch's way).
    counts = {t: dst.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
              for t in (*TRACK_TABLES, "ml_10d_embeddings", "albums", "artists", *WHOLE_TABLES)
              if dst.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                             (t,)).fetchone()}
    size = os.path.getsize(out_db)
    print("\n  row counts")
    for k, v in counts.items():
        print(f"     {k:<24} {v:>12,}")
    print(f"\n  demo.db {size / 1e9:.2f} GB "
          f"({size / max(counts.get('tracks', 1), 1):.0f} bytes/track)")

    print("\n  query plans (each must stay index-driven — see search.py / similar.py)")
    for label, sql in _PLAN_CHECKS:
        for row in dst.execute("EXPLAIN QUERY PLAN " + sql):
            print(f"     {label:<16} {row[-1]}")

    if fam_counts:
        print("\n  tag families in the slice (F6 gate needs >= 40 in a 1500-wide pool)")
        for k in sorted(fam_counts):
            print(f"     {k:<30} {fam_counts[k]:>10,}")

    trk = counts.get("tracks", 1)
    emb = counts.get("ml_10d_embeddings")
    if emb is None:
        sc = os.path.join(os.path.dirname(out_db), "demo_vector_ids.i64")
        emb = os.path.getsize(sc) // 8 if os.path.exists(sc) else 0
        where = "sidecar (demo_vectors.f32)"
    else:
        where = "demo.db"
    print(f"\n  embeddings: {emb:,}/{trk:,} ({emb / trk * 100:.1f}%) in the {where}"
          f" — tracks without one are searchable but have no similars")

    return {
        "built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source_db": os.path.basename(MASTER),
        "min_popularity": args.min_pop,
        "deduped": not args.no_dedupe,
        "family_floor": args.family_floor,
        "compact": not args.no_compact,
        "sample_tail": args.sample_tail,
        "tracks": counts.get("tracks", 0),
        "artists": counts.get("artists", 0),
        "albums": counts.get("albums", 0),
        "embeddings": emb,
        "rows": counts,
        "families": fam_counts,
        "demo_db_bytes": size,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--min-pop", type=int, default=30,
                    help="popularity floor for the slice (default 30 → ~2.06M candidates)")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap the candidate count (most popular first)")
    ap.add_argument("--family-floor", type=int, default=15_000,
                    help="top up any tag family below this many members (0 disables)")
    ap.add_argument("--no-dedupe", action="store_true",
                    help="keep catalog copies instead of collapsing them")
    ap.add_argument("--sample-tail", type=int, default=0, metavar="N",
                    help="also draw N tracks uniformly from BELOW the popularity floor. "
                         "210M of 255M tracks sit at popularity 0, so this is the only way "
                         "past --min-pop 1 (~38.6M) and the only way to keep the long tail's "
                         "character rather than just its famous end")
    ap.add_argument("--no-compact", action="store_true",
                    help="write the naive layout: full track_audio_features, unpacked "
                         "preview_url, embeddings in the db (~751 B/track vs ~505)")
    ap.add_argument("--smoke", action="store_true",
                    help="30k tracks, no top-up — end-to-end validation in ~1 min")
    ap.add_argument("--out", default=None, help=f"output directory (default {OUT_DIR})")
    args = ap.parse_args()

    out_dir = args.out or OUT_DIR
    # The idx_search sort and the DISTINCT temp b-trees spill here. Left unset, SQLite uses
    # /tmp — which on some systems is a RAM disk, and a 58M-row index sort would then be an
    # OOM instead of a spill. Same discipline as build_track_search.py.
    tmpdir = os.path.join(ROOT, "_build_tmp")
    os.makedirs(tmpdir, exist_ok=True)
    os.environ["SQLITE_TMPDIR"] = tmpdir
    if args.smoke:
        args.limit = args.limit or 30_000
        args.family_floor = 0
    os.makedirs(out_dir, exist_ok=True)
    out_db = os.path.join(out_dir, "demo.db")

    print(f"zinthos demo slice{'  (SMOKE)' if args.smoke else ''}")
    print(f"  source {MASTER}")
    print(f"  output {out_db}\n")

    src = sqlite3.connect(f"file:{MASTER}?mode=ro", uri=True)
    src.execute("PRAGMA query_only=ON")
    src.execute("PRAGMA mmap_size=20000000000")
    src.execute("PRAGMA cache_size=-400000")

    t_all = time.time()
    print("phase 1 — select")
    ids, pops = select_ids(src, args.min_pop, args.limit)
    if ids.size == 0:
        sys.exit("no candidates — is track_search built?")
    if args.family_floor:
        ids, pops = topup(src, ids, pops, args.min_pop, args.family_floor, BITMAP_DIR)
    if args.sample_tail:
        add_i, add_p = sample_tail(src, args.sample_tail, args.min_pop, ids)
        if add_i.size:
            ids = np.concatenate([ids, add_i])
            pops = np.concatenate([pops, add_p])
            # The tail draw cannot collide with the head (it only reads popularity < min_pop)
            # but the family top-up may have already taken some of the same rows.
            ids, uniq = np.unique(ids, return_index=True)
            pops = pops[uniq]
            print(f"  slice now {ids.size:,} candidates")

    print("\nphase 2 — dedupe")
    if args.no_dedupe:
        ids = np.sort(ids)
        print("  skipped (--no-dedupe)")
    else:
        ids = dedupe(src, ids, pops)

    fams, arrs = load_families(BITMAP_DIR)
    fam_counts = family_counts(ids, fams, arrs) if fams else {}

    print(f"\nphase 3 — write {ids.size:,} tracks"
          f"{'' if args.no_compact else '  (compact layout)'}")
    dst = write_db(out_db, ids, not args.no_compact, out_dir)

    print("\nphase 4 — indexes")
    build_indexes(dst)

    print("\nphase 5 — report")
    manifest = report(dst, out_db, args, fam_counts)
    with open(os.path.join(out_dir, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=1)
    dst.close()
    print(f"\n✓ demo slice built in {time.time() - t_all:.0f}s → {out_db}")
    print("  next: demo/build_demo_index.py, then the match sidecar and tag ids "
          "(see demo/README.md)")


if __name__ == "__main__":
    main()
