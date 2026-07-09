-- ════════════════════════════════════════════════════════════════════════════
-- Zinthos — master.db schema (v2)
--
-- KEY STRATEGY: integer surrogate keys. We REUSE the source SQLite `rowid`s as
-- our primary keys (tracks.rowid, artists.rowid, albums.rowid). This is the
-- single biggest space/perf win at 256M tracks / 348M associations: a 22-char
-- TEXT id repeated across 5 tables becomes an 8-byte (often varint-packed) int.
-- The original Source A text id lives in `track_mappings` (the Rosetta Stone) and
-- in `source_a_id` on artists/albums for external linking.
--
-- BULK-LOAD NOTE: create these tables WITHOUT the indexes at the bottom, bulk
-- load, then create indexes one-at-a-time (see implementation_plan_v2.md §7).
-- `idx_tracks_isrc` is the exception — build it BEFORE the Source B phase.
-- ════════════════════════════════════════════════════════════════════════════

-- 1. Albums (created first so tracks can FK to it)
CREATE TABLE albums (
    album_id      INTEGER PRIMARY KEY,  -- = source albums.rowid
    source_a_id   TEXT,                 -- original Source A album id (external linking)
    title         TEXT NOT NULL,
    album_type    TEXT,                 -- 'album', 'single', 'compilation'
    release_date  TEXT,                 -- album release date (also denormalized onto tracks)
    cover_art_url TEXT                  -- largest album_image url
);

-- 2. Core track metadata
CREATE TABLE tracks (
    track_id     INTEGER PRIMARY KEY,   -- = source tracks.rowid
    album_id     INTEGER,               -- = source tracks.album_rowid
    isrc         TEXT,                  -- universal recording barcode (Source B join key)
    title        TEXT,
    popularity   INTEGER,
    release_date TEXT,                  -- denormalized from albums.release_date for era_prox/F6
    is_explicit  INTEGER,
    duration_ms  INTEGER,
    preview_url  TEXT,                  -- 30s audio preview (~87% coverage) — core to playback
    FOREIGN KEY(album_id) REFERENCES albums(album_id) ON DELETE SET NULL
);

-- 3. Universal track mappings (Source A/Source B/Source C Rosetta Stone)
CREATE TABLE track_mappings (
    track_id    INTEGER,
    platform    TEXT,                   -- 'source_a', 'source_b', 'source_c'
    platform_id TEXT,                   -- that platform's id (e.g. the Source A base62 id)
    PRIMARY KEY (track_id, platform),
    FOREIGN KEY(track_id) REFERENCES tracks(track_id) ON DELETE CASCADE
);  -- NOTE: was WITHOUT ROWID; removed — it made the 256M-row bulk load 38x slower
    -- (8812s vs 230s for the equivalent rowid table). Space saving not worth it.

-- 4. Raw acoustic math & music theory
-- track_id is a plain column (NOT the rowid): the loader streams audio features in
-- af's order and appends by auto-rowid, so it can't be ordered by track_id at insert
-- time. Uniqueness/lookup is provided by idx_taf_track_id, built after the bulk load.
CREATE TABLE track_audio_features (
    track_id        INTEGER,             -- = source tracks.rowid (remapped from af.track_id text via in-RAM map)
    danceability    REAL,
    energy          REAL,
    "key"           INTEGER,
    loudness        REAL,
    mode            INTEGER,
    speechiness     REAL,
    acousticness    REAL,
    instrumentalness REAL,
    liveness        REAL,
    valence         REAL,
    tempo           REAL,                -- INTEGER in source; widened to REAL
    time_signature  INTEGER,
    camelot_code    TEXT,                -- pre-computed harmonic-mixing code, e.g. '8A','9B' (NULL if key/mode null)
    FOREIGN KEY(track_id) REFERENCES tracks(track_id) ON DELETE CASCADE
);

-- 5. Unique artists (cultural tie-breaker)
CREATE TABLE artists (
    artist_id       INTEGER PRIMARY KEY, -- = source artists.rowid
    source_a_id     TEXT,                -- original Source A artist id
    name            TEXT,
    popularity      INTEGER,             -- Source A artist popularity (0-100)
    followers_total INTEGER              -- for artist ranking / F3
);

-- 6. Track-to-artist mapping (multiple artists per track)
CREATE TABLE track_artists (
    track_id        INTEGER,
    artist_id       INTEGER,
    artist_position INTEGER,             -- rank within a track by ascending artist_id (NOT authoritative credited order)
    PRIMARY KEY (track_id, artist_id),
    FOREIGN KEY(track_id)  REFERENCES tracks(track_id)   ON DELETE CASCADE,
    FOREIGN KEY(artist_id) REFERENCES artists(artist_id) ON DELETE CASCADE
);  -- NOTE: was WITHOUT ROWID; removed for bulk-load speed (see track_mappings note).
    -- Loaded ORDER BY (track_id, artist_id) so both the rowid and the PK index append sequentially.

-- 7. Artist genres (cultural prior / tie-breaker)
CREATE TABLE artist_genres (
    artist_id INTEGER,
    genre     TEXT,
    PRIMARY KEY (artist_id, genre),
    FOREIGN KEY(artist_id) REFERENCES artists(artist_id) ON DELETE CASCADE
) WITHOUT ROWID;

-- 8. Source B ground-truth genres (~141.67M labels).
--    `genre` stores the RAW Source B AlbumGenreName; the 20-genre consolidation
--    is applied in the Python ML layer to keep this table auditable.
CREATE TABLE source_b_genres (
    track_id INTEGER PRIMARY KEY,
    genre    TEXT NOT NULL,
    FOREIGN KEY(track_id) REFERENCES tracks(track_id) ON DELETE CASCADE
);

-- 9. LightGBM genre predictions
CREATE TABLE ml_genre_predictions (
    track_id         INTEGER PRIMARY KEY,
    predicted_genre  TEXT NOT NULL,
    confidence_score REAL NOT NULL,
    tiebreaker_applied INTEGER DEFAULT 0, -- 1 if artist history used, 0 if purely acoustic
    model_version    TEXT NOT NULL,        -- e.g. 'lgbm_v1'
    FOREIGN KEY(track_id) REFERENCES tracks(track_id) ON DELETE CASCADE
);

-- 10. 10D embeddings (supervised autoencoder → FAISS)
CREATE TABLE ml_10d_embeddings (
    track_id      INTEGER PRIMARY KEY,
    vector_blob   BLOB NOT NULL,          -- 10D coordinate for FAISS
    model_version TEXT NOT NULL,          -- e.g. 'autoencoder_v1'
    FOREIGN KEY(track_id) REFERENCES tracks(track_id) ON DELETE CASCADE
);

-- ── Performance indexes (build AFTER bulk load, one at a time) ───────────────
-- NOTE: track_id-keyed tables need no track_id index — the INTEGER PK is the rowid.

-- Build this one BEFORE the Source B phase (it drives ISRC → track lookups):
CREATE INDEX idx_tracks_isrc ON tracks(isrc);

-- Multi-platform id lookups (find a track by its Source A/Source B id):
CREATE INDEX idx_track_mappings_platform_id ON track_mappings(platform_id);

-- Reverse join artist → tracks:
CREATE INDEX idx_track_artists_artist_id ON track_artists(artist_id);

-- track_audio_features lookup/uniqueness by track_id (built after the append-load):
CREATE UNIQUE INDEX idx_taf_track_id ON track_audio_features(track_id);

-- Genre filtering for CLI/semantic queries:
CREATE INDEX idx_source_b_genres_genre   ON source_b_genres(genre);
CREATE INDEX idx_ml_predictions_genre  ON ml_genre_predictions(predicted_genre);

-- Top-confidence retrieval:
CREATE INDEX idx_ml_predictions_confidence ON ml_genre_predictions(confidence_score);

-- Artist lookup by name (F3 GET /artist/{name}):
CREATE INDEX idx_artists_name ON artists(name);

-- Case-insensitive artist lookup (F3's fallback probe). Equality with COLLATE NOCASE
-- cannot seek the BINARY index above — without this it full-scans 15.4M names (~0.5 s
-- per miss, measured). Added 2026-07-08; ~331 MB. Seek time ~1 ms.
CREATE INDEX idx_artists_name_nocase ON artists(name COLLATE NOCASE);
