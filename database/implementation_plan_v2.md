# Master DB — Implementation Plan **v2**

> Staff review of `implementation_plan.md`. I verified every assumption against the live
> source databases, the schema, and the host before writing this. Sections marked
> **🔴 HOLE** are correctness/OOM/perf defects in v1 that would have bitten you.
> Sections marked **🟢 FIX** are the v2 replacement.

---

## 0. Ground Truth (verified, not assumed)

I inspected the real sources on `2026-06-18`. Numbers below are from `max(rowid)` / `.schema`, not estimates.

| Source | Path | Verified rows | Notes |
|--------|------|--------------:|-------|
| `source_a.sqlite3` (125 GB) | `/run/media/zer0/7FE7-F0D8/Databases/` | tracks **256,039,007**, artists **15,430,442**, albums **58,591,047**, track_artists **348,055,756**, album_images **175,809,992**, artist_genres **2,229,947** | |
| `source_a_audio_features.sqlite3` (39 GB) | same dir | track_audio_features **255,597,711** | keyed by `track_id` TEXT, has `null_response` flag |
| `source_b.csv` (110 GB) | same dir | ~177.8M lines | 51 columns, RFC-quoted |
| `source_a_track_files.sqlite3` (105 GB) | same dir | — | **Not in v1 inventory.** Confirm it is genuinely unused before relying on that. |

**Host reality (corrected):**
- RAM: 16 GB. ✅ matches.
- `/home` (NVMe, build target): **246 GB free**, *not* 306 GB as v1 claims. 🔴 Biggest space risk — see §6.
- **Source disk: now internal NVMe (`/dev/nvme1n1p1`), separate device from `/home`** (done 2026-06-18).
  Mount path is unchanged: `/run/media/zer0/7FE7-F0D8/Databases/`.
- 🔴 **CRITICAL — the source filesystem is exfat (FUSE), and that, not the bus, governs performance.**
  Benchmarked on the internal NVMe (2026-06-18):
  - **Random** reads into the source are SLOW: 200k id→rowid probes into `sp.tracks` = **23.6 s default
    / 12.0 s even with mmap+4 GB cache** (~17k/s) → the full 256M remap would be **~4 h**. Each random
    read pays a FUSE userspace round-trip (the time is ~all sys + IO-wait, <1 s user).
  - **Sequential** reads are FAST: 2M-row scan = 0.33 s (~6M rows/s). Sequential is fine.
  - **Routing the same probe to a `/home` ext4 index = 0.33 s for 200k (~600k/s)** — ~35× faster.
- 🟢 **GOVERNING PRINCIPLE (drives the whole builder): read source tables SEQUENTIALLY only; perform
  every random-access join against a `/home` (ext4, `master.db`) table — never against an exfat source
  table.** This makes the `/home`-routed remap the *primary* path (not a fallback), and it also reshapes
  Phases 1 and 3 (see those phases). With this, expected total build ≈ **3–5 h**; without it, the random
  exfat probes alone blow past 8 h.
- *Avoid gratuitous re-scans of source* — v1's up-to-62 full passes (H1) were eliminated for
  correctness/RAM reasons regardless of disk speed.

### Real source schemas (the parts v1 got wrong)

```
artists(rowid PK, id TEXT, fetched_at, name, followers_total, popularity)        -- unique idx on id
albums (rowid PK, id TEXT, name, album_type, ..., label, popularity, release_date, total_tracks)
tracks (rowid PK, id TEXT, name, preview_url, album_rowid, track_number,
        external_id_isrc, popularity, ..., duration_ms, explicit)                -- NO release_date column
track_artists(track_rowid, artist_rowid)                                         -- NO position, NO PK, NO extra cols
artist_genres(artist_rowid, genre)
album_images(album_rowid, width, height, url)                                    -- 175.8M rows, ~3 per album
track_audio_features(rowid PK, track_id TEXT, fetched_at, null_response,
        duration_ms, time_signature, tempo INTEGER, key, mode,
        danceability REAL, energy, loudness, speechiness, acousticness,
        instrumentalness, liveness, valence)                                     -- unique idx on track_id
```

---

## 1. Critical Holes in v1 (ranked by severity)

### 🔴 H1 — Phase 3's sharded in-memory join is unnecessary *and* re-scans USB up to 62×
v1 shards Phase 3 by first character of `track_id` and, per shard, **full-scans both** the 39 GB
features DB and the 125 GB tracks DB to build an in-memory `unordered_map`. Source A IDs are base62,
so first-char sharding implies up to 62 shards → potentially **62 full scans of each source over USB**
(several TB of reads) and an OOM-prone 8–10 GB map per shard. The memory math is also internally
inconsistent (`~32M/shard` implies 8 shards, not 62).

**Root cause of the mistake:** `tracks` and `track_audio_features` are **separate target tables**
sharing the same PK (`track_id`). They are *never joined* during the build. There is **no reason to
join the two sources at all.** v1 invented a join that the schema doesn't require.

### 🔴 H2 — `track_artists.artist_position` cannot be derived; source has no ordering
Target `track_artists.artist_position INTEGER` and v1 Phase 4 compute a position counter. But the
source `track_artists(track_rowid, artist_rowid)` has **no position/order column and no primary key**.
Any "position" you assign is an artifact of physical row order, which SQLite does not guarantee to be
meaningful. Decide explicitly: (a) drop `artist_position` as meaningless, or (b) define it as
"ascending source-rowid order within a track" and document that it is *not* Source A's credited order.

### 🔴 H3 — Plan ↔ schema mismatches that will not compile/insert
- v1 Phase 5 does `UPDATE track_audio_features SET replay_gain=?` — **there is no `replay_gain`
  column** in `schema.sql`. Either add it or drop the step.
- v1 Phase 5 inserts `source_b_genres(track_id, raw_genre, mapped_genre)` — schema has only
  `source_b_genres(track_id, genre)`. **No raw/mapped columns exist.**
- This directly contradicts v1's own "Data Lineage" principle (keep raw vs mapped separate). The
  schema can't express it. **Pick one** (see §3).

### 🔴 H4 — `tracks.release_date` has no source
Target `tracks.release_date` exists; **source `tracks` has no release_date column** (confirmed). The
date lives on `albums.release_date` (and on Source B's `TrackReleaseDate`). v1 never populates it.

### 🔴 H5 — Index ordering bug: Phase 5 needs `idx_tracks_isrc`, but indexes are built in Phase 7
Phase 5 relies on `idx_tracks_isrc` for ISRC lookups, yet v1 creates all indexes in Phase 7 (after
Phase 5). As written, every Source B lookup is a full table scan of 256M rows. The ISRC index must
exist **before** the Source B phase.

### 🔴 H6 — Source B ISRC join is many-to-many; PK conflicts guaranteed
- One ISRC → **many** Source A `track_id`s (re-releases/markets). `WHERE isrc=?` returns multiple rows.
- Many Source B rows → **same** ISRC (same recording on multiple albums) with possibly *different*
  `AlbumGenreName`. Since `source_b_genres.track_id` is the PK, you get conflicts and
  nondeterministic "winner". Need an explicit `INSERT ... ON CONFLICT` policy (e.g., first-non-empty,
  or highest `TrackRank`).
- Source B genre is **album-level (`AlbumGenreName`) and frequently empty** (first CSV row has empty
  genre, `AlbumGenreId=-1`). Filtering empties is what yields ~141.67M, not 177.8M.

### 🔴 H7 — Disk budget is wrong and ignores WAL + index temp space
v1 says "306 GB free … fits easily." Real free = **246 GB**. Output est ~86 GB, but:
- WAL in `journal_mode=WAL` grows toward DB size during bulk load unless checkpointed → can transiently
  *double* on-disk footprint.
- `CREATE INDEX` on 256M/348M-row tables runs an **external merge sort in temp**; with
  `temp_store=MEMORY` that OOMs, with `temp_store=FILE` it needs tens of GB of scratch.
- Net: the build can spike well past 86 GB. Must be actively managed (§6).

### 🟠 H8 — `null_response` / NULL features unaddressed
A large share of `track_audio_features` rows have `null_response=1` with NULL `key`/`mode`/`tempo`/etc.
Camelot computation and downstream ML must handle these; v1's camelot phase assumes key/mode exist.

### 🟠 H9 — `tempo` is INTEGER in source, REAL in target; CSV parsing must be quote-aware
- Source `tempo` is `INTEGER` (precision already lost upstream); target is `REAL`. Cosmetic but note it.
- The Source B CSV has RFC-4180 quoted fields containing commas (e.g. `"  J2O"`, album titles). A naive
  `split(',')` **will mis-align every column after the first quoted field.** Use a real CSV parser.

### 🟠 H10 — `synchronous=OFF` over a multi-hour build = unrecoverable corruption, no resumability
A full build is many hours over USB. With `synchronous=OFF`/`journal_mode=OFF`, any crash/power loss
corrupts `master.db` and forces a restart from zero. v1 has no resumability story.

### 🟡 H11 — In-memory maps are larger than v1's estimates
- Phase 1 album-image map: 58.6M kept entries built by scanning **175.8M** rows; at ~80–120 B/entry
  that's ~6–7 GB — right at the edge, and avoidable (§4 P1).
- Phase 2/4 `artist rowid→id` as `vector<std::string>` of 15.4M × (string overhead ~32 B + 22 B) ≈
  0.8–1.5 GB; v1's "2 GB" is plausible but the `track rowid→id` map for **256M** tracks (5–6 GB) is
  *not budgeted* and is only avoided if you let the **source** do the join (§4 P4).

---

## 2. v2 Architecture Principle: let SQLite move the bytes

The cleanest, fastest, lowest-RAM builder for the Source A side is **not** a row-by-row C++ marshaller.
ATTACH the source DBs to the `master.db` connection and run `INSERT INTO main.<t> SELECT … FROM
src.<t>`. This runs at IO speed inside SQLite's C core, needs ~0 heap, and lets the source's existing
PK/indexes do all joins. C++ is reserved for the one thing SQL can't do well: **parsing the 110 GB
Source B CSV.**

- 🟢 No in-memory join maps anywhere (kills H1, most of H11).
- 🟢 Camelot is a pure SQL `CASE` computed at insert time — **no separate Phase 6, no extra pass** (H8 handled with `WHEN key IS NULL`).
- 🟢 Single pass per source table.

C++/`sqlite3` C API is still used to drive the connection, set PRAGMAs, run the CSV phase, manage
checkpoints, and record phase progress for resumability. This satisfies the "C++ ETL" mandate while
using SQL for bulk movement.

---

## 3. Schema decisions — RESOLVED (2026-06-18) and baked into `schema.sql`

These were open in the first v2 draft; they are now decided and reflected in the rewritten `schema.sql`.

1. **Key strategy → INTEGER surrogate keys.** `master.db` reuses the source `rowid`s as PKs
   (`tracks.rowid`, `artists.rowid`, `albums.rowid`). The Source A text id lives in `track_mappings`
   (platform='source_a') plus `source_a_id` on `artists`/`albums`. This shrinks the DB by tens of GB and
   — crucially — makes Phases 4 & 7 *simpler* (the source's `track_artists`/`artist_genres` already
   store the exact integer rowids we want; see §4). The **only** added cost is remapping
   `track_audio_features` from its text `track_id` to the integer key (one indexed pass, §4 P3).
2. **Source B genre lineage → store RAW `AlbumGenreName`** in `source_b_genres.genre`; the 20-genre
   consolidation happens in the Python ML layer (dict/map), keeping the master DB auditable.
   Conflict policy (1:N ISRC, H6): `ON CONFLICT(track_id) DO UPDATE SET genre=excluded.genre
   WHERE source_b_genres.genre=''` (first non-empty wins; or switch to highest `TrackRank`).
3. **`replay_gain` → DROPPED.** Redundant with `loudness` (already a feature) and Source B gain is
   album-level/noisy. Column removed from schema; Phase-5 sub-step deleted.
4. **`tracks.release_date` → from `albums.release_date`** at track-insert time (single pass via the
   `album_rowid` link). No Source B override needed.
5. **`artist_position` → kept, documented as source-order rank** (not authoritative credited order).
6. **`track_files` (105 GB) → DEFERRED.** Not in the core build. Language/instrumental search becomes an
   optional post-build `track_languages(track_id, lang, has_lyrics)` enrichment pass if wanted later.
   (`language_of_performance` ~72% coverage, top token `"zxx"` = instrumental.)
7. **New columns added** (cheap, high value): `tracks.preview_url` (~87% coverage, enables playback),
   `artists.popularity` + `artists.followers_total` (F3 ranking), `albums.release_date`, and
   `source_a_id` on `artists`/`albums` for external linking.
8. **`WITHOUT ROWID`** applied to the pure-junction tables (`track_mappings`, `track_artists`,
   `artist_genres`) to drop the redundant rowid btree on 348M+ rows.

---

## 4. Build Phases (v2)

> Global setup: open `master.db`, apply `schema.sql` **without indexes**, set bulk PRAGMAs (§5),
> `ATTACH '…/source_a.sqlite3' AS sp; ATTACH '…/source_a_audio_features.sqlite3' AS af;`.
> After each phase: `wal_checkpoint(TRUNCATE)` and record completion (§7).

### Phase 1 — Albums (58.6M) 🟢 FIX (exfat-aware: no per-album random probes)
`album_id` = source `albums.rowid`. ⚠️ The obvious correlated subquery `(SELECT url FROM sp.album_images
WHERE album_rowid=a.rowid …)` would do **58.6M random probes into exfat** (slow). Instead, do it in two
**sequential** passes routed through `/home`:
1. Sequential scan `sp.album_images` (175.8M rows), aggregating the best image per album into a `/home`
   temp/staging table (GROUP BY spills to `SQLITE_TMPDIR` on `/home`, not exfat):
```sql
CREATE TABLE main._album_cover AS
SELECT album_rowid, url FROM (
  SELECT album_rowid, url,
         ROW_NUMBER() OVER (PARTITION BY album_rowid ORDER BY width*height DESC) rn
  FROM sp.album_images)              -- single sequential scan of album_images
WHERE rn = 1;
-- (no extra index needed: album_rowid below is matched via the albums sequential scan)
```
2. Sequential scan `sp.albums`, LEFT JOIN the `/home` cover table:
```sql
INSERT INTO main.albums(album_id, source_a_id, title, album_type, release_date, cover_art_url)
SELECT a.rowid, a.id, a.name, a.album_type, a.release_date, c.url
FROM sp.albums a LEFT JOIN main._album_cover c ON c.album_rowid = a.rowid;
DROP TABLE main._album_cover;
```
(The `ROW_NUMBER` window sorts 175.8M rows in `/home` temp — bounded, sequential source read. If you
prefer C++: stream `album_images` sequentially and keep a `vector` of best-(w*h,url) keyed by
`album_rowid` (~58.6M entries, <1 GB) — also fine, no exfat random reads either way.)

### Phase 2 — Artists + artist_genres (15.4M + 2.2M) 🟢 (now pure copies — no joins)
`artist_id` = source `artists.rowid`, so `artist_genres` copies straight across (it already stores
`artist_rowid`):
```sql
INSERT INTO main.artists(artist_id, source_a_id, name, popularity, followers_total)
SELECT rowid, id, name, popularity, followers_total FROM sp.artists;

INSERT OR IGNORE INTO main.artist_genres(artist_id, genre)
SELECT artist_rowid, genre FROM sp.artist_genres;     -- no JOIN needed anymore
```

### Phase 3 — Tracks + audio features (256M) 🟢 FIX (exfat-aware: all random access routed to /home)
Three sub-steps **in this order** (3a → 3b → build index → 3c). `track_id` = source `tracks.rowid`,
`album_id` = source `tracks.album_rowid` (direct, no lookup).

**3a. tracks** — sequential scan of `sp.tracks`; `release_date` from `main.albums` (built in Phase 1, on
**/home** — the random join target is ext4, not exfat). `preview_url` kept (~87% coverage):
```sql
INSERT INTO main.tracks(track_id, album_id, isrc, title, popularity, release_date,
                        is_explicit, duration_ms, preview_url)
SELECT t.rowid, t.album_rowid, t.external_id_isrc, t.name, t.popularity, ma.release_date,
       t.explicit, t.duration_ms, t.preview_url
FROM sp.tracks t                                   -- sequential exfat read
LEFT JOIN main.albums ma ON ma.album_id = t.album_rowid;   -- random probe into /home ext4 (fast)
```

**3b. track_mappings** — sequential scan; this is the Rosetta Stone AND the /home probe target for 3c:
```sql
INSERT INTO main.track_mappings(track_id, platform, platform_id)
SELECT rowid, 'source_a', id FROM sp.tracks;        -- pure sequential, no join
```

**Build `idx_track_mappings_platform_id` NOW** (build-early, on /home) — 3c probes it.

**3c. audio features** — the `id`→integer remap. `af.track_audio_features` is keyed by Source A *text*
id. 🔴 Do **NOT** `JOIN sp.tracks` (that's 256M random probes into exfat ≈ 4 h, benchmarked §0).
Instead scan `af` sequentially and probe `main.track_mappings` on **/home** (≈35× faster):
```sql
INSERT INTO main.track_audio_features(track_id, danceability, energy, "key", loudness, mode,
       speechiness, acousticness, instrumentalness, liveness, valence, tempo, time_signature, camelot_code)
SELECT m.track_id, af.danceability, af.energy, af."key", af.loudness, af.mode, af.speechiness,
       af.acousticness, af.instrumentalness, af.liveness, af.valence, af.tempo, af.time_signature,
       CASE
         WHEN af."key" IS NULL OR af.mode IS NULL OR af."key" < 0 THEN NULL  -- Source A key=-1 means "no key"
         WHEN af.mode = 1 THEN CASE af."key"   -- major → 'xB'
           WHEN 0 THEN '8B'  WHEN 1 THEN '3B'  WHEN 2 THEN '10B' WHEN 3 THEN '5B'
           WHEN 4 THEN '12B' WHEN 5 THEN '7B'  WHEN 6 THEN '2B'  WHEN 7 THEN '9B'
           WHEN 8 THEN '4B'  WHEN 9 THEN '11B' WHEN 10 THEN '6B' WHEN 11 THEN '1B' END
         ELSE CASE af."key"                     -- minor (mode=0) → 'xA'
           WHEN 0 THEN '5A'  WHEN 1 THEN '12A' WHEN 2 THEN '7A'  WHEN 3 THEN '2A'
           WHEN 4 THEN '9A'  WHEN 5 THEN '4A'  WHEN 6 THEN '11A' WHEN 7 THEN '6A'
           WHEN 8 THEN '1A'  WHEN 9 THEN '8A'  WHEN 10 THEN '3A' WHEN 11 THEN '10A' END
       END AS camelot_code
FROM af.track_audio_features af                              -- sequential exfat read
JOIN main.track_mappings m ON m.platform_id = af.track_id    -- random probe into /home ext4 (fast)
WHERE af.null_response = 0;
```
The camelot `CASE` above is final/copy-pasteable (verified: C-major→8B, A-minor→8A). Note the `"key"`
quoting (reserved word) and the `key < 0` guard.

> **Orphan policy:** `null_response=1` rows are skipped (no features). Tracks without a features row are
> still inserted; downstream ML simply filters on the presence of a `track_audio_features` row.

### Phase 4 — track_artists (348M) 🟢 FIX (now a direct two-column copy — no joins at all)
With integer keys, the source `track_artists(track_rowid, artist_rowid)` columns **are already** our
`(track_id, artist_id)`. No joins, no maps:
```sql
INSERT OR IGNORE INTO main.track_artists(track_id, artist_id, artist_position)
SELECT track_rowid, artist_rowid, <position>
FROM sp.track_artists;
```
`artist_position` (H2): compute in the C++ driver with a `vector<uint16_t>` of size `max(track_rowid)`
(~256M × 2 B ≈ **512 MB**, the only sizeable allocation in the whole build), incrementing per
`track_rowid` as rows stream by. Don't `ORDER BY` in SQL (forces a 348M-row temp sort). `INSERT OR
IGNORE` absorbs duplicate `(track_id, artist_id)` PK collisions present in real data. (If you don't
need `artist_position`, this phase becomes a pure `INSERT…SELECT` with zero C++ and zero RAM.)

### Phase 5 — Source B CSV (~178M lines) 🟢 C++, quote-aware, after the ISRC index
**Move `CREATE INDEX idx_tracks_isrc` to BEFORE this phase (fixes H5).** Then:
1. Stream the CSV with an **RFC-4180-aware parser** (quote/escape handling — H9). Extract
   `TrackISRC`, `AlbumGenreName`, `TrackReleaseDate`, `TrackGain` by header-resolved column index.
2. Skip rows with empty `TrackISRC` or empty `AlbumGenreName`.
3. For each row, prepared `SELECT track_id FROM tracks WHERE isrc=?` — returns the integer key(s);
   **iterate all matches** (1:N, H6).
4. `INSERT … ON CONFLICT(track_id) DO UPDATE … WHERE genre=''` per the policy in §3.2. (No
   `replay_gain` step — that column was dropped, §3.3.)
5. Batch in transactions of 200k–500k; `wal_checkpoint(TRUNCATE)` periodically.

> Perf reality check (H1/USB): the CSV read is sequential off USB (fine), but each of ~178M rows does a
> random NVMe index probe. v1's "200k lookups/sec" is optimistic; budget for 30–90 min and verify with a
> 5M-row dry run before committing to the full pass. Consider de-duping ISRCs in a first pass if probe
> cost dominates.

### Phase 6 — *(eliminated)* Camelot folded into Phase 3.

### Phase 7 — Indexes, aggregates, finalize
Order matters and most disk-temp is spent here (§6):
1. **Two indexes are already built earlier — do NOT rebuild:** `idx_track_mappings_platform_id` (in
   Phase 3b, before the audio-features remap) and `idx_tracks_isrc` (before Phase 5). Both are required
   *early* because the exfat source forces those random joins onto `/home` (§0).
2. Build the remaining indexes from `schema.sql` **one at a time**, checkpointing between each so
   WAL/temp don't accumulate.
3. (Optional, not in current schema) `artists.track_count`: if you decide you want it, add the column
   and fill it with **one grouped pass**, not correlated subqueries — materialize
   `SELECT artist_id, COUNT(*) FROM track_artists GROUP BY artist_id` into a temp table and join-update.
4. `PRAGMA analysis_limit=1000; PRAGMA optimize; ANALYZE;` — `analysis_limit` avoids a full-table
   stats scan on 256M rows.
5. `PRAGMA wal_checkpoint(TRUNCATE); PRAGMA journal_mode=DELETE;` to leave a clean, single-file DB.
6. (Recommended) `PRAGMA integrity_check;` and `PRAGMA foreign_key_check;` before declaring done.

---

## 5. PRAGMA / connection tuning (bulk-load profile)

```sql
PRAGMA page_size = 32768;        -- set ONCE before schema; fewer btree levels for 256M rows
PRAGMA journal_mode = OFF;       -- DB is fully rebuildable; OFF avoids WAL disk blow-up + is fastest
PRAGMA synchronous = OFF;        -- acceptable ONLY because each phase is replayable (see §7)
PRAGMA temp_store = FILE;        -- index sorts spill to disk, NOT RAM (prevents OOM, H7)
PRAGMA cache_size = -2000000;    -- ~2 GB page cache; leave headroom for the 512 MB position vector
PRAGMA mmap_size = 30000000000;  -- 30 GB mmap reads on NVMe; page-cache backed, not RSS
PRAGMA foreign_keys = OFF;       -- (default) never enforce FKs during bulk load
PRAGMA cache_spill = TRUE;
```
Set `SQLITE_TMPDIR=/home/zer0/.../tmp` so index-build scratch lands on the 246 GB `/home` NVMe, never on
the source disk or a small `/tmp`. Compile the builder `-O3`. Use one prepared statement per phase, bind in a
tight loop, commit every 200k–500k rows.

> Note: `journal_mode=OFF` + `synchronous=OFF` means a crash mid-phase can corrupt the file. That's why
> §7 makes phases **idempotent and replayable** — on crash you re-run from the last completed phase
> rather than trusting the file.

---

## 6. Disk budget (the real constraint)

Integer keys cut the v1 text-key estimates substantially (the 22-char id is gone from `tracks`,
`track_audio_features`, `track_artists`×~1.4, `track_mappings`, `source_b_genres`; indexes shrink
too). Conservative revised budget:

| Item | Text-key est. (v1) | Integer-key est. (v2) |
|------|-----:|-----:|
| `/home` free today | — | **246 GB** |
| Final `master.db` tables | ~70 GB | **~45–55 GB** |
| All indexes | ~15–20 GB | **~8–12 GB** |
| Index-build temp scratch (external sort, peak) | +20–40 GB | **+15–30 GB transient** |
| WAL (avoided by `journal_mode=OFF`) | ~0 | ~0 |

Peak transient ≈ 55 + 12 + 30 ≈ **~95 GB**, comfortably under 246 GB **only if** you (a) keep
`journal_mode=OFF`, (b) build indexes one-at-a-time, (c) keep `SQLITE_TMPDIR` on `/home`. Do **not** let
a WAL file coexist with index temp — that's the path to ENOSPC mid-build. Re-check `df -h /home` before
starting. (These are estimates; verify against a 5M-row dry run.)

---

## 7. Resumability & operational safety (new)

A 6–12 h USB build *will* be interrupted at least once. Make it survivable:
- Keep a tiny `_build_progress(phase TEXT PRIMARY KEY, done_at INTEGER)` table in `master.db`. At
  startup, skip any completed phase. Each phase is a single transaction or replayable with
  `INSERT OR IGNORE` / `ON CONFLICT`, so re-running is safe.
- For Phase 5, also persist the CSV byte-offset/line number every checkpoint so a restart resumes
  mid-file instead of re-probing 178M ISRCs.
- After Phase 7, run `integrity_check` + `foreign_key_check`. Only then copy/rename to the canonical
  `master.db` path.
- Take a snapshot/hardlink of `master.db` after the expensive Phase 3/4 so an index-phase failure
  doesn't cost the 256M/348M-row inserts.
- 🔴 **Partial-table hazard under `journal_mode=OFF`:** a crash mid-`INSERT…SELECT` leaves a partially
  written, untrustworthy table. So on (re)start of any phase **not** marked complete in
  `_build_progress`, **`DROP`/recreate that phase's target table(s) fresh** before loading. Don't try
  to "resume into" a half-written bulk table. (Phase 5 is the exception — it's row-batched with a saved
  CSV offset and `ON CONFLICT`, so it resumes mid-stream.)

---

## 9. Implementation readiness — nail these down before writing C++

The plan/schema are otherwise ready. These are the remaining concrete items the builder author needs:

1. **Execution model (which phases are `sqlite3_exec` vs prepared loops).**
   - *Pure SQL, one `sqlite3_exec(INSERT…SELECT)`:* Phases 1, 2, 3-tracks, 3-mappings, 3-audiofeatures.
     These stream inside SQLite at IO speed; no row marshalling in C++.
   - *C++ prepared-statement loop:* Phase 4 **only if** `artist_position` is kept (needs the
     `vector<uint16_t>` counter); Phase 5 (CSV parse + ISRC probe + `ON CONFLICT`). Everything else is SQL.
2. **CSV parser (Phase 5).** flake.nix ships no CSV lib. Hand-roll a small RFC-4180 state machine
   (quotes, doubled-`""` escapes, embedded commas/newlines) or vendor a single header — **don't** add a
   Nix dependency. Resolve columns by header name once, then index by position. Target fields:
   `TrackISRC, AlbumGenreName, TrackReleaseDate`.
3. **Toolchain / build.** In the devshell verify `pkg-config --exists sqlite3` (needs `sqlite.dev` on
   the include path); compile `-O3 -std=c++17 $(pkg-config --cflags --libs sqlite3)`. Confirm the linked
   libsqlite3 is ≥ 3.51 (it is — sources were written by 3.51.2) so `WITHOUT ROWID`, `ON CONFLICT`, and
   `analysis_limit` are all available.
4. **`PRAGMA page_size=32768` must be set on the fresh, empty DB BEFORE applying `schema.sql`** (it's
   immutable once tables exist). Order: open → `page_size` → `journal_mode=OFF` etc. → run schema (tables
   only) → bulk phases → indexes.
5. **ATTACH model.** One connection: `ATTACH source_a AS sp; ATTACH audio_features AS af;` Master is
   `main`. Per the §0 exfat rule, source tables (`sp.*`, `af.*`) are only ever **scanned sequentially**;
   every random-access join targets a `main.*` table on `/home` (3a→`main.albums`, 3c→`main.track_mappings`,
   Phase 5→`main.tracks`). Never random-probe `sp.*`/`af.*`.
6. **Dry run first.** Before the full run, execute every phase against a ~5M-row slice (e.g. a
   `WHERE rowid < 5000000` filter on the source scans) end-to-end to validate SQL, camelot output,
   ON CONFLICT behavior, and to get real per-phase timings. This is cheap insurance on a 6–12 h build.

---

## 8. Summary of changes vs v1

| # | v1 | v2 |
|---|----|----|
| H1 | Sharded in-memory join, up to 62 USB full-scans, 8–10 GB/shard | `INSERT…SELECT` into separate tables; **no join, no shards, one pass** |
| H2 | Silent `artist_position` from nonexistent source order | Explicitly defined or dropped; documented |
| H3 | `replay_gain` / `raw_genre` / `mapped_genre` not in schema | Schema reconciled; conflict policy defined |
| H4 | `tracks.release_date` never populated | Filled from `albums.release_date` |
| H5 | ISRC index built *after* the phase that needs it | `idx_tracks_isrc` built **before** Phase 5 |
| H6 | 1:1 ISRC assumption | 1:N handled; `ON CONFLICT` policy; empty-genre filter |
| H7 | "306 GB, fits easily" | Real 246 GB; WAL off; temp on NVMe; one-at-a-time indexes |
| H8 | Camelot ignores NULL key/mode; separate pass | NULL-safe `CASE`, folded into Phase 3 |
| H9 | Naive CSV split | RFC-4180 quote-aware parser |
| H10 | No resumability | Phase-progress table + replayable phases + CSV offset |
| H11 | Under-budgeted RAM maps | Only one ~512 MB vector; everything else streamed |

**Net effect:** RAM peak drops from ~10 GB to <1 GB, source is read ~once instead of dozens of times,
the latent plan↔schema bugs are removed. The source is internal NVMe but **exfat (FUSE)**, so random
reads into it are slow regardless of hardware (benchmarked §0); the builder therefore reads source
**sequentially only** and routes every random join to a `/home` ext4 table (Phases 1, 3a, 3c, 5). All
§3 schema decisions are resolved and reflected in `schema.sql`. The plan is ready to implement.
