"""
build_track_search.py — M0: build the `track_search` hot table inside master.db
═══════════════════════════════════════════════════════════════════════════════
A compact, denormalized, one-row-per-(featured)-track table. It is the FILTER source
for F1/F2 and the RE-RANK source for F6. Built once (data is static — no write-sync).

  • Features [0,1] → scaled ints 0–1000; tempo = BPM (int); loudness = dB (int).
  • genre_id (0–19) = COALESCE( consolidate(source_b raw genre), ml predicted_genre )
        ground truth preferred; genre_source records which won (0=source_b, 1=ml).
  • Covering index idx_search (popularity-led) built AFTER the load.

MEMORY DISCIPLINE (the box is 15 GB; ~9 GB resident = swap-thrash threshold):
  • Insert in track_id-ordered BATCHES, each its own transaction + WAL truncate, so the
    WAL never balloons (the v1 single-transaction build grew an 11 GB WAL → swap death).
  • Modest page cache; on-disk temp dir for the index sort (NOT temp_store=MEMORY).

Genre vocabulary authority:
  • macro→id  : model_training/genre_label_encoder.joblib  (mirrored in MACRO_GENRES below)
  • raw→macro : model_training/training_data.py GENRE_MAP  (+ EXTENDED_MAP for the 47
                Source B genres that map was missing)

Usage:
  python build_track_search.py --smoke      # one small batch, no index — validate logic
  python build_track_search.py              # full batched build (254.8M) + idx_search
"""
from __future__ import annotations

import argparse
import os
import sys
import time

# ── Genre vocabulary ────────────────────────────────────────────────────────────
# Canonical 20 macro genres; list index = genre_id (mirrors genre_label_encoder.joblib).
MACRO_GENRES = [
    "african", "alternative", "asian", "christian", "classical", "country", "dance",
    "electronic", "folk", "hip-hop", "jazz", "kids", "latin", "metal", "other",
    "pop", "r&b", "reggae", "rock", "soundtrack",
]
MACRO_ID = {name: i for i, name in enumerate(MACRO_GENRES)}

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "model_training"))
from training_data import GENRE_MAP  # noqa: E402  (the original 30 raw→macro mappings)

# The 47 Source B genres GENRE_MAP was missing. Clear cases mapped to the obvious macro;
# the ⚠ marks are genuine judgment calls — override here and re-run the genre UPDATE if wrong.
EXTENDED_MAP: dict[str, str] = {
    # Latin family
    "Axé/Forró": "latin", "Banda/Grupero": "latin", "Bolero": "latin", "Corridos": "latin",
    "Flamenco": "latin", "Folklore latino-américain": "latin", "MPB": "latin",
    "Norteño": "latin", "Pop latino": "latin", "Ranchera": "latin",
    "Rap/Funk Brasileiro": "latin", "Rock latino": "latin", "Salsa": "latin",
    "Samba/Pagode": "latin", "Sertanejo": "latin", "Tango": "latin",
    # R&B / soul family
    "Contemporary R&B": "r&b", "Contemporary Soul": "r&b", "Old school soul": "r&b",
    "Oldschool R&B": "r&b", "Soul": "r&b",
    "Blues": "r&b",                       # ⚠ blues→r&b (roots lineage); folk also defensible
    # Soundtrack / score
    "Film Scores": "soundtrack", "TV Soundtracks": "soundtrack",
    "Gaming": "soundtrack",               # ⚠ game music → soundtrack
    # Country / rock
    "Bluegrass": "country", "Urban Cowboy": "country", "Rock & Roll/Rockabilly": "rock",
    # Electronic
    "Techno/House": "electronic",
    "South African House": "electronic",  # ⚠ could be dance
    # Hip-hop
    "Grime": "hip-hop",
    # Christian / religious
    "Gospel": "christian",
    "Spiritualiteit & religie": "other",  # ⚠ spirituality+religion umbrella → other
    # Folk / regional-language
    "French Chanson": "folk", "Türk halk müziği": "folk",   # ⚠ Turkish FOLK music
    "Schlager & Volksmusik": "folk",                        # ⚠ Volksmusik = folk
    "Deutsche Musik": "other", "Nederlandstalige muziek": "other", "Afrikaans": "other",
    # Pop / kids
    "International Pop": "pop", "Kinderen & familie": "kids",
    # Non-music / spoken → other
    "Business": "other", "Education": "other", "Technology": "other", "Storytelling": "other",
    "Komedie": "other", "Hörbücher auf Deutsch": "other",
}

FEATURE_SCALE = {  # column → scaling expression applied to track_audio_features
    "danceability": "ROUND({c}*1000)", "energy": "ROUND({c}*1000)",
    "valence": "ROUND({c}*1000)", "acousticness": "ROUND({c}*1000)",
    "instrumentalness": "ROUND({c}*1000)", "speechiness": "ROUND({c}*1000)",
    "liveness": "ROUND({c}*1000)", "tempo": "ROUND({c})", "loudness": "ROUND({c})",
}

DB_PATH = os.path.join(ROOT, "master.db")
TMPDIR = os.path.join(ROOT, "_build_tmp")           # on-disk temp for the index sort
BATCH = 4_000_000                                    # track_id range per transaction


def build(smoke: bool) -> None:
    import sqlite3

    os.makedirs(TMPDIR, exist_ok=True)
    os.environ["SQLITE_TMPDIR"] = TMPDIR             # keep index-sort spill OFF tmpfs/RAM

    full_map = {**GENRE_MAP, **EXTENDED_MAP}
    print(f"  genre map: {len(full_map)} raw → 20 macro  "
          f"({len(GENRE_MAP)} original + {len(EXTENDED_MAP)} extended)")

    db = sqlite3.connect(DB_PATH, timeout=60)
    c = db.cursor()
    c.execute("PRAGMA busy_timeout=60000")
    c.execute("PRAGMA synchronous=OFF")             # table is rebuildable
    c.execute("PRAGMA temp_store=FILE")             # index sort spills to SQLITE_TMPDIR (disk)
    c.execute("PRAGMA cache_size=-524288")          # ~512 MB page cache
    c.execute("PRAGMA mmap_size=4000000000")        # 4 GB mmap of clean read pages (reclaimable)

    # ── genres dimension + raw/macro → id lookup ────────────────────────────────
    c.execute("DROP TABLE IF EXISTS genres")
    c.execute("CREATE TABLE genres (genre_id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
    c.executemany("INSERT INTO genres VALUES (?,?)", list(enumerate(MACRO_GENRES)))

    c.execute("DROP TABLE IF EXISTS _genre_str2id")
    c.execute("CREATE TABLE _genre_str2id (genre_string TEXT PRIMARY KEY, genre_id INTEGER)")
    s2id = {raw: MACRO_ID[macro] for raw, macro in full_map.items()}
    s2id.update({macro: i for i, macro in enumerate(MACRO_GENRES)})
    c.executemany("INSERT OR REPLACE INTO _genre_str2id VALUES (?,?)", list(s2id.items()))

    c.execute("DROP TABLE IF EXISTS track_search")
    c.execute("""
        CREATE TABLE track_search (
            track_id     INTEGER PRIMARY KEY,
            popularity   INTEGER NOT NULL DEFAULT 0,
            genre_id     INTEGER,
            genre_source INTEGER,
            release_year INTEGER,
            danceability INTEGER, energy INTEGER, valence INTEGER,
            acousticness INTEGER, instrumentalness INTEGER, speechiness INTEGER,
            liveness INTEGER, tempo INTEGER, loudness INTEGER
        )""")
    db.commit()

    feat_cols = list(FEATURE_SCALE)
    feat_select = ",\n            ".join(
        f"CAST({FEATURE_SCALE[col].format(c='taf.'+col)} AS INTEGER)" for col in feat_cols)
    # Drive FROM tracks (PK = track_id order) so inserts APPEND sequentially (no B-tree thrash).
    insert_sql = f"""
        INSERT INTO track_search
            (track_id, popularity, genre_id, genre_source, release_year, {', '.join(feat_cols)})
        SELECT
            t.track_id,
            COALESCE(t.popularity, 0),
            COALESCE(dm.genre_id, mm.genre_id),
            CASE WHEN dm.genre_id IS NOT NULL THEN 0
                 WHEN mm.genre_id IS NOT NULL THEN 1 END,
            CASE WHEN t.release_date GLOB '[0-9][0-9][0-9][0-9]*'
                 THEN CAST(substr(t.release_date,1,4) AS INTEGER) END,
            {feat_select}
        FROM tracks t
        JOIN track_audio_features taf    ON taf.track_id = t.track_id
        LEFT JOIN source_b_genres d ON d.track_id = t.track_id
        LEFT JOIN _genre_str2id dm       ON dm.genre_string = d.genre
        LEFT JOIN ml_genre_predictions m ON m.track_id = t.track_id
        LEFT JOIN _genre_str2id mm       ON mm.genre_string = m.predicted_genre
        WHERE t.track_id > ? AND t.track_id <= ?
    """

    max_id = c.execute("SELECT max(track_id) FROM tracks").fetchone()[0]
    if smoke:
        max_id = min(max_id, 2_000_000)
    print(f"  building track_search{' (SMOKE)' if smoke else ''} over track_id ≤ {max_id:,} "
          f"in {BATCH:,}-id batches …")

    t0 = time.time()
    total = 0
    lo = 0
    while lo < max_id:
        hi = min(lo + BATCH, max_id)
        c.execute(insert_sql, (lo, hi))
        total += c.rowcount if c.rowcount and c.rowcount > 0 else 0
        db.commit()
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")     # keep WAL tiny
        el = time.time() - t0
        pct = hi / max_id * 100
        eta = el / pct * (100 - pct) if pct else 0
        print(f"    id≤{hi:>12,} ({pct:5.1f}%)  rows={total:>13,}  "
              f"[{el:5.0f}s, eta {eta:5.0f}s]", flush=True)
        lo = hi
    print(f"  ✓ inserted {total:,} rows in {time.time()-t0:.0f}s")

    src = dict(c.execute(
        "SELECT genre_source, COUNT(*) FROM track_search GROUP BY genre_source").fetchall())
    print("  genre_source breakdown:")
    print(f"     source_b truth (0): {src.get(0,0):,}")
    print(f"     ml predicted (1): {src.get(1,0):,}")
    print(f"     no genre   (NULL): {src.get(None,0):,}")

    if not smoke:
        print("  building idx_search (covering, popularity-led; sort spills to disk) …")
        t = time.time()
        c.execute("""
            CREATE INDEX idx_search ON track_search(
                popularity, energy, valence, danceability, acousticness,
                instrumentalness, tempo, loudness, speechiness, liveness,
                genre_id, release_year)""")
        db.commit()
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        print(f"  ✓ idx_search in {time.time()-t:.0f}s")

    c.execute("DROP TABLE _genre_str2id")
    db.commit()
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    plan = c.execute(
        "EXPLAIN QUERY PLAN SELECT track_id FROM track_search "
        "WHERE valence < 300 AND energy < 500 ORDER BY popularity DESC LIMIT 300"
    ).fetchall()
    print("  EXPLAIN QUERY PLAN (F1 sample):")
    for row in plan:
        print("     ", row[-1])

    db.close()
    print("\n══════════════════ M0 DONE ══════════════════")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true", help="one small batch, skip idx_search")
    build(ap.parse_args().smoke)
