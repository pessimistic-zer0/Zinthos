"""
embed_tracks.py — Stage 3 of F6: embed every featured track with the SAE encoder
═══════════════════════════════════════════════════════════════════════════════
Runs the trained encoder (sae_encoder.pt) over ALL ~254.82M tracks that have audio
features — labeled, F5-predicted, and NULL-genre alike (the genre label is irrelevant
at inference) — and writes a 10-D vector per track to `ml_10d_embeddings`.

This is the long pole of F6: GPU compute is trivial (a <8k-param MLP), so SQLite read +
BLOB write throughput dominates. Expect HOURS. It is fully resumable.

Per-batch pipeline (mirrors predict_genres.py's scale strategy):
  • Keyset (cursor) pagination on track_id — NOT LIMIT/OFFSET.
  • Drop rows with any NULL/NaN feature (un-embeddable — a full 13-vector is required).
  • scaler.transform (the SAME genre_scaler.joblib used everywhere) → encoder(eval) on GPU.
  • Encode each 10-D vector to a 40-byte little-endian float32 BLOB; store RAW (FAISS
    normalizes for cosine downstream — see F6_implementation_plan.md §4/§5).
  • Also stream raw vectors + ids to aligned flat files (embeddings.f32 / embed_ids.i64) so
    Stage 4 never re-scans the 129 GB DB. Flat write happens BEFORE the DB commit so a crash
    can only leave the flat files LONGER than the checkpoint, which resume truncates back.
  • One transaction per batch (WAL auto-checkpoints; no unbounded WAL growth).

Resumable via an auto-created `ml_embedding_checkpoint` row (keyset on track_id).

Usage:
  python embed_tracks.py                    # full run (GPU), writes DB + flat files
  python embed_tracks.py --limit-batches 2  # smoke test
  python embed_tracks.py --no-flatfiles     # DB only (Stage 4 regenerates flats from DB)
  python embed_tracks.py --cpu              # force CPU (smoke only)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time

import joblib
import numpy as np
import torch

from train_sae import SAE  # reuse the exact architecture; import has no side effects

# ── Paths (resolved relative to this file) ────────────────────────────────────
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB_PATH = os.path.join(ROOT, "master.db")
ENCODER_PATH = os.path.join(HERE, "sae_encoder.pt")
CONFIG_PATH = os.path.join(HERE, "sae_config.json")
EMB_F32 = os.path.join(HERE, "embeddings.f32")     # raw float32 vectors, 10/track
IDS_I64 = os.path.join(HERE, "embed_ids.i64")      # int64 track_ids, aligned with EMB_F32

BATCH_SIZE = 250_000
EXPECTED_FEATURED = 254_819_856                     # for ETA display only
F32_ITEM = 10 * 4                                   # bytes per embedding row (10 × float32)
I64_ITEM = 8                                        # bytes per id row

# Feature order is LOAD-BEARING: must match the scaler, the CSV, and the encoder's training.
# duration_ms (index 11) lives in `tracks`, the other 12 in `track_audio_features`.
FEATURE_COLS = [
    "danceability", "energy", "key", "loudness", "mode",
    "speechiness", "acousticness", "instrumentalness",
    "liveness", "valence", "tempo", "duration_ms", "time_signature",
]

# NOTE: NO `source_b` filter — we embed EVERY featured track. Keyset pagination on track_id.
SELECT_BATCH = f"""
SELECT af.track_id,
       af.danceability, af.energy, af."key", af.loudness, af.mode,
       af.speechiness, af.acousticness, af.instrumentalness, af.liveness,
       af.valence, af.tempo, t.duration_ms, af.time_signature
FROM track_audio_features af
JOIN tracks t ON t.track_id = af.track_id
WHERE af.track_id > ?
ORDER BY af.track_id
LIMIT ?;
"""


def tune_pragmas(con: sqlite3.Connection) -> None:
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute("PRAGMA temp_store=MEMORY;")
    con.execute("PRAGMA cache_size=-262144;")    # ~256 MB page cache
    con.execute("PRAGMA mmap_size=1073741824;")  # 1 GB memory-mapped I/O


def ensure_checkpoint(wcon: sqlite3.Connection) -> tuple[int, int]:
    wcon.execute(
        "CREATE TABLE IF NOT EXISTS ml_embedding_checkpoint ("
        " id INTEGER PRIMARY KEY CHECK(id=1),"
        " last_track_id INTEGER NOT NULL,"
        " processed INTEGER NOT NULL DEFAULT 0,"
        " embedded INTEGER NOT NULL DEFAULT 0,"
        " skipped_nan INTEGER NOT NULL DEFAULT 0)"
    )
    wcon.commit()
    row = wcon.execute(
        "SELECT last_track_id, embedded FROM ml_embedding_checkpoint WHERE id=1"
    ).fetchone()
    if row is None:
        wcon.execute("INSERT INTO ml_embedding_checkpoint (id, last_track_id) VALUES (1, 0)")
        wcon.commit()
        return 0, 0
    return int(row[0]), int(row[1])


def load_encoder(device: str):
    cfg = json.load(open(CONFIG_PATH))
    assert cfg["feature_cols"] == FEATURE_COLS, "config feature order != FEATURE_COLS"
    sae = SAE(in_dim=cfg["in_dim"], emb_dim=cfg["emb_dim"], n_classes=cfg["n_classes"])
    sae.encoder.load_state_dict(torch.load(ENCODER_PATH, map_location="cpu"))
    enc = sae.encoder.to(device).eval()
    return enc, cfg


def open_flat(path: str, item: int, keep_rows: int, fresh: bool):
    """Open a flat file for appending, aligned to `keep_rows` already-committed rows."""
    if fresh or not os.path.exists(path):
        return open(path, "wb")
    have = os.path.getsize(path)
    if have < keep_rows * item:
        sys.exit(f"✗ {os.path.basename(path)} has {have} B but checkpoint expects "
                 f"≥ {keep_rows*item} B. Flat/DB misaligned — rerun with --no-flatfiles "
                 "or delete the flat files to rebuild them from the DB in Stage 4.")
    f = open(path, "r+b")
    f.truncate(keep_rows * item)      # drop any rows from an uncommitted crashed batch
    f.seek(keep_rows * item)
    return f


def run(args: argparse.Namespace) -> None:
    for p in (DB_PATH, ENCODER_PATH, CONFIG_PATH):
        if not os.path.exists(p):
            sys.exit(f"✗ Missing required file: {p}")

    device = "cpu" if args.cpu or not torch.cuda.is_available() else "cuda"
    if device == "cpu" and not args.cpu:
        print("⚠ CUDA unavailable — running encoder on CPU.")

    enc, cfg = load_encoder(device)
    model_version = cfg["model_version"]
    scaler = joblib.load(os.path.join(HERE, cfg["scaler_path"]))
    assert scaler.n_features_in_ == len(FEATURE_COLS), "scaler feature mismatch"
    print(f"  ✓ encoder ({cfg['emb_dim']}-D, α={cfg['alpha']}) + scaler ({scaler.n_features_in_} feats)"
          f" | model_version={model_version} | device={device}")

    free_gb = shutil.disk_usage(HERE).free / 1e9
    print(f"  Disk free: {free_gb:.0f} GB (need ~28 GB: DB +~16, flats +~12)")
    if free_gb < 40:
        sys.exit("✗ < 40 GB free — clear space before the full embedding run.")

    rcon = sqlite3.connect(DB_PATH)
    wcon = sqlite3.connect(DB_PATH)
    tune_pragmas(rcon)
    tune_pragmas(wcon)

    cursor, embedded_so_far = ensure_checkpoint(wcon)
    if cursor > 0:
        print(f"  ↻ Resuming: track_id > {cursor:,}, {embedded_so_far:,} already embedded")

    f_vec = f_ids = None
    if not args.no_flatfiles:
        fresh = cursor == 0
        f_vec = open_flat(EMB_F32, F32_ITEM, embedded_so_far, fresh)
        f_ids = open_flat(IDS_I64, I64_ITEM, embedded_so_far, fresh)

    insert_sql = ("INSERT OR IGNORE INTO ml_10d_embeddings "
                  "(track_id, vector_blob, model_version) VALUES (?, ?, ?)")

    tot_proc = tot_emb = tot_nan = 0
    batch_no = 0
    wall = time.time()

    while True:
        if args.limit_batches and batch_no >= args.limit_batches:
            break
        t0 = time.time()
        rows = rcon.execute(SELECT_BATCH, (cursor, BATCH_SIZE)).fetchall()
        if not rows:
            break
        batch_no += 1

        ids = np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows))
        feats = np.array([r[1:] for r in rows], dtype=np.float64)   # (n, 13)
        batch_max_id = int(ids[-1])                                  # rows ORDER BY track_id

        valid = ~np.isnan(feats).any(axis=1)
        n_nan = int((~valid).sum())
        ids_v, feats_v = ids[valid], feats[valid]

        if len(ids_v):
            X = scaler.transform(feats_v.astype(np.float32)).astype(np.float32, copy=False)
            with torch.no_grad():
                xb = torch.from_numpy(X).to(device, non_blocking=True)
                emb = enc(xb).cpu().numpy()                          # (n, 10) float32
            emb = np.ascontiguousarray(emb, dtype="<f4")             # little-endian raw

            # 1) flat files FIRST (so a crash leaves flats ≥ checkpoint, which resume truncates)
            if f_vec is not None:
                f_vec.write(emb.tobytes())
                f_ids.write(np.ascontiguousarray(ids_v, dtype="<i8").tobytes())
                f_vec.flush(); f_ids.flush()
            # 2) DB rows
            payload = [(int(ids_v[i]), emb[i].tobytes(), model_version) for i in range(len(ids_v))]
            wcon.executemany(insert_sql, payload)

        # 3) advance checkpoint + commit (atomic per batch)
        wcon.execute(
            "UPDATE ml_embedding_checkpoint SET last_track_id=?, processed=processed+?, "
            "embedded=embedded+?, skipped_nan=skipped_nan+? WHERE id=1",
            (batch_max_id, len(rows), len(ids_v), n_nan),
        )
        wcon.commit()
        cursor = batch_max_id

        tot_proc += len(rows); tot_emb += len(ids_v); tot_nan += n_nan
        dt = time.time() - t0
        rate = len(rows) / dt if dt else 0
        done = embedded_so_far + tot_emb
        eta_h = (EXPECTED_FEATURED - cursor) / rate / 3600 if rate else 0
        print(f"[batch {batch_no:>5}] cur={cursor:>12,} | {len(rows):>7,} rows @ {rate:>7,.0f}/s "
              f"| emb={tot_emb:,} nan={tot_nan:,} | done≈{done:,} | "
              f"elapsed={(time.time()-wall)/60:.1f}m ETA≈{eta_h:.1f}h")

    if f_vec is not None:
        f_vec.close(); f_ids.close()
    rcon.close(); wcon.close()

    print("\n══════════════════ STAGE 3 DONE ══════════════════")
    print(f"  Rows processed   : {tot_proc:,}")
    print(f"  Embedded (this run): {tot_emb:,}")
    print(f"  Skipped (NaN)    : {tot_nan:,}")
    print(f"  Total embedded   : {embedded_so_far + tot_emb:,}")
    if f_vec is not None:
        print(f"  embeddings.f32   : {os.path.getsize(EMB_F32)/1e9:.2f} GB")
        print(f"  embed_ids.i64    : {os.path.getsize(IDS_I64)/1e9:.2f} GB")
    print(f"  Wall time        : {(time.time()-wall)/60:.1f} min")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="F6 Stage 3: embed all featured tracks")
    p.add_argument("--limit-batches", type=int, default=0, help="process at most N batches (smoke)")
    p.add_argument("--no-flatfiles", action="store_true", help="write DB only (skip flat files)")
    p.add_argument("--cpu", action="store_true", help="force CPU encoder (smoke only)")
    return p.parse_args()


if __name__ == "__main__":
    run(parse_args())
