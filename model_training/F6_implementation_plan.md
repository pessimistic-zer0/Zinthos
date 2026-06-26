# F6 Implementation Plan — Supervised Autoencoder → 10-D Embeddings

> **Status:** plan finalized 2026-06-20. F5 (genre prediction) is done. This document is the
> authoritative, code-ready plan for F6. It supersedes the loose notes in the kickoff brief.
>
> **One-line goal:** compress the 13 audio features into a genre-coherent **10-D** embedding,
> write it for all ~254.82M featured tracks, and index it in FAISS for "sounds-similar" search.

---

## 0. Grounded environment & dataset facts (verified 2026-06-20)

Everything below was checked against the live machine and `master.db`, not assumed.

| Resource | Reality | Consequence for this plan |
|---|---|---|
| **GPU** | RTX 4050 Laptop, **6.05 GB VRAM**, CUDA via torch `2.12.0+cu130` ✓ | The SAE is a sub-100k-param MLP → VRAM is a **non-constraint**. Bottleneck is data I/O. Use large batches. |
| **RAM** | 15 GB total, **~9 GB free**, 19 GB swap | `sae_X.npy` is 6.46 GB → **mmap + OS page cache** is the default. Do *not* force a full-RAM load (no headroom for perm index + torch). |
| **CPU** | 12 cores | FAISS CPU build and pandas prep parallelize well here. |
| **Disk** | 164 GB free on `/home` (already 82% used) | ~33 GB of new artifacts fits, but headroom is the thing to watch. Preflight-check it. |
| **faiss** | **NOT installed** | Stage 4 needs `pip install faiss-cpu`. |
| **cuML / FIL** | 26.06 ✓ | Not needed for F6 (was for F5 GPU inference); leave as-is. |
| **Scaler** | `genre_scaler.joblib` = `StandardScaler`, `n_features_in_=13` | **Reuse verbatim. Never refit.** This is what makes the embedding space share the GBDT's feature space. (NOT `genre_scaler_v2.joblib`.) |
| **Label encoder** | `genre_label_encoder.joblib`, 20 classes (alphabetical) | Reuse verbatim. Classes below. |
| **CSV** | `labeled_tracks.csv`, 9.4 GB, header = 13 features + `genre`, rows in `track_id` scan order | Genres are well-mixed: Stage 1 measured max contiguous run **4,605** « batch 16,384 → per-epoch permutation is safe (no block-shuffle). |
| **master.db** | 129 GB at repo root, WAL mode | `DB_PATH = <repo_root>/master.db`. |

> **Stage 1 is COMPLETE** (2026-06-20). Counted exactly **124,254,624** rows; wrote `sae_X.npy`
> (6.46 GB), `sae_y.npy` (124 MB), `sae_prep_meta.json` in 1.7 min. No NaN; labels 0–19; scaling
> verified. Full genre histogram is heavily imbalanced (~75×: pop 19.1% … *other* 0.3%) — see §3.

**The 20 macro genres (encoder order — load-bearing for the classifier head):**
```
['african', 'alternative', 'asian', 'christian', 'classical', 'country', 'dance',
 'electronic', 'folk', 'hip-hop', 'jazz', 'kids', 'latin', 'metal', 'other',
 'pop', 'r&b', 'reggae', 'rock', 'soundtrack']
```

**Feature order (LOAD-BEARING — must match the scaler and `track_audio_features`):**
```python
FEATURE_COLS = [
    "danceability", "energy", "key", "loudness", "mode",
    "speechiness", "acousticness", "instrumentalness",
    "liveness", "valence", "tempo", "duration_ms", "time_signature",
]
```
`duration_ms` (index 11) lives in `tracks`, the other 12 in `track_audio_features`. The CSV
already has all 13 in this order; only Stage 3 (DB re-query) must JOIN `tracks` for `duration_ms`.

**Verified row counts:**
- Featured tracks (embedding universe): **254,819,856**
- Labeled AND featured (training universe): **124,702,033**
- `labeled_tracks.csv` rows (the training set): **124,254,624**
- Labeled but featureless (un-embeddable, correctly excluded): 554,350

---

## 1. Architecture & the "why"

Two heads off **one 10-D bottleneck**, trained jointly, then we keep **only the encoder**.

```
                       ┌──────────► DECODER (10→32→64→13) ──► x̂   ── reconstruction loss (MSE)
 x (13) ─► ENCODER ─► [ 10-D ]
                       └──────────► CLASSIFIER (10→20) ─────► ŷ   ── classification loss (CE)

 total_loss = MSE_meanperfeat(x̂, x) + α · CE_weighted(logits, y)     # α = 0.5 (chosen empirically, §3/§7)
```

- **Reconstruction** keeps the 10-D faithful to *how the track sounds* (no labels needed).
- **Classification** keeps it *genre-coherent* (uses the 124.25M ground-truth labels).
- **Empirical finding (the α sweep):** the two objectives barely compete — reconstructing 13
  correlated features from 10 dims is *easy*, so recon stays near-lossless (~0.005–0.014 MSE)
  across α ∈ {0.01, 0.1, 0.5}. Since acoustic fidelity is essentially "free," α just controls how
  much **genre structure** to impose on top, and more helps (without collapsing). Hence α=0.5.
- The **tension between the two** is what makes the embedding good: tracks close in 10-D should
  both sound alike *and* tend to share a genre.
- The classifier is deliberately **shallow** (a single `Linear(10→20)`). A deep head would absorb
  the genre signal itself and let the bottleneck off the hook; keeping it shallow forces the
  *bottleneck* to be linearly genre-separable.
- The bottleneck ends in **BatchNorm1d(10), no nonlinear activation**. The BN gives the 10 dims
  ≈unit variance *for free* (baked into the encoder, deterministic via running stats at inference),
  so no single dim dominates distances. Similarity is **cosine** (L2-normalize at index/query
  time); the DB stores the **raw** vector so we never lose information.

**Training labels = ground-truth Source B genres only (the CSV).** We do **NOT** train on F5's
predicted genres: they're a function of the same 13 features (circular — the SAE would just
imitate the GBDT) and ~45% are wrong (would distort the very distances we optimize).

### Model definition (PyTorch, exact)

```python
import torch.nn as nn

class SAE(nn.Module):
    def __init__(self, in_dim=13, emb_dim=10, n_classes=20):
        super().__init__()
        self.encoder = nn.Sequential(
            nn.Linear(in_dim, 64), nn.BatchNorm1d(64), nn.ReLU(),
            nn.Linear(64, 32),     nn.BatchNorm1d(32), nn.ReLU(),
            nn.Linear(32, emb_dim), nn.BatchNorm1d(emb_dim),  # bottleneck: BN, NO activation
        )
        self.decoder = nn.Sequential(
            nn.Linear(emb_dim, 32), nn.BatchNorm1d(32), nn.ReLU(),
            nn.Linear(32, 64),      nn.BatchNorm1d(64), nn.ReLU(),
            nn.Linear(64, in_dim),
        )
        self.classifier = nn.Linear(emb_dim, n_classes)   # shallow on purpose

    def forward(self, x):
        z = self.encoder(x)
        return z, self.decoder(z), self.classifier(z)
```

Param count ≈ 13·64 + 64·32 + 32·10 + (mirror) + 10·20 ≈ **<15k params** → trivial on 6 GB.

---

## 2. Stage 1 — Data prep  →  `prep_sae_data.py`

**Purpose:** turn the 9.4 GB CSV into memory-mappable, pre-scaled numpy arrays so each training
epoch mmaps instead of re-parsing 9.4 GB of text.

**Inputs:** `labeled_tracks.csv`, `genre_scaler.joblib`, `genre_label_encoder.joblib`
**Outputs:**
- `sae_X.npy` — float32, shape `(N, 13)`, **scaler-transformed**, C-contiguous.
- `sae_y.npy` — int8, shape `(N,)`, encoded genre labels (0–19).
- `sae_prep_meta.json` — provenance + integrity record (see schema below).

**Algorithm:**
1. **Count rows once:** `N = wc -l(CSV) - 1` (expect **124,254,624**; assert within ±1% and abort
   on mismatch — protects against a truncated/partial CSV).
2. **Preallocate real `.npy` memmaps** so Stage 2 can `np.load(mmap_mode='r')`:
   ```python
   X = np.lib.format.open_memmap("sae_X.npy", mode="w+", dtype=np.float32, shape=(N, 13))
   y = np.lib.format.open_memmap("sae_y.npy", mode="w+", dtype=np.int8,   shape=(N,))
   ```
   (`open_memmap` writes a valid `.npy` header — a bare `np.memmap` would not.)
3. **Validate the label vocabulary up front:** read the distinct `genre` values (cheap, one pass
   or from a sampled scan) and assert ⊆ the 20 encoder classes. Fail **loud** — never silently drop.
4. **Stream + transform** with `pd.read_csv(chunksize=1_000_000, usecols=FEATURE_COLS+['genre'])`:
   ```python
   off = 0
   for chunk in reader:
       Xc = chunk[FEATURE_COLS].to_numpy(np.float32)
       assert not np.isnan(Xc).any(), f"NaN feature at rows {off}..{off+len(chunk)}"
       X[off:off+len(chunk)] = scaler.transform(Xc).astype(np.float32, copy=False)
       y[off:off+len(chunk)] = le.transform(chunk["genre"].to_numpy())   # raises on unseen → good
       off += len(chunk)
       # accumulate: genre histogram, NaN count, per-genre max contiguous run length
   assert off == N, f"row count drift: wrote {off}, expected {N}"
   X.flush(); y.flush()
   ```
5. **Write `sae_prep_meta.json`** (schema):
   ```json
   {
     "n": 124254624, "feature_cols": [...13...], "classes": [...20...],
     "scaler_path": "genre_scaler.joblib", "X_dtype": "float32", "y_dtype": "int8",
     "csv_size_bytes": 9439054991, "csv_mtime": "2025-03-25T15:21:00",
     "genre_histogram": {"pop": ..., "rock": ...}, "created_utc": "2026-06-20T..."
   }
   ```
6. **Print the genre-contiguity diagnostic** (max contiguous run per genre). This decides the
   Stage-2 shuffle strategy: if runs are short (expected, since the CSV is in `track_id` order),
   the simple per-epoch permutation is fine; if any genre forms a huge contiguous block, switch
   Stage 1 to block-shuffled writes (see Stage 2 fallback).

**Runtime:** ~10–20 min (pandas-bound). **Peak RAM:** < 2 GB. **Disk written:** 6.46 GB + 124 MB.

**Idempotency:** re-running overwrites the memmaps cleanly. Cheap to redo; not checkpointed.

---

## 3. Stage 2 — Train the SAE  →  `train_sae.py`

**Inputs:** `sae_X.npy`, `sae_y.npy`, `sae_prep_meta.json`, `genre_scaler.joblib` (for the config record).
**Outputs:**
- `sae_encoder.pt` — encoder `state_dict` **only** (decoder + classifier are discarded after training).
- `sae_config.json` — everything Stage 3 needs to reconstruct the encoder + reproduce the run.
- `sae_curves.png` — train/val reconstruction MSE, classification CE, and val genre-accuracy.

**Validation split:** deterministic **modulo holdout** — `val = (row_index % 100 == 0)` → ~1.24M
val rows. Reproducible, requires no shuffling of the file, and is stable across reruns.

**Training loop (manual batching — matches the codebase's explicit-loop style):**
```python
X = np.load("sae_X.npy", mmap_mode="r")        # 6.46 GB, page-cached after epoch 1
y = np.load("sae_y.npy", mmap_mode="r")
model = SAE().cuda()
opt = torch.optim.AdamW(model.parameters(), lr=1e-3, weight_decay=1e-5)
sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=EPOCHS * steps_per_epoch)
mse = nn.MSELoss()                              # mean over all elements → mean-per-feature
ce  = nn.CrossEntropyLoss(weight=class_w)       # class_w = sqrt-inv-freq, mean-normalized, cap 10×

for epoch in range(EPOCHS):
    perm = np.random.permutation(N)            # 124M int64 ≈ 1 GB, fits
    perm = perm[~is_val_mask[perm]]            # train rows only
    for batch_idx in chunks(perm, BATCH):
        xb = torch.from_numpy(X[batch_idx]).pin_memory().cuda(non_blocking=True)
        yb = torch.from_numpy(y[batch_idx].astype(np.int64)).pin_memory().cuda(non_blocking=True)
        z, xhat, logits = model(xb)
        recon, clf = mse(xhat, xb), ce(logits, yb)
        loss = recon + ALPHA * clf              # log BOTH terms raw — tune α so neither collapses
        opt.zero_grad(); loss.backward(); opt.step(); sched.step()
    evaluate_on_val(...)                        # recon MSE, CE, overall + per-class genre-acc
```

**Hyperparameters (final values — these are the "knobs", justified in §7):**

| Param | Value | Notes |
|---|---|---|
| `ALPHA` (α) | **0.5** | Chosen by sweep (see below), NOT the loss-scale guess. recon turned out ~30× lower than predicted (≈0.005), CE higher (~2.0), so genre dominates the gradient at every α — but recon stays near-lossless regardless, so higher α is a free win on genre coherence. |
| Embedding metric | **cosine** | L2-normalize at index/query; DB stores raw. BN at bottleneck makes the space well-conditioned for it. |
| Class weights | **sqrt-inv-freq, mean-normalized, cap 10×, ON** | Mean-normalization keeps the CE scale (and thus effective α) stable; see note below. |
| `BATCH` / lr | 16384 / 1e-3 | VRAM is free; can raise BATCH→65536 if epoch wall-time matters (it won't — page-cache-bound). |
| `EPOCHS` | **5 max + early stop** | Expect to stop ~epoch 2 (a <15k-param net on 124M rows saturates fast). Patience ½ epoch on val total loss. |
| Optimizer | AdamW, lr 1e-3, wd 1e-5 | Cosine decay over all steps. (wd barely matters — can't overfit at 124M : 15k.) |
| AMP | off | Pointless for a <15k-param MLP; would add overhead. |

**Class imbalance (confirmed by Stage 1's full-file histogram):** the 20 genres span ~75×
(pop 23.78M / 19.1% … *other* 316k / 0.3%). Only the **classifier head** is affected (the
reconstruction head is label-agnostic). Use `CrossEntropyLoss(weight=w)` with `w` =
sqrt-inverse-frequency, normalized and capped (e.g. ≤10×), so rare genres (african, country,
christian, other) aren't swallowed by their acoustic neighbors in the 10-D space — without
letting them dominate the loss. It's a knob: compare val genre-accuracy *per class* (not just
overall) with weights on vs. off. The full histogram lives in `sae_prep_meta.json`.

**α sweep result (2026-06-20, seed 42, trained to convergence ~6 min each):**

| α | val recon MSE | val genre-acc | verdict |
|---|---|---|---|
| 0.01 | 0.0016 | 40.9% | weakest genre structure |
| 0.1 | 0.0046 | 43.2% | — |
| **0.5** | **0.0136** | **45.0%** | **chosen** |

Reconstruction is near-lossless at all three (≤~12% per-feature error even at 0.5), so the two
objectives barely compete; α only trades genre coherence, which climbs monotonically. Per-class,
α=0.5's gains land on the *hard* genres (soundtrack 5→19%, kids 19→27%, asian 24→29%, reggae
8→15%, dance 8→14%) while the easy/distinct ones stay flat. 45% linear-probe accuracy is far from
the ~100% of a collapsed (20-blob) space, and recon proves per-track detail is preserved → not
collapsing. **Decision: α=0.5.** (Would not go to 1.0 — diminishing genre gain, rising recon /
over-separation risk.)

**Indexing the mmap by a random permutation** does scattered reads, but `sae_X.npy` (6.46 GB)
fits the OS page cache (15 GB RAM), so after epoch 1 reads are RAM-speed.
**Fallback if epoch time is I/O-bound** (verify with the diagnostic): change Stage 1 to write in
**block-shuffled** order (buffer ~8M rows, shuffle in RAM, write sequentially) and read contiguous
batches with per-epoch block-offset jitter. Not the default; only if measured I/O demands it.

**`sae_config.json` schema:**
```json
{
  "model_version": "autoencoder_v1",
  "in_dim": 13, "emb_dim": 10, "n_classes": 20,
  "encoder_widths": [13, 64, 32, 10], "decoder_widths": [10, 32, 64, 13],
  "bottleneck_batchnorm": true, "classifier": "linear",
  "feature_cols": [...13...], "scaler_path": "genre_scaler.joblib",
  "alpha": 0.5, "batch": 16384, "lr": 0.001, "epochs_max": 5,
  "class_weighting": "sqrt_inv_freq_cap10_meannorm",
  "embedding_metric": "cosine", "blob_dtype": "<f4", "blob_stores": "raw",
  "val_recon_mse": ..., "val_genre_acc": ..., "best_epoch": ..., "trained_utc": "..."
}
```

**Success criteria (sanity, not a hard gate):** val reconstruction MSE clearly below a
PCA-to-10D baseline, and val genre-accuracy in the same ballpark as the GBDT (~50%) — if the
embedding can linearly recover genre about as well as the full GBDT, it's genre-coherent enough.

---

## 4. Stage 3 — Embed all featured tracks  →  `embed_tracks.py`

**This is the long pole of F6** (I/O-bound write of ~16 GB into a 129 GB WAL DB). GPU compute is
trivial; SQLite write throughput dominates. Expect *hours*. Built to be **resumable**.

**Inputs:** `sae_encoder.pt`, `sae_config.json`, `genre_scaler.joblib`, `master.db`.
**Outputs:**
- Fills **`ml_10d_embeddings(track_id, vector_blob, model_version)`**.
- Streams two aligned flat files for Stage 4 (so it never re-scans the 129 GB DB):
  - `embeddings.f32` — raw float32 vectors, 254.82M × 40 B ≈ **10.2 GB**.
  - `embed_ids.i64` — int64 track_ids, 254.82M × 8 B ≈ **2 GB**.

**Query — reuse `predict_genres.py`'s `SELECT_BATCH` but DROP the `NOT EXISTS source_b` filter**
(we embed *every* featured track — labeled, F5-predicted, and NULL alike; the label is irrelevant
at inference):
```sql
SELECT af.track_id,
       af.danceability, af.energy, af."key", af.loudness, af.mode,
       af.speechiness, af.acousticness, af.instrumentalness, af.liveness,
       af.valence, af.tempo, t.duration_ms, af.time_signature
FROM track_audio_features af
JOIN tracks t ON t.track_id = af.track_id
WHERE af.track_id > ?            -- keyset (cursor) pagination, NOT LIMIT/OFFSET
ORDER BY af.track_id
LIMIT ?;                          -- BATCH_SIZE = 250_000
```

**Per-batch pipeline:**
1. Drop rows with any NaN feature (un-embeddable — a full vector is required). Count them.
2. `scaler.transform(feats)` → float32.
3. `encoder.eval()`, under `torch.no_grad()`, on GPU → `(n, 10)` float32, copied to host.
4. Encode each vector to a 40-byte BLOB: `vec.astype('<f4').tobytes()` (**little-endian float32**,
   documented so the serving layer / FAISS reader agrees).
5. `executemany("INSERT OR IGNORE INTO ml_10d_embeddings(track_id, vector_blob, model_version) VALUES (?,?,?)", rows)`
   with `model_version = "autoencoder_v1"`.
6. Append the batch's vectors to `embeddings.f32` and ids to `embed_ids.i64`.
7. Advance `ml_embedding_checkpoint` and **commit once per batch** (one transaction per batch →
   WAL auto-checkpoints, doesn't grow unbounded — same pattern as `predict_genres.py`).

**Resumability** (new checkpoint table, mirrors `ml_prediction_checkpoint`):
```sql
CREATE TABLE IF NOT EXISTS ml_embedding_checkpoint (
  id INTEGER PRIMARY KEY CHECK(id=1),
  last_track_id INTEGER NOT NULL,
  processed INTEGER NOT NULL DEFAULT 0,
  embedded  INTEGER NOT NULL DEFAULT 0,
  skipped_nan INTEGER NOT NULL DEFAULT 0
);
```
On resume, also **truncate the flat files** back to `embedded * 40` / `* 8` bytes so they stay
aligned with the DB (or rebuild them from the DB if missing — see note). Use two connections on
the WAL file (rcon reads, wcon writes + checkpoint), `tune_pragmas` identical to `predict_genres.py`.

**CLI:** `--limit-batches N` (smoke test), `--no-flatfiles` (DB only), `--batch-size`.

**Note on the flat files vs. DB:** the DB BLOBs are the source of truth for the serving layer
(join track_id → vector). The flat files are a *build convenience* for Stage 4 and can be deleted
afterward; if absent, Stage 4 can regenerate them by scanning `ml_10d_embeddings`.

---

## 5. Stage 4 — Build the FAISS index  →  `build_faiss.py`

> **BUILD RESULT (2026-06-21) — shipped: `IVFSQfp16`, NOT IVFPQ.**
> A quantizer sweep on a 5M subset killed the PRD's IVFPQ default and settled the index type:
>
> | index | recall@10 (5M) | full-scale size* | verdict |
> |---|---|---|---|
> | IVFPQ (m=5,nbits=8) | 29% | ~5.6 GB | dead — PQ is wrong at 10-D |
> | IVFSQ8 | 68% | ~6.6 GB | 8-bit/dim too coarse for cosine near-ties |
> | **IVFSQfp16** | 89.6% | **9.17 GB** | **chosen** |
> | IVFFlat | 91% | ~14 GB | best recall, but won't fit 15 GB RAM comfortably |
>
> \*Extrapolated from measured 5M index files (incl. IndexIDMap2 id + IVF overhead, ~16 B/vec).
>
> Final full build: **IVFSQfp16, nlist=16384, 254,819,846 vectors, 9.17 GB**, cosine
> (`METRIC_INNER_PRODUCT` on L2-normalized vecs; DB stores raw). **recall@10 = 94.3%**
> (higher than the 5M test — the space is far denser at full scale) and **flat across
> nprobe∈{64..512}**, so serve with **nprobe=64** (cheapest, no recall cost).
>
> Hard lessons baked into `build_faiss.py`: (1) **write the index BEFORE validation** — the
> exact-search validation reads the whole 10 GB memmap and, with the ~9 GB index resident,
> drove the 15 GB box deep into swap (the build's `add` took 85 min, not 15). (2) On this box,
> **`--no-validate` during the build** + validate separately keeps peak memory survivable.
> (3) IVFPQ/IVFFlat/IVFSQ8 all remain available via flags.

**Install first:** `pip install faiss-cpu`. (faiss-gpu would need the whole index in VRAM;
9.17 GB ≫ 6 GB on the RTX 4050, and at 10-D CPU IVF search is already ~ms/query — so CPU it is.)

**Metric = cosine.** Vectors are **L2-normalized** before train/add (and queries normalized the
same way); with normalized vectors, inner product = cosine. The DB BLOBs stay **raw** — we
normalize a copy here. Use `METRIC_INNER_PRODUCT`.

**Index:** `IndexIDMap2(IVFPQ)` so we `add_with_ids(track_id)` and search returns track_ids directly.
```python
import faiss, numpy as np
d, nlist, m, nbits = 10, 32768, 5, 8      # m=5 → 2 dims/subquantizer (only sane split of 10)
quantizer = faiss.IndexFlatIP(d)          # inner product on normalized vecs = cosine
ivfpq = faiss.IndexIVFPQ(quantizer, d, nlist, m, nbits, faiss.METRIC_INNER_PRODUCT)
index = faiss.IndexIDMap2(ivfpq)

vecs = np.memmap("embeddings.f32", np.float32, "r").reshape(-1, d)
ids  = np.memmap("embed_ids.i64",  np.int64,   "r")

def normed(a):                            # L2-normalize a (copy), guard zero-norm
    a = np.ascontiguousarray(a, np.float32)
    n = np.linalg.norm(a, axis=1, keepdims=True); n[n == 0] = 1.0
    return a / n

sample = vecs[np.random.choice(len(vecs), 2_000_000, replace=False)]
ivfpq.train(normed(sample))
for s in range(0, len(vecs), 1_000_000):                 # add in 1M batches
    index.add_with_ids(normed(vecs[s:s+1_000_000]),
                       np.ascontiguousarray(ids[s:s+1_000_000]))
index.nprobe = 32                                        # search-time recall/speed knob
faiss.write_index(index, "embeddings.faiss")
```

**Params (knobs):** cosine (`METRIC_INNER_PRODUCT` on normalized vecs); `nlist≈32768 ≈ 2·√N`;
`m=5, nbits=8`; `nprobe≈32` default at search.
**Memory for build:** index ≈ 1.3 GB + 80 MB training sample, streamed adds → comfortably < 9 GB.
**Validation:** sample 1k query vectors, compare IVFPQ top-10 against a brute-force `IndexFlatL2`
on a 1M subset; report recall@10. If recall is poor at 10-D, fall back to **IVFFlat** (40 B/vec →
~10 GB index, higher recall, no PQ loss) — PRD specifies IVFPQ so that's the default, IVFFlat is
the documented escape hatch.

---

## 6. Cross-cutting concerns

**Load-bearing invariant (do not break):** the *same* `genre_scaler.joblib` and the *same*
`FEATURE_COLS` order flow through prep → train → embed. This is what guarantees the 10-D space
lives in the GBDT's feature space. Any refit/reorder silently corrupts the embeddings.

**BLOB endianness:** little-endian float32 ×10 (`'<f4'`), 40 bytes. Read back with
`np.frombuffer(blob, dtype='<f4')`. Recorded here and in `sae_config.json`.

**Disk preflight (Stage 3 & 4 should check):** new artifacts ≈ `sae_X.npy` 6.46 + `embeddings.f32`
10.2 + `embed_ids.i64` 2.0 + `embeddings.faiss` ~1.3 + DB growth ~16 ≈ **~36 GB** against 164 GB
free. Fine, but the box is 82% full — print free space and abort if < ~50 GB before the big writes.
`embeddings.f32`/`embed_ids.i64` are deletable after Stage 4.

**WAL growth (Stage 3):** controlled by per-batch commits (auto-checkpoint). No manual
`wal_checkpoint` needed; matches `predict_genres.py`.

**Deliverables summary:**

| Stage | Script | Key artifacts |
|---|---|---|
| 1 | `prep_sae_data.py` | `sae_X.npy`, `sae_y.npy`, `sae_prep_meta.json` |
| 2 | `train_sae.py` | `sae_encoder.pt`, `sae_config.json`, `sae_curves.png` |
| 3 | `embed_tracks.py` | `ml_10d_embeddings` (filled), `embeddings.f32`, `embed_ids.i64` |
| 4 | `build_faiss.py` | `embeddings.faiss` |

**Out of scope for v1:** semi-supervised reconstruction on unlabeled/predicted tracks (deferred —
124.25M clean labels is plenty); mood detection (separate feature).

---

## 7. Knobs — final values + rationale

These only affect Stages 2 and 4 — **Stage 1 prep is independent of all of them**.

| Knob | Value | Rationale |
|---|---|---|
| **α** (recon vs. genre) | **0.5** | Settled by the sweep in §3 (0.01/0.1/0.5). recon is near-lossless at every α (the objectives barely compete), so α just buys genre coherence → take the most that doesn't collapse. 0.5 wins, especially on hard genres; not 1.0 (diminishing returns + over-separation risk). |
| **Embedding metric** | **cosine** | Stateless (nothing to version/misapply at query time — avoids a repeat of the `genre_scaler_v2` mistake); robust to the linear bottleneck's arbitrary per-dim scale. DB stores **raw**, FAISS uses normalized. |
| **Bottleneck BN** | **on** (`BatchNorm1d(10)`, no activation) | Gives the 10 dims ≈unit variance for free, baked into the encoder → no separate emb-scaler artifact; conditions the space for cosine. |
| Class weights | sqrt-inv-freq, mean-norm, cap 10×, **on** | Given the 75× skew, keeps rare genres (african/country/christian/other) from collapsing into neighbors. Mean-norm so it doesn't secretly shift α. A/B via *per-class* val accuracy. |
| Encoder/decoder widths | `13→64→32→10` | Well-matched to a 13-D input; wider buys little (input carries only 13 dims), and at 124M : <15k params overfitting is impossible. |
| Classifier | `Linear(10→20)` (shallow) | Forces the *bottleneck* to be linearly genre-separable → FAISS neighbors come back genre-coherent. |
| Batch / epochs | 16384 / 5-max + early-stop | Page-cache-bound, so batch barely affects wall-time; expect early-stop ~epoch 2. |
| FAISS index | **IVFSQfp16**, `nlist=16384`, `nprobe=64` | Settled by sweep (§5): PQ dead (29%) & SQ8 too coarse (68%) at 10-D; fp16 = 94.3% recall@10 at 9.17 GB. IVFFlat (~14 GB, ~97%) is the escape hatch but won't fit 15 GB RAM comfortably. |

---

## 8. Suggested execution order

1. `prep_sae_data.py` → produce `sae_X.npy` / `sae_y.npy` (10–20 min). **Independent of all knobs.**
2. `train_sae.py` → encoder. Quick iteration; tune α on the val metrics here.
3. `embed_tracks.py` → the long run (hours). Resumable; can run unattended.
4. `pip install faiss-cpu` → `build_faiss.py` → `embeddings.faiss`. Validate recall@10.
