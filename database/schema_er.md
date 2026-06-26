# master.db — Entity-Relationship Diagram

Integer-keyed, normalized schema. `track_id` / `artist_id` / `album_id` are the
reused source SQLite rowids. The `tracks` ↔ `artists` many-to-many is resolved by
the `track_artists` junction. The three `ml_*` / `source_b_*` tables and
`track_audio_features` are 1:1 satellites of `tracks` (same PK).

```mermaid
erDiagram
    albums ||--o{ tracks : "has"
    tracks ||--o{ track_mappings : "maps to"
    tracks ||--o| track_audio_features : "has features"
    tracks ||--o{ track_artists : "credited on"
    artists ||--o{ track_artists : "credits"
    artists ||--o{ artist_genres : "tagged"
    tracks ||--o| source_b_genres : "ground-truth genre"
    tracks ||--o| ml_genre_predictions : "predicted genre"
    tracks ||--o| ml_10d_embeddings : "10D embedding"

    albums {
        INTEGER album_id PK "= source rowid"
        TEXT    source_a_id
        TEXT    title
        TEXT    album_type
        TEXT    release_date
        TEXT    cover_art_url
    }

    tracks {
        INTEGER track_id PK "= source rowid"
        INTEGER album_id FK
        TEXT    isrc
        TEXT    title
        INTEGER popularity
        TEXT    release_date
        INTEGER is_explicit
        INTEGER duration_ms
        TEXT    preview_url
    }

    track_mappings {
        INTEGER track_id PK,FK
        TEXT    platform PK
        TEXT    platform_id "idx"
    }

    track_audio_features {
        INTEGER track_id PK,FK
        REAL    danceability
        REAL    energy
        INTEGER key
        REAL    loudness
        INTEGER mode
        REAL    speechiness
        REAL    acousticness
        REAL    instrumentalness
        REAL    liveness
        REAL    valence
        REAL    tempo
        INTEGER time_signature
        TEXT    camelot_code "e.g. 8B / 5A"
    }

    artists {
        INTEGER artist_id PK "= source rowid"
        TEXT    source_a_id
        TEXT    name "idx"
        INTEGER popularity
        INTEGER followers_total
    }

    track_artists {
        INTEGER track_id PK,FK
        INTEGER artist_id PK,FK
        INTEGER artist_position
    }

    artist_genres {
        INTEGER artist_id PK,FK
        TEXT    genre PK
    }

    source_b_genres {
        INTEGER track_id PK,FK
        TEXT    genre "raw Source B AlbumGenreName"
    }

    ml_genre_predictions {
        INTEGER track_id PK,FK
        TEXT    predicted_genre "idx"
        REAL    confidence_score "idx"
        INTEGER tiebreaker_applied
        TEXT    model_version
    }

    ml_10d_embeddings {
        INTEGER track_id PK,FK
        BLOB    vector_blob
        TEXT    model_version
    }
```

**Cardinality legend:** `||` = exactly one, `o{` = zero-or-many, `o|` = zero-or-one.
So `albums ||--o{ tracks` = one album has many tracks; `tracks ||--o| track_audio_features`
= a track has at most one feature row.

**Indexes** (`"idx"` tagged above): `idx_tracks_isrc`, `idx_track_mappings_platform_id`,
`idx_track_artists_artist_id`, `idx_artists_name`, `idx_source_b_genres_genre`,
`idx_ml_predictions_genre`, `idx_ml_predictions_confidence`. Track-keyed satellites need
no index — their `INTEGER PRIMARY KEY` *is* the rowid.
```
```
