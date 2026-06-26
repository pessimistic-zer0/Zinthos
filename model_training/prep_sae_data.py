"""
prep_sae_data.py — Stage 1 of F6 (Supervised Autoencoder) data prep
═══════════════════════════════════════════════════════════════════════════════
Turns labeled_tracks.csv (9.4 GB) into memory-mappable, pre-scaled numpy arrays
so each training epoch can mmap instead of re-parsing 9.4 GB of text every time.

Outputs (written next to this file):
  • sae_X.npy        float32 (N, 13)  — scaler-transformed features, C-contiguous
  • sae_y.npy        int8    (N,)     — encoded genre labels (0..19)
  • sae_prep_meta.json                — provenance + integrity record

Load-bearing invariants (DO NOT break — they make the 10-D space share the GBDT's
feature space; see model_training/F6_implementation_plan.md §6):
  • FEATURE_COLS order is fixed and matches genre_scaler.joblib's columns.
  • We REUSE genre_scaler.joblib (13-feature StandardScaler) — never refit.
  • Labels come from genre_label_encoder.joblib (the same 20-class encoder as F5).

Design notes:
  • Row count is established in ONE cheap pass (count '\\n'), then the .npy memmaps
    are preallocated exactly — so the streaming pass writes straight to disk and
    peak RAM stays under ~2 GB regardless of the 124.25M-row scale.
  • open_memmap (not bare np.memmap) writes a real .npy header so Stage 2 can do
    np.load(..., mmap_mode='r').
  • A genre-contiguity diagnostic is printed: it tells Stage 2 whether the simple
    per-epoch permutation is safe or whether we need block-shuffled prep.

Usage:
  python prep_sae_data.py                     # full run (~10-20 min)
  python prep_sae_data.py --limit-rows 200000 # fast smoke test (skips the count)
  python prep_sae_data.py --chunk-size 1000000
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone

import joblib
import numpy as np
import pandas as pd

# ── Paths (resolved relative to this file so cwd doesn't matter) ───────────────
HERE = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(HERE, "labeled_tracks.csv")
SCALER_PATH = os.path.join(HERE, "genre_scaler.joblib")          # 13-feature scaler
ENCODER_PATH = os.path.join(HERE, "genre_label_encoder.joblib")  # 20-class encoder
X_PATH = os.path.join(HERE, "sae_X.npy")
Y_PATH = os.path.join(HERE, "sae_y.npy")
META_PATH = os.path.join(HERE, "sae_prep_meta.json")

# Expected training-set size (model_training/CLAUDE.md, verified 2026-06-20). Used
# only as a sanity bound on the counted row total — abort if reality is way off.
EXPECTED_ROWS = 124_254_624

# Feature order is LOAD-BEARING: must match the scaler's columns and the CSV header.
FEATURE_COLS = [
    "danceability", "energy", "key", "loudness", "mode",
    "speechiness", "acousticness", "instrumentalness",
    "liveness", "valence", "tempo", "duration_ms", "time_signature",
]

# Stage 2's training batch size — the contiguity verdict is relative to this.
TRAIN_BATCH_REF = 16_384


# ── Cheap one-pass row counter ────────────────────────────────────────────────
def count_data_rows(path: str, block_mb: int = 16) -> int:
    """Count CSV data rows (= lines minus header) by scanning bytes for '\\n'.

    Handles a possibly-missing trailing newline. Fast (~30-60 s for 9.4 GB) and
    far cheaper than the parse pass it sizes the memmaps for.
    """
    newlines = 0
    last_byte = b""
    block = block_mb << 20
    with open(path, "rb") as f:
        while True:
            buf = f.read(block)
            if not buf:
                break
            newlines += buf.count(b"\n")
            last_byte = buf[-1:]
    total_lines = newlines if last_byte == b"\n" else newlines + 1
    return max(total_lines - 1, 0)  # subtract the header row


# ── Genre-contiguity diagnostic ───────────────────────────────────────────────
class RunTracker:
    """Longest contiguous run of each genre code across streamed chunks.

    If the global max run is >> the training batch size, sequential reads would
    yield genre-homogeneous batches and Stage 2 would need block-shuffled prep.
    """

    def __init__(self, n_classes: int):
        self.max_run = np.zeros(n_classes, dtype=np.int64)
        self.cur_code = -1
        self.cur_len = 0

    def update(self, codes: np.ndarray) -> None:
        if len(codes) == 0:
            return
        change = np.flatnonzero(np.diff(codes)) + 1
        starts = np.concatenate(([0], change))
        ends = np.concatenate((change, [len(codes)]))
        rcodes = codes[starts]
        rlens = (ends - starts).astype(np.int64)

        if self.cur_code == rcodes[0]:
            rlens[0] += self.cur_len          # this chunk continues the carried run
        elif self.cur_code >= 0:
            # carried run ended at the chunk boundary → finalize it
            self.max_run[self.cur_code] = max(self.max_run[self.cur_code], self.cur_len)

        if len(rcodes) > 1:                    # finalize every run except the trailing one
            np.maximum.at(self.max_run, rcodes[:-1], rlens[:-1])

        self.cur_code = int(rcodes[-1])        # carry the trailing run into the next chunk
        self.cur_len = int(rlens[-1])

    def finalize(self) -> None:
        if self.cur_code >= 0:
            self.max_run[self.cur_code] = max(self.max_run[self.cur_code], self.cur_len)


# ── Main ──────────────────────────────────────────────────────────────────────
def run(args: argparse.Namespace) -> None:
    for path in (CSV_PATH, SCALER_PATH, ENCODER_PATH):
        if not os.path.exists(path):
            sys.exit(f"✗ Missing required file: {path}")

    print("Loading scaler + label encoder...")
    scaler = joblib.load(SCALER_PATH)
    le = joblib.load(ENCODER_PATH)
    n_classes = len(le.classes_)
    assert scaler.n_features_in_ == len(FEATURE_COLS), (
        f"scaler expects {scaler.n_features_in_} features, FEATURE_COLS has {len(FEATURE_COLS)}"
    )
    genre_to_code = {g: i for i, g in enumerate(le.classes_)}
    print(f"  ✓ StandardScaler ({scaler.n_features_in_} feats) + "
          f"LabelEncoder ({n_classes} genres)")

    # ── Establish N and preallocate the .npy memmaps ──────────────────────────
    if args.limit_rows:
        N = int(args.limit_rows)
        print(f"\n  [smoke test] capping at N = {N:,} rows (skipping full count)")
    else:
        print("\nCounting CSV rows (one cheap byte-scan pass)...")
        t = time.time()
        N = count_data_rows(CSV_PATH)
        print(f"  ✓ {N:,} data rows in {time.time()-t:.0f}s")
        lo, hi = EXPECTED_ROWS * 0.99, EXPECTED_ROWS * 1.01
        if not (lo <= N <= hi):
            sys.exit(f"✗ Row count {N:,} is outside ±1% of the expected "
                     f"{EXPECTED_ROWS:,}. CSV may be truncated/wrong — aborting.")

    need_bytes = N * len(FEATURE_COLS) * 4 + N * 1
    free_bytes = shutil.disk_usage(HERE).free
    print(f"  Disk: need ~{need_bytes/1e9:.2f} GB, free {free_bytes/1e9:.1f} GB")
    if free_bytes < need_bytes + 2e9:
        sys.exit("✗ Not enough free disk (want output size + 2 GB margin).")

    print(f"  Allocating sae_X.npy {N:,}×{len(FEATURE_COLS)} float32 "
          f"(~{need_bytes/1e9:.2f} GB) + sae_y.npy int8...")
    X = np.lib.format.open_memmap(X_PATH, mode="w+", dtype=np.float32,
                                  shape=(N, len(FEATURE_COLS)))
    y = np.lib.format.open_memmap(Y_PATH, mode="w+", dtype=np.int8, shape=(N,))

    # ── Stream → transform → write ────────────────────────────────────────────
    print("\nStreaming CSV → scaler.transform → memmap...")
    hist = np.zeros(n_classes, dtype=np.int64)
    runs = RunTracker(n_classes)
    off = 0
    wall = time.time()
    reader = pd.read_csv(
        CSV_PATH,
        chunksize=args.chunk_size,
        usecols=FEATURE_COLS + ["genre"],
        dtype={c: np.float32 for c in FEATURE_COLS},
    )
    for chunk in reader:
        if off >= N:
            break
        Xc = chunk[FEATURE_COLS].to_numpy(np.float32, copy=False)  # enforce column order
        if np.isnan(Xc).any():
            bad = off + int(np.argmax(np.isnan(Xc).any(axis=1)))
            sys.exit(f"✗ NaN feature value near CSV data row {bad:,}. The CSV is "
                     "supposed to be NaN-free (NULL-feature rows were pre-dropped).")

        codes = chunk["genre"].map(genre_to_code)
        if codes.isna().any():
            unknown = sorted(set(chunk["genre"][codes.isna()]))[:10]
            sys.exit(f"✗ Genre(s) not in the 20-class encoder: {unknown} — aborting.")
        codes = codes.to_numpy(np.int64)

        k = len(chunk)
        if off + k > N:                 # cap the final chunk (smoke test or count drift)
            if not args.limit_rows:
                print(f"  ⚠ more rows than counted; capping at N={N:,}")
            k = N - off
            Xc, codes = Xc[:k], codes[:k]

        X[off:off + k] = scaler.transform(Xc).astype(np.float32, copy=False)
        y[off:off + k] = codes.astype(np.int8)
        hist += np.bincount(codes, minlength=n_classes)
        runs.update(codes)
        off += k

        elapsed = time.time() - wall
        rate = off / elapsed if elapsed else 0
        eta = (N - off) / rate / 60 if rate else 0
        print(f"  rows {off:>12,}/{N:,} | {rate:>9,.0f}/s | "
              f"elapsed {elapsed/60:4.1f}m | ETA {eta:4.1f}m", end="\r", flush=True)

    print()
    runs.finalize()
    X.flush(); y.flush()
    del X; del y

    if off != N:
        sys.exit(f"\n✗ Wrote {off:,} rows but allocated {N:,}. Arrays are misaligned; "
                 "re-run after investigating the CSV row count.")

    # ── Provenance / integrity record ─────────────────────────────────────────
    overall_max_run = int(runs.max_run.max())
    verdict = ("well-mixed (per-epoch permutation is safe)"
               if overall_max_run < TRAIN_BATCH_REF
               else "CLUMPED — Stage 2 should use block-shuffled prep")
    csv_stat = os.stat(CSV_PATH)
    meta = {
        "n": N,
        "feature_cols": FEATURE_COLS,
        "classes": list(le.classes_),
        "scaler_path": os.path.basename(SCALER_PATH),
        "x_path": os.path.basename(X_PATH), "x_dtype": "float32",
        "x_shape": [N, len(FEATURE_COLS)],
        "y_path": os.path.basename(Y_PATH), "y_dtype": "int8",
        "csv": {
            "path": os.path.basename(CSV_PATH),
            "size_bytes": csv_stat.st_size,
            "mtime_utc": datetime.fromtimestamp(csv_stat.st_mtime, timezone.utc).isoformat(),
        },
        "genre_histogram": {le.classes_[i]: int(hist[i]) for i in range(n_classes)},
        "contiguity": {
            "train_batch_ref": TRAIN_BATCH_REF,
            "max_run_overall": overall_max_run,
            "max_run_per_genre": {le.classes_[i]: int(runs.max_run[i])
                                  for i in range(n_classes)},
            "verdict": verdict,
        },
        "smoke_test": bool(args.limit_rows),
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "elapsed_sec": round(time.time() - wall, 1),
    }
    with open(META_PATH, "w") as f:
        json.dump(meta, f, indent=2)

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n══════════════════ STAGE 1 DONE ══════════════════")
    print(f"  Rows written     : {off:,}")
    print(f"  sae_X.npy        : {os.path.getsize(X_PATH)/1e9:.2f} GB")
    print(f"  sae_y.npy        : {os.path.getsize(Y_PATH)/1e6:.0f} MB")
    print(f"  Wall time        : {(time.time()-wall)/60:.1f} min")
    print(f"  Max genre run    : {overall_max_run:,} rows "
          f"(batch ref {TRAIN_BATCH_REF:,}) → {verdict}")
    print("  Genre distribution:")
    for i in np.argsort(hist)[::-1]:
        pct = hist[i] / off * 100 if off else 0
        bar = "█" * int(pct / 2)
        print(f"    {le.classes_[i]:<12s} {hist[i]:>12,} ({pct:4.1f}%) {bar}")
    print(f"\n  ✓ meta → {os.path.basename(META_PATH)}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="F6 Stage 1: CSV → scaled mmap arrays")
    p.add_argument("--chunk-size", type=int, default=2_000_000,
                   help="pandas read_csv chunk size (default 2,000,000)")
    p.add_argument("--limit-rows", type=int, default=0,
                   help="Cap N for a fast smoke test (skips the full row count).")
    return p.parse_args()


if __name__ == "__main__":
    run(parse_args())
