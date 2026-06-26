# master.db — Validation Checklist (for a fresh session)

**Your task:** rigorously verify that `/home/zer0/Desktop/sonic_something/master.db` is
complete, internally consistent, free of corruption, and free of unintended duplicates —
so it can be trusted as the foundation for the ML stages. **Read-only: do NOT modify the DB.**
Work through every section, run the queries, and produce a final **PASS/FAIL table**
(Check | Expected | Actual | Verdict). Flag anything that deviates from "Expected".

> Note: this is a ~115 GB SQLite DB on NVMe. Full-scan checks (counts, range checks,
> `integrity_check`) each take from tens of seconds to ~10 minutes. That's normal — let them run
> (use `screen`/`tmux` if you like). Use the `sqlite3` CLI.

---

## 0. Background (so you understand what you're checking)
- Built by `database/master_db_builder.cpp` from `source_a.sqlite3`,
  `source_a_audio_features.sqlite3`, and `source_b.csv`. **Read `database/schema.sql` first** —
  it is the authoritative schema with explanatory comments.
- **Integer surrogate keys:** `track_id`/`artist_id`/`album_id` are integers (reused source rowids).
  The original Source A text ids live in `track_mappings.platform_id` and `*.source_a_id`.
- **Key per-table facts you must account for (these are by design, not bugs):**
  - `track_audio_features.track_id` is a **plain indexed column**, not the rowid PK (uniqueness is via
    `idx_taf_track_id`). Loaded by appending in source order.
  - `track_mappings`, `track_artists`, `tracks`, `source_b_genres` are normal rowid tables;
    `artist_genres` is `WITHOUT ROWID`.
  - `source_b_genres.genre` holds the **raw** Source B `AlbumGenreName` (consolidation happens later
    in Python — do not expect 20 clean genres here).
  - `ml_genre_predictions` and `ml_10d_embeddings` are **intentionally empty** (filled by later stages).

## 1. Confirm the build actually finished
```bash
grep -E "BUILD COMPLETE|FATAL|Error" /home/zer0/Desktop/sonic_something/build.log | tail
```
Expected: a `=== BUILD COMPLETE ... ===` line and **no** FATAL/Error. If the build didn't finish, stop
and report — the checks below assume a completed build.

## 2. Structural integrity (corruption) — THE critical one
```sql
PRAGMA integrity_check;     -- full check (~5–10 min). MUST print exactly: ok
```
(`quick_check` is faster but less thorough — use the full `integrity_check`.) Any output other than
`ok` means corruption → FAIL.

## 3. Foreign-key / referential consistency (orphans)
FKs were disabled during the bulk load, so verify there are no orphaned rows:
```sql
PRAGMA foreign_key_check;   -- expected: ZERO rows returned
```
If it returns rows, report which table/parent. (Spot-confirm the big ones if you want:)
```sql
-- audio features pointing at non-existent tracks (expect 0):
SELECT count(*) FROM track_audio_features f LEFT JOIN tracks t ON t.track_id=f.track_id WHERE t.track_id IS NULL;
-- track_artists pointing at non-existent track or artist (expect 0 each):
SELECT count(*) FROM track_artists x LEFT JOIN tracks t ON t.track_id=x.track_id WHERE t.track_id IS NULL;
SELECT count(*) FROM track_artists x LEFT JOIN artists a ON a.artist_id=x.artist_id WHERE a.artist_id IS NULL;
-- source_b genres pointing at non-existent tracks (expect 0):
SELECT count(*) FROM source_b_genres d LEFT JOIN tracks t ON t.track_id=d.track_id WHERE t.track_id IS NULL;
```

## 4. Row counts vs expected
```sql
SELECT 'albums', count(*) FROM albums
UNION ALL SELECT 'artists', count(*) FROM artists
UNION ALL SELECT 'artist_genres', count(*) FROM artist_genres
UNION ALL SELECT 'tracks', count(*) FROM tracks
UNION ALL SELECT 'track_mappings', count(*) FROM track_mappings
UNION ALL SELECT 'track_audio_features', count(*) FROM track_audio_features
UNION ALL SELECT 'track_artists', count(*) FROM track_artists
UNION ALL SELECT 'source_b_genres', count(*) FROM source_b_genres
UNION ALL SELECT 'ml_genre_predictions', count(*) FROM ml_genre_predictions
UNION ALL SELECT 'ml_10d_embeddings', count(*) FROM ml_10d_embeddings;
```
| Table | Expected (from build log) |
|---|---|
| albums | 58,590,982 |
| artists | 15,430,442 |
| artist_genres | 2,229,947 |
| tracks | 256,039,007 |
| track_mappings | 256,039,007 |
| track_audio_features | 254,819,856 |
| track_artists | 348,055,676 (≈80 fewer than the 348,055,756 source rows — duplicate (track_id,artist_id) pairs correctly de-duplicated) |
| source_b_genres | 125,256,383 |
| ml_genre_predictions | 0 (intentional) |
| ml_10d_embeddings | 0 (intentional) |

Small deviations on the big tables are suspicious — report exact numbers.

## 5. Uniqueness / "no repetition"
The PKs/unique indexes should already guarantee these; confirm them. (count vs distinct is cheaper than
GROUP BY HAVING.)
```sql
-- tracks: track_id is PK → unique. Expect equal:
SELECT count(*) AS rows, count(DISTINCT track_id) AS distinct_ids FROM tracks;
-- track_audio_features: track_id must be unique (idx_taf_track_id). Expect equal:
SELECT count(*) AS rows, count(DISTINCT track_id) AS distinct_ids FROM track_audio_features;
-- source_b: track_id is PK → unique. Expect equal:
SELECT count(*) AS rows, count(DISTINCT track_id) AS distinct_ids FROM source_b_genres;
-- track_mappings: source_a platform_id must be unique 1:1 with track_id. Expect equal:
SELECT count(*) AS rows, count(DISTINCT platform_id) AS distinct_pid FROM track_mappings;
-- track_artists: no duplicate (track_id, artist_id) pairs (PK). Expect 0 rows:
SELECT track_id, artist_id, count(*) c FROM track_artists GROUP BY track_id, artist_id HAVING c>1 LIMIT 5;
```

## 6. Audio-feature value sanity (no garbage data)
Each of these should return **0** out-of-range rows. (Run them; they're full scans.)
```sql
SELECT count(*) FROM track_audio_features WHERE danceability     NOT BETWEEN 0 AND 1 AND danceability     IS NOT NULL;
SELECT count(*) FROM track_audio_features WHERE energy           NOT BETWEEN 0 AND 1 AND energy           IS NOT NULL;
SELECT count(*) FROM track_audio_features WHERE speechiness      NOT BETWEEN 0 AND 1 AND speechiness      IS NOT NULL;
SELECT count(*) FROM track_audio_features WHERE acousticness     NOT BETWEEN 0 AND 1 AND acousticness     IS NOT NULL;
SELECT count(*) FROM track_audio_features WHERE instrumentalness NOT BETWEEN 0 AND 1 AND instrumentalness IS NOT NULL;
SELECT count(*) FROM track_audio_features WHERE liveness         NOT BETWEEN 0 AND 1 AND liveness         IS NOT NULL;
SELECT count(*) FROM track_audio_features WHERE valence          NOT BETWEEN 0 AND 1 AND valence          IS NOT NULL;
SELECT count(*) FROM track_audio_features WHERE "key"            NOT BETWEEN -1 AND 11 AND "key"          IS NOT NULL;
SELECT count(*) FROM track_audio_features WHERE mode             NOT IN (0,1) AND mode IS NOT NULL;
SELECT count(*) FROM track_audio_features WHERE time_signature   NOT BETWEEN 0 AND 7 AND time_signature   IS NOT NULL;
SELECT count(*) FROM track_audio_features WHERE tempo < 0;        -- expect 0
SELECT count(*) FROM tracks WHERE popularity NOT BETWEEN 0 AND 100 AND popularity IS NOT NULL;  -- expect 0
```
**Camelot codes** must be one of the 24 valid wheel codes or NULL:
```sql
SELECT count(*) FROM track_audio_features
WHERE camelot_code IS NOT NULL
  AND camelot_code NOT IN ('1A','2A','3A','4A','5A','6A','7A','8A','9A','10A','11A','12A',
                           '1B','2B','3B','4B','5B','6B','7B','8B','9B','10B','11B','12B');   -- expect 0
-- spot-check the mapping is correct: C major(key=0,mode=1)→8B, A minor(key=9,mode=0)→8A
SELECT "key", mode, camelot_code, count(*) FROM track_audio_features
WHERE ("key"=0 AND mode=1) OR ("key"=9 AND mode=0) GROUP BY 1,2,3;
-- and the NULL guard: rows with key=-1 or NULL key/mode must have NULL camelot:
SELECT count(*) FROM track_audio_features WHERE ("key" IS NULL OR mode IS NULL OR "key"<0) AND camelot_code IS NOT NULL; -- expect 0
```

## 7. Coverage / containment (sanity, not necessarily 100%)
```sql
-- every audio/mapping/source_b track_id must exist in tracks (containment). Expect 0 each:
SELECT count(*) FROM track_mappings m LEFT JOIN tracks t ON t.track_id=m.track_id WHERE t.track_id IS NULL;
-- how many tracks have features / a source_b genre / at least one artist (report %, not pass/fail):
SELECT (SELECT count(*) FROM track_audio_features) AS with_features,
       (SELECT count(*) FROM source_b_genres) AS with_source_b_genre,
       (SELECT count(DISTINCT track_id) FROM track_artists) AS with_artist,
       (SELECT count(*) FROM tracks) AS total_tracks;
```

## 8. Indexes & stats present
```sql
SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%' ORDER BY name;
-- expect: idx_artists_name, idx_source_b_genres_genre, idx_ml_predictions_confidence,
--         idx_ml_predictions_genre, idx_taf_track_id, idx_track_artists_artist_id,
--         idx_track_mappings_platform_id, idx_tracks_isrc
SELECT count(*) FROM sqlite_stat1;   -- > 0 means ANALYZE ran
```

---

## Known-OK anomalies (do NOT flag these as failures)
- **`track_audio_features` (254.8M) < `tracks` (256M):** ~1.2M tracks legitimately have no audio
  features (source `null_response=1` rows were skipped). Expected.
- **~1,258 audio rows scanned but unmatched** during build (orphan audio with no track) — already
  excluded; not in the DB.
- **`source_b_genres` (~125M) < `tracks`:** only tracks whose ISRC matched a non-empty Source B
  album genre get a row. Expected; not all tracks have a genre label.
- **Some `tracks.isrc` / `tracks.preview_url` are NULL** (~13% of preview_url). Expected.
- **`ml_genre_predictions` / `ml_10d_embeddings` empty (0 rows).** Expected — filled by later stages.
- **`source_b_genres.genre` is raw text** with many distinct values (not 20 consolidated). Expected.

## Deliverable
Produce a single table: **Check | Expected | Actual | PASS/FAIL**, with a one-line verdict at the end:
"DB is sound — safe to proceed to the ML stage" or a list of concrete problems found. If `integrity_check`
≠ `ok` or `foreign_key_check` returns rows, the overall verdict is FAIL regardless of other checks.
