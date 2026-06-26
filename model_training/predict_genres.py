"""
predict_genres.py — Batch genre inference for the unlabeled half of master.db
═══════════════════════════════════════════════════════════════════════════════
Fills `ml_genre_predictions` for every track that (a) has audio features and
(b) has NO Source B ground-truth genre, using the pre-trained LightGBM model plus
a "Two-Brain" (Acoustic + Cultural) validation cascade.

Pipeline per track
------------------
  Gate 1 (Acoustic pass) : top probability >= --threshold  → accept, tiebreaker=0
  Gate 2 (Cultural pass) : top prob < threshold, but an artist micro-genre in
                           `artist_genres` structurally supports the model's macro
                           guess                            → accept, tiebreaker=1
  Gate 3 (Fall-through)  : low prob AND no cultural support → SKIP (leave NULL)

Hard-won correctness facts (verified against the live artifacts):
  • The model is a native lightgbm.Booster → call .predict(X); there is NO
    .predict_proba(). For `multiclass`, .predict() already returns (n, 20) probs.
  • Features MUST be StandardScaler-transformed (genre_scaler.joblib, 13-feature)
    before predicting — the model was trained on scaled inputs.
  • `duration_ms` is NOT in track_audio_features; it lives in `tracks`. It is
    feature index 11 in the model's expected order.

Scale strategy:
  • Keyset (cursor) pagination on track_id — NOT LIMIT/OFFSET (which is O(n^2)).
  • Each batch is an independent short SELECT, so the long-read-vs-write locking
    problem never arises; master.db runs in WAL with separate read/write conns.
  • Resumable: a checkpoint row records the last processed track_id.

Usage:
  python predict_genres.py                      # full run, defaults
  python predict_genres.py --threshold 0.50     # tune Gate 1
  python predict_genres.py --limit-batches 2    # smoke test on 2 batches
  python predict_genres.py --audit-vocab        # dump micro-genre→macro coverage
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
import unicodedata

import joblib
import lightgbm as lgb
import numpy as np

# ── Paths (resolved relative to this file so cwd doesn't matter) ───────────────
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB_PATH = os.path.join(ROOT, "master.db")
MODEL_PATH = os.path.join(HERE, "genre_gbdt.txt")
SCALER_PATH = os.path.join(HERE, "genre_scaler.joblib")          # 13-feature scaler
ENCODER_PATH = os.path.join(HERE, "genre_label_encoder.joblib")

MODEL_VERSION = "lgbm_v1"
BATCH_SIZE = 250_000

# Feature order is LOAD-BEARING: it must match booster.feature_name() and the
# scaler's column order exactly. duration_ms (index 11) comes from `tracks`.
FEATURE_COLS = [
    "danceability", "energy", "key", "loudness", "mode",
    "speechiness", "acousticness", "instrumentalness",
    "liveness", "valence", "tempo", "duration_ms", "time_signature",
]

# ── Cultural tie-breaker vocabulary ───────────────────────────────────────────
# Gate 2 is a *validation gate*, not a micro→macro classifier. For each macro we
# list keywords; we ONLY test the keyword set of the macro the model already
# guessed. This sidesteps ambiguity (e.g. "pop punk" supports BOTH rock and pop,
# and that's fine — we test against whichever one the model predicted).
#
# Matching is substring-on-normalized-text, leaning on distinctive multi-char
# keywords. "other" is intentionally empty: it is a catch-all macro that cannot
# be culturally validated, so low-confidence "other" predictions fall through.
MACRO_KEYWORDS: dict[str, set[str]] = {
    "african": {
        "afrobeat", "afrobeats", "afropop", "afro house", "amapiano", "highlife",
        "soukous", "kwaito", "gqom", "bongo flava", "naija", "mbalax",
        "genge", "gengetone", "kuduro", "ndombolo",
    },
    "alternative": {
        "alternative", "alt rock", "alt-rock", "indie", "shoegaze", "emo",
        "grunge", "britpop", "post-punk", "new wave", "art rock", "math rock",
    },
    "asian": {
        "k-pop", "j-pop", "c-pop", "mandopop", "cantopop", "city pop", "j-rock",
        "bollywood", "desi", "hindi", "punjabi", "bhangra", "anime", "thai",
        "vietnam", "indo", "tamil", "telugu", "khmer", "gamelan", "sufi",
        "qawwali", "ghazal", "carnatic", "hindustani", "bhajan",
    },
    "christian": {
        "christian", "worship", "gospel", "ccm", "praise", "hymn", "spiritual",
    },
    "classical": {
        "classical", "baroque", "opera", "orchestra", "orchestral", "symphony",
        "chamber music", "choral", "concerto", "cantata", "requiem", "gregorian",
        "renaissance", "early music", "string quartet",
    },
    "country": {
        "country", "bluegrass", "americana", "honky", "nashville",
        "western swing", "outlaw country", "alt-country", "cowboy",
    },
    "dance": {
        "dance", "edm", "eurodance", "dance pop", "disco", "nightcore",
        "hi-nrg", "hands up", "handsup", "big room", "hardstyle",
    },
    "electronic": {
        "house", "techno", "trance", "electro", "dubstep", "drum and bass",
        "dnb", "ambient", "idm", "breakbeat", "downtempo", "synthwave",
        "electronica", "trip hop", "glitch", "big beat", "gabber", "jungle",
        "future bass", "chillwave", "vaporwave", "psytrance", "acid",
        "breakcore", "bass music",
    },
    "folk": {
        "folk", "singer-songwriter", "traditional music", "celtic", "fado",
        "sea shanty", "neofolk", "freak folk", "tradfolk",
    },
    "hip-hop": {
        "hip hop", "rap", "trap", "drill", "grime", "boom bap", "crunk",
        "g-funk", "gangster", "cloud rap", "phonk", "hyphy",
    },
    "jazz": {
        "jazz", "bebop", "swing", "bossa nova", "fusion", "big band", "ragtime",
        "dixieland", "hard bop", "cool jazz", "smooth jazz", "free jazz",
    },
    "kids": {
        "kids", "children", "nursery", "lullaby", "cartoon", "disney",
    },
    "latin": {
        "latin", "reggaeton", "salsa", "bachata", "merengue", "cumbia", "bolero",
        "mariachi", "ranchera", "banda", "norteno", "corrido", "tango", "forro",
        "sertanejo", "tropical", "vallenato", "flamenco", "samba",
    },
    "metal": {
        "metal", "core", "djent", "thrash", "grindcore", "doom", "sludge",
        "death", "black metal", "grind", "deathcore", "metalcore",
    },
    "other": set(),  # catch-all macro: not culturally validatable on purpose
    "pop": {
        "pop", "synthpop", "electropop", "power pop", "art pop", "indie pop",
        "dream pop", "bubblegum", "teen pop", "chamber pop", "sophisti-pop",
    },
    "r&b": {
        "r&b", "rnb", "soul", "funk", "motown", "neo soul", "new jack",
        "quiet storm", "doo-wop",
    },
    "reggae": {
        "reggae", "dancehall", "ska", "rocksteady", "roots reggae", "riddim",
        "ragga", "lovers rock",
    },
    "rock": {
        "rock", "punk", "gaze", "grunge", "garage", "psych", "hard rock",
        "classic rock", "prog", "post-rock", "surf", "rockabilly", "stoner",
    },
    "soundtrack": {
        "soundtrack", "score", "film", "cinematic", "video game", "vgm",
        "musical", "broadway", "theme", "library music", "trailer",
    },
}


def normalize_genre(s: str) -> str:
    """Lower-case, fold diacritics, and collapse whitespace for matching.

    Diacritic folding is essential: Source A genres include 'forró', 'norteño',
    'raï', etc., which must match plain-ASCII keywords ('forro', 'norteno').
    """
    decomposed = unicodedata.normalize("NFKD", s.lower())
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(ascii_only.split())


def gate2_supports(predicted_macro: str, artist_micro_genres: set[str]) -> bool:
    """True if any artist micro-genre structurally supports the predicted macro."""
    keywords = MACRO_KEYWORDS.get(predicted_macro)
    if not keywords:
        return False
    for micro in artist_micro_genres:
        for kw in keywords:
            if kw in micro:
                return True
    return False


# ── DB helpers ────────────────────────────────────────────────────────────────
def tune_pragmas(con: sqlite3.Connection) -> None:
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute("PRAGMA temp_store=MEMORY;")
    con.execute("PRAGMA cache_size=-262144;")   # ~256 MB page cache
    con.execute("PRAGMA mmap_size=1073741824;")  # 1 GB memory-mapped I/O


def ensure_checkpoint(wcon: sqlite3.Connection) -> int:
    wcon.execute(
        "CREATE TABLE IF NOT EXISTS ml_prediction_checkpoint ("
        " id INTEGER PRIMARY KEY CHECK(id=1),"
        " last_track_id INTEGER NOT NULL,"
        " processed INTEGER NOT NULL DEFAULT 0,"
        " accepted INTEGER NOT NULL DEFAULT 0)"
    )
    wcon.commit()
    row = wcon.execute(
        "SELECT last_track_id FROM ml_prediction_checkpoint WHERE id=1"
    ).fetchone()
    if row is None:
        wcon.execute(
            "INSERT INTO ml_prediction_checkpoint (id, last_track_id) VALUES (1, 0)"
        )
        wcon.commit()
        return 0
    return int(row[0])


SELECT_BATCH = f"""
SELECT af.track_id,
       af.danceability, af.energy, af."key", af.loudness, af.mode,
       af.speechiness, af.acousticness, af.instrumentalness, af.liveness,
       af.valence, af.tempo, t.duration_ms, af.time_signature
FROM track_audio_features af
JOIN tracks t ON t.track_id = af.track_id
WHERE af.track_id > ?
  AND NOT EXISTS (
      SELECT 1 FROM source_b_genres d WHERE d.track_id = af.track_id
  )
ORDER BY af.track_id
LIMIT ?;
"""


def fetch_artist_micro_genres(
    rcon: sqlite3.Connection, track_ids: list[int]
) -> dict[int, set[str]]:
    """Map each given track_id → set of normalized artist micro-genres.

    Uses a temp table + join (not a giant IN-clause) to avoid the SQLite
    bound-variable limit on large low-confidence batches.
    """
    rcon.execute("DELETE FROM tmp_lowconf;")
    rcon.executemany(
        "INSERT OR IGNORE INTO tmp_lowconf(track_id) VALUES (?)",
        [(tid,) for tid in track_ids],
    )
    out: dict[int, set[str]] = {}
    cur = rcon.execute(
        "SELECT ta.track_id, ag.genre "
        "FROM tmp_lowconf l "
        "JOIN track_artists ta ON ta.track_id = l.track_id "
        "JOIN artist_genres ag ON ag.artist_id = ta.artist_id"
    )
    for tid, genre in cur:
        if genre:
            out.setdefault(tid, set()).add(normalize_genre(genre))
    return out


# ── Audit mode: measure keyword coverage of the micro-genre vocabulary ────────
def audit_vocab(rcon: sqlite3.Connection, sample_rows: int = 5_000_000) -> None:
    print(f"Sampling distinct micro-genres from first {sample_rows:,} "
          "artist_genres rows...")
    cur = rcon.execute(
        "WITH s AS (SELECT genre FROM artist_genres LIMIT ?) "
        "SELECT genre, count(*) c FROM s GROUP BY genre ORDER BY c DESC",
        (sample_rows,),
    )
    rows = cur.fetchall()
    # A micro-genre is "covered" if it supports at least one macro.
    all_keywords = [(macro, kw) for macro, kws in MACRO_KEYWORDS.items() for kw in kws]
    covered = 0
    uncovered: list[tuple[str, int]] = []
    for genre, c in rows:
        norm = normalize_genre(genre)
        if any(kw in norm for _, kw in all_keywords):
            covered += 1
        else:
            uncovered.append((genre, c))
    total = len(rows)
    print(f"Distinct micro-genres: {total:,} | covered: {covered:,} "
          f"({covered/total*100:.1f}%) | uncovered: {len(uncovered):,}")
    print("\nTop 50 UNCOVERED micro-genres (consider adding keywords):")
    for genre, c in sorted(uncovered, key=lambda x: -x[1])[:50]:
        print(f"  {c:>8,}  {genre}")


# ── Inference backend (CPU LightGBM, or GPU via RAPIDS FIL) ───────────────────
def build_predictor(args: argparse.Namespace, booster: lgb.Booster):
    """Return a callable: X (float32, n×13) -> proba (n×20) as host numpy.

    --gpu uses NVIDIA FIL (RAPIDS cuML), which loads the SAME genre_gbdt.txt
    LightGBM model and runs the forest on the GPU. Falls through with a clear
    error if cuML can't be imported/loaded so we can fall back to CPU.
    """
    if not args.gpu:
        print("  backend: CPU (lightgbm.Booster.predict)")
        return lambda X: booster.predict(X)

    try:
        import cupy as cp
    except Exception:
        cp = None

    fil, errs = None, []
    # cuML's FIL import path has shifted across RAPIDS releases — try the knowns.
    loaders = [
        lambda: __import__("cuml.fil", fromlist=["ForestInference"]).ForestInference,
        lambda: __import__("cuml.fil.fil", fromlist=["ForestInference"]).ForestInference,
        lambda: __import__("cuml", fromlist=["ForestInference"]).ForestInference,
        lambda: __import__("cuml.experimental.fil", fromlist=["ForestInference"]).ForestInference,
    ]
    for loader in loaders:
        try:
            FIL = loader()
            fil = FIL.load(MODEL_PATH, model_type="lightgbm")
            break
        except Exception as e:  # noqa: BLE001 — surface every attempt
            errs.append(repr(e))
    if fil is None:
        sys.exit("✗ Could not initialise FIL GPU predictor. Tried:\n  "
                 + "\n  ".join(errs)
                 + "\n  (drop --gpu to use the CPU backend.)")
    print("  backend: GPU (RAPIDS FIL / ForestInference)")

    def predict(X):
        Xc = np.ascontiguousarray(X, dtype=np.float32)
        out = fil.predict_proba(Xc)
        if cp is not None and isinstance(out, cp.ndarray):
            out = out.get()
        return np.asarray(out)

    return predict


# ── Main inference loop ───────────────────────────────────────────────────────
def run(args: argparse.Namespace) -> None:
    for path in (DB_PATH, MODEL_PATH, SCALER_PATH, ENCODER_PATH):
        if not os.path.exists(path):
            sys.exit(f"✗ Missing required file: {path}")

    print("Loading model artifacts...")
    booster = lgb.Booster(model_file=MODEL_PATH)
    scaler = joblib.load(SCALER_PATH)
    le = joblib.load(ENCODER_PATH)
    assert booster.num_feature() == len(FEATURE_COLS), "feature count mismatch"
    assert scaler.n_features_in_ == len(FEATURE_COLS), "scaler feature mismatch"
    classes = np.asarray(le.classes_)
    print(f"  ✓ Booster ({booster.num_feature()} feats, "
          f"{booster.num_model_per_iteration()} classes), "
          f"scaler, encoder ({len(classes)} genres)")
    predict_fn = build_predictor(args, booster)

    # Two connections on one WAL file: rcon reads (+ temp), wcon writes (+ ckpt).
    rcon = sqlite3.connect(DB_PATH)
    wcon = sqlite3.connect(DB_PATH)
    tune_pragmas(rcon)
    tune_pragmas(wcon)
    rcon.execute("CREATE TEMP TABLE IF NOT EXISTS tmp_lowconf(track_id INTEGER PRIMARY KEY);")

    if args.audit_vocab:
        audit_vocab(rcon)
        rcon.close(); wcon.close()
        return

    cursor = ensure_checkpoint(wcon)
    if cursor > 0:
        print(f"  ↻ Resuming from checkpoint track_id > {cursor:,}")

    threshold = args.threshold
    insert_sql = (
        "INSERT OR IGNORE INTO ml_genre_predictions "
        "(track_id, predicted_genre, confidence_score, tiebreaker_applied, model_version) "
        "VALUES (?, ?, ?, ?, ?)"
    )

    tot_seen = tot_null = tot_acoustic = tot_cultural = tot_skipped = 0
    batch_no = 0
    wall_start = time.time()

    while True:
        if args.limit_batches and batch_no >= args.limit_batches:
            break
        t0 = time.time()
        rows = rcon.execute(SELECT_BATCH, (cursor, BATCH_SIZE)).fetchall()
        if not rows:
            break
        batch_no += 1

        ids = np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows))
        feats = np.array([r[1:] for r in rows], dtype=np.float64)  # (n, 13)
        batch_max_id = int(ids[-1])  # rows are ORDER BY track_id

        # Gate 0: drop rows with any NULL feature (None → NaN). Decision: skip.
        valid = ~np.isnan(feats).any(axis=1)
        n_null = int((~valid).sum())
        ids, feats = ids[valid], feats[valid]
        tot_seen += len(rows)
        tot_null += n_null

        accepted: list[tuple[int, str, float, int, str]] = []

        if len(ids):
            X = scaler.transform(feats.astype(np.float32)).astype(np.float32, copy=False)
            proba = predict_fn(X)                        # (n, 20) probabilities
            top_idx = proba.argmax(axis=1)
            top_p = proba.max(axis=1)
            macros = classes[top_idx]

            g1 = top_p >= threshold                     # Gate 1: acoustic pass
            for i in np.nonzero(g1)[0]:
                accepted.append((int(ids[i]), str(macros[i]), float(top_p[i]), 0, MODEL_VERSION))
            tot_acoustic += int(g1.sum())

            low = np.nonzero(~g1)[0]                     # Gate 2 candidates
            if len(low):
                low_ids = [int(ids[i]) for i in low]
                micro_map = fetch_artist_micro_genres(rcon, low_ids)
                for i in low:
                    tid = int(ids[i])
                    macro = str(macros[i])
                    micros = micro_map.get(tid)
                    if micros and gate2_supports(macro, micros):
                        accepted.append((tid, macro, float(top_p[i]), 1, MODEL_VERSION))
                        tot_cultural += 1
                    else:
                        tot_skipped += 1  # Gate 3: fall-through, leave NULL

        # Atomic write + checkpoint advance, one commit per batch.
        if accepted:
            wcon.executemany(insert_sql, accepted)
        wcon.execute(
            "UPDATE ml_prediction_checkpoint "
            "SET last_track_id=?, processed=processed+?, accepted=accepted+? WHERE id=1",
            (batch_max_id, len(rows), len(accepted)),
        )
        wcon.commit()
        cursor = batch_max_id

        dt = time.time() - t0
        rate = len(rows) / dt if dt else 0
        elapsed = time.time() - wall_start
        print(
            f"[batch {batch_no:>4}] cur={cursor:>12,} | {len(rows):>7,} rows "
            f"@ {rate:>7,.0f}/s | acoustic={tot_acoustic:,} cultural={tot_cultural:,} "
            f"skip={tot_skipped:,} null={tot_null:,} | elapsed={elapsed/60:.1f}m"
        )

    rcon.close()
    wcon.close()
    written = tot_acoustic + tot_cultural
    print("\n══════════════════ DONE ══════════════════")
    print(f"  Rows scanned     : {tot_seen:,}")
    print(f"  Dropped (NULL)   : {tot_null:,}")
    print(f"  Gate 1 acoustic  : {tot_acoustic:,}")
    print(f"  Gate 2 cultural  : {tot_cultural:,}")
    print(f"  Gate 3 skipped   : {tot_skipped:,}")
    print(f"  Written total    : {written:,}")
    print(f"  Wall time        : {(time.time()-wall_start)/60:.1f} min")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Two-Brain genre inference over master.db")
    p.add_argument("--threshold", type=float, default=0.55,
                   help="Gate 1 acoustic-acceptance probability (default 0.55)")
    p.add_argument("--limit-batches", type=int, default=0,
                   help="Process at most N batches (0 = all). For smoke tests.")
    p.add_argument("--audit-vocab", action="store_true",
                   help="Report micro-genre→macro keyword coverage, then exit.")
    p.add_argument("--gpu", action="store_true",
                   help="Run inference on GPU via RAPIDS FIL (needs cuml-cu12).")
    return p.parse_args()


if __name__ == "__main__":
    run(parse_args())
