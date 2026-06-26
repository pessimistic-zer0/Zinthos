# F6 — Design Decisions & Engineering Retrospective

> **Companion to `F6_implementation_plan.md`.** That doc is the forward-looking spec; *this* one
> is the after-the-fact record: what we built, how, **every decision and its rationale**, the
> **alternatives we rejected and why**, the **surprises**, and the **lessons**. Written so a future
> engineer (or a future Claude session) can reconstruct not just *what* the code does but *why it
> is the way it is* — including the dead-ends, so nobody re-walks them.
>
> **Feature F6** = "perceptually similar tracks": compress the 13 audio features into a 10-D
> embedding that is both acoustically faithful and genre-coherent, write it for every track, and
> index it for fast similarity search.
>
> **Outcome (2026-06-21):** complete. Trained encoder, 254,819,846 embeddings written, FAISS
> index at **94.3% recall@10**, 9.17 GB. Total compute ≈ a few hours, mostly one swap-bound build.

---

## 0. Context & constraints that shaped everything

### The data (verified against `master.db`, 2026-06-20)
| Quantity | Count |
|---|---|
| Source B-labeled (ground truth) | 125,256,383 |
| Tracks with audio features (embedding universe) | 254,819,856 |
| Labeled **and** featured (training universe) | 124,702,033 |
| `labeled_tracks.csv` rows (actual training set) | 124,254,624 |
| Labeled but **no** features (un-embeddable) | 554,350 |

**The 20 macro genres** (alphabetical = label-encoder order): african, alternative, asian,
christian, classical, country, dance, electronic, folk, hip-hop, jazz, kids, latin, metal, other,
pop, r&b, reggae, rock, soundtrack.

**The 13 features** (load-bearing order): danceability, energy, key, loudness, mode, speechiness,
acousticness, instrumentalness, liveness, valence, tempo, duration_ms, time_signature.
`duration_ms` lives in `tracks`; the other 12 in `track_audio_features`.

### The hardware (this is not incidental — it drove the FAISS decision)
- **RTX 4050 Laptop, 6.05 GB VRAM** · **~15 GB RAM** (16 nominal) · **19 GB swap** · 12 cores · ~160 GB free NVMe.
- torch 2.12.0+cu130 (CUDA ✓), cuML 26.06, `faiss-cpu 1.14.3` (installed during this work).
- `master.db` = 129 GB, WAL mode.

> The single most important environmental fact: **anything resident above ~9 GB pushes this box
> into swap-thrashing.** This is why the index is fp16 (9 GB) and not Flat (~14 GB), and why one
> build took 85 minutes. See §5 and §7.

---

## 1. The model — Supervised Autoencoder (SAE)

```
                       ┌──► DECODER (10→32→64→13) ──► x̂   reconstruction loss  (MSE)
 x(13) ─► ENCODER ─► [10-D + BatchNorm]
                       └──► CLASSIFIER (Linear 10→20) ─► ŷ  classification loss (weighted CE)

 loss = MSE_meanperfeat(x̂, x) + α · CE_weighted(ŷ, y)        α = 0.5
```
After training we **keep only the encoder** (7,239 params total in the full model).

### Decisions inside the architecture
| Decision | What we chose | Why | Rejected alternative |
|---|---|---|---|
| Supervised vs. plain AE | **Supervised** (genre head) | A pure AE only preserves *sound*; the genre head forces the bottleneck to also separate genres, so neighbors are genre-coherent. | Plain autoencoder — neighborhoods would smear across genres. |
| Bottleneck activation | **BatchNorm1d(10), no nonlinearity** | BN gives the 10 dims ≈unit variance *for free* (deterministic via running stats at inference) → no dim dominates cosine distance, **and no separate embedding-scaler artifact to version/misapply**. | (a) plain linear bottleneck → arbitrary per-dim scale; (b) a separate post-hoc standardization scaler → another file to keep in sync (we'd been burned by `genre_scaler_v2` before). |
| Classifier depth | **Shallow `Linear(10→20)`** | Forces the *bottleneck itself* to be linearly genre-separable → that's exactly what makes nearest-neighbor lookups return same-genre results. | A deep classifier head would absorb the genre signal and let the bottleneck off the hook. |
| Encoder widths | `13→64→32→10` | Well-matched to a 13-D input; at 124M rows : <15k params, **overfitting is physically impossible** — the only risk is underfitting, which the val curves ruled out. | Wider nets — marginal at 13-D input, no measured benefit. |
| Training labels | **Ground-truth Source B only** | F5's *predicted* genres are a function of the same 13 features (circular — the SAE would just imitate the GBDT) and ~45% wrong (would distort the distances we optimize). | Training on F5 predictions — rejected on both circularity and noise grounds. |
| Semi-supervised | **Skipped for v1** | 124.25M clean labels is plenty; streaming unlabeled tracks through recon-only adds complexity for marginal gain. | Masked-classification semi-supervised training — deferred. |

---

## 2. Stage 1 — Data prep (`prep_sae_data.py`)

**What:** stream `labeled_tracks.csv` (9.4 GB) → scale with the existing `genre_scaler.joblib` →
write memory-mappable `sae_X.npy` (float32, 124,254,624 × 13, 6.46 GB) + `sae_y.npy` (int8) +
`sae_prep_meta.json`.

**How / why these choices:**
- **Memmap, not re-parse.** Each epoch mmaps a pre-scaled array instead of re-parsing 9.4 GB of
  CSV. We count rows once (byte-scan, 9 s), preallocate exact `.npy` memmaps, stream-fill. Peak
  RAM < 2 GB despite the 124M-row scale.
- **Reuse `genre_scaler.joblib` verbatim (never refit).** *The* load-bearing invariant of F6: the
  same 13-feature scaler flows prep → train → embed, so the 10-D space lives in the GBDT's feature
  space. (Explicitly **not** `genre_scaler_v2.joblib`, an abandoned 20-feature experiment.)
- **int8 labels** (20 classes < 128) → 124 MB instead of 248 MB.

**Results / diagnostics:**
- 124,254,624 rows, **0 NaN**, labels 0–19. Parsed in 1.7 min.
- Genre distribution is **heavily imbalanced (~75×)**: pop 19.1% → *other* 0.3%. (Fed Stage 2's
  class weighting.)
- **Genre-contiguity diagnostic:** max contiguous single-genre run = **4,605** « batch 16,384.
  This *proved* the CSV (in `track_id` order) is well-mixed, so Stage 2's simple per-epoch
  permutation is safe — **no block-shuffle prep needed** (a complication we'd have otherwise had
  to add).

**Rejected here:** full-RAM load (no headroom on 15 GB); re-parsing CSV per epoch (slow);
block-shuffled writes (unnecessary, per the diagnostic).

---

## 3. Stage 2 — Training (`train_sae.py`) — and the α saga

**What:** train the SAE on GPU; keep the encoder. 16384 batch, AdamW (1e-3, wd 1e-5), cosine
decay, ≤5 epochs + early stop, deterministic **1% modulo holdout** (`idx % 100 == 0` → 1,242,547
val rows — reproducible, no file shuffle).

**Class imbalance handling:** `CrossEntropyLoss(weight=w)`, `w` = sqrt-inverse-frequency,
**normalized so the frequency-weighted mean weight = 1** (preserves the CE scale so it doesn't
secretly change α), capped at 10×. Heaviest weights landed on other×5.1, christian×3.2, country×3.2.

### The α decision — where I was wrong, and the correction
α weights the genre loss against reconstruction. I made a prediction, it was wrong, and the
empirical correction *flipped the reasoning*. Recording this honestly because it's the most
instructive decision in F6:

1. **My a-priori guess (WRONG):** "recon MSE on standardized features ≈ 0.1–0.2, 20-class CE ≈
   1.3–1.8, so α≈0.1 puts them at parity with reconstruction primary." Defaulted α=0.1.
2. **Reality after training at α=0.1:** recon converged to **0.0046** (~30× lower than guessed —
   reconstructing 13 *correlated* features from 10 dims is easy), CE stuck at **~2.07** (higher —
   a *linear* head on 10-D has a lower ceiling than the full GBDT). So `α·CE = 0.207` vs
   `recon = 0.0046` → **genre dominated the gradient ~45×**, the opposite of "recon primary."
3. **The sweep {0.01, 0.1, 0.5}** (each ~6 min, tagged outputs so nothing clobbered):

   | α | val recon MSE | val genre-acc | note |
   |---|---|---|---|
   | 0.01 | 0.0016 | 40.9% | weakest genre structure |
   | 0.1 | 0.0046 | 43.2% | — |
   | **0.5** | **0.0136** | **45.0%** | **chosen** |

4. **The insight that settled it:** reconstruction stays **near-lossless at every α** (≤~12%
   per-feature error even at 0.5) — the two objectives *barely compete*. So acoustic fidelity is
   essentially free, and α just controls *how much genre structure to add on top*. More helps,
   and it isn't collapsing (45% linear-probe acc is far from the ~100% of a degenerate 20-blob
   space; recon proves per-track detail is preserved). **Decision: α=0.5.**

**Final model (α=0.5):** val recon 0.0136, val genre-acc 45.0%, best epoch 4, 6.2 min, ~595k
samples/s. Per-class accuracy tells the real story and *confirms it behaves like a proper acoustic
embedding*:
- Acoustically distinct genres separate cleanly: hip-hop 76%, classical 73%, jazz 59%,
  electronic 57%, metal 53%.
- Acoustically *overlapping* genres blend: r&b 0.7%, country 0.5%, dance 8–14%, alternative ~10%.
  **This is correct, not a bug** — r&b genuinely sits between pop and hip-hop acoustically, so a
  "sounds-like" search *should* place them near each other.

**Rejected here:** α=0.1 / α=0.01 (less genre coherence, no recon benefit); α=1.0 (diminishing
genre gain, rising recon error, over-separation risk); unweighted CE (rare genres would collapse
into neighbors).

---

## 4. Stage 3 — Embedding all tracks (`embed_tracks.py`)

**What:** run the encoder over **every** featured track (labeled, F5-predicted, NULL alike — the
label is irrelevant at inference) and write a 10-D vector each to `ml_10d_embeddings`.

**How / why:**
- **Reused `predict_genres.py`'s proven scale pattern:** keyset (cursor) pagination on `track_id`
  (not LIMIT/OFFSET), WAL with two connections (read / write+checkpoint), one commit per batch
  (WAL auto-checkpoints — no unbounded growth), and a **resumable** `ml_embedding_checkpoint` row.
- **Dropped the source_b filter** that `predict_genres.py` has — we want *all* featured tracks.
- **BLOB = little-endian float32 × 10 = 40 bytes**, storing the **raw** encoder output (FAISS
  normalizes for cosine downstream; keeping raw in the DB is lossless and future-proof).
- **Aligned flat files** `embeddings.f32` (10.2 GB) + `embed_ids.i64` (2 GB) streamed alongside,
  so Stage 4 never re-scans the 129 GB DB. **Flat write happens *before* the DB commit**, so a
  crash can only leave the flats *longer* than the checkpoint, which resume truncates back —
  alignment is always recoverable.

**Results:** **254,819,846** embedded; exactly **10** tracks skipped for a NULL feature
(254,819,856 − 10); **21.7 min**; ~220k rows/s. Byte-verified: a DB BLOB decodes to 10 float32,
flat-file vector == DB vector, ids strictly sorted & unique, flat row count == DB row count.

**Rejected here:** normalizing before storage (lose raw, can't change metric later); re-querying
the DB for FAISS instead of flat files (slow, touches the 129 GB DB again); LIMIT/OFFSET pagination
(O(n²)).

---

## 5. Stage 4 — FAISS index (`build_faiss.py`) — the quantizer hunt + the swap incident

This stage had the most surprises and the most rejected alternatives.

### Metric
**Cosine** — L2-normalize vectors at index *and* query time (inner product on normalized vectors
= cosine). Chosen over raw L2 because (a) the linear bottleneck's per-dim scale is arbitrary so raw
L2 would let one dim dominate, and (b) normalization is **stateless** — no stored stats to version
or misapply (avoiding a repeat of the `genre_scaler_v2` class of bug). The DB keeps **raw**
vectors; we normalize a copy only at build/query.

### The quantizer sweep (the central decision)
The PRD specified **IVFPQ**. We tested it and it failed, then swept the field on a 5M subset:

| Index | recall@10 (5M) | full-scale size | verdict |
|---|---|---|---|
| IVFPQ (m=5, nbits=8) | **29.5%** (even at nprobe 256) | ~5.6 GB | **dead** — PQ is structurally wrong at 10-D |
| IVFSQ8 (8-bit/dim) | 68.6% | ~6.6 GB | too coarse — cosine near-ties get reordered by int8 rounding |
| **IVFSQfp16 (half-precision)** | **89.6%** | **9.17 GB** | **chosen** |
| IVFFlat (no quantization) | 91.0% | ~14 GB | best recall, but won't fit 15 GB RAM comfortably |

> Sizes are extrapolated from the **measured** 5M-subset index files (fp16's extrapolation hit
> 9.17 GB exactly, validating the method). They **include the `IndexIDMap2` id map + IVF list
> overhead (~16 B/vec on top of the quantized code)** — so they're well above the raw code size
> (PQ 5 B, SQ8 10 B, fp16 20 B, Flat 40 B per vector). A future optimization: drop `IndexIDMap2`
> and use the IVF index's native `add_with_ids` to save ~8 B/vec (~2 GB at full scale).

**Why PQ dies at 10-D:** Product Quantization's whole purpose is memory savings by compressing
*high*-dimensional vectors. At 10-D the vectors are already tiny (40 bytes), so PQ buys almost
nothing while its distance *estimates* become badly distorted — and cosine neighbors here are so
close that any distortion reorders them. SQ8's per-dimension 8-bit rounding has the same fatal
problem, just milder (68% vs 29%). **fp16 has enough precision to preserve the rankings.**

**Why fp16 over Flat:** Flat's 91% vs fp16's 89.6% is within the 200-query noise band — but Flat
is ~14 GB, which on a 15 GB box means no headroom for the OS/SQLite/API. fp16 at 9 GB leaves ~5 GB.
Not worth ~4 GB of RAM for an imperceptible recall change. (We left the Flat command staged; the
user reviewed the final numbers and chose to skip it.)

### The swap-thrash incident (and the fix it forced)
The full IVFSQfp16 build's `add` phase took **85 minutes** instead of the ~15 I estimated. Cause:
the ~9 GB index + the 10 GB `embeddings.f32` memmap being read simultaneously **overflowed 15 GB
RAM and maxed all 19 GB of swap** → the build crawled. Then the in-build validation (an exact
brute-force pass that reads the *entire* 10 GB memmap *again*, with the 9 GB index still resident)
piled on more pressure.

**Two fixes baked into `build_faiss.py` from this:**
1. **Write the index to disk BEFORE validation.** Originally we validated then wrote — meaning a
   validation-phase OOM would have discarded the entire expensive build. Now the build is persisted
   the instant `add` finishes.
2. **`--no-validate` is the recommended mode on this box** for big builds; validate separately
   afterward against the finished index (lower peak memory).

### Final index — and two pleasant surprises
**IVFSQfp16, nlist=16384, 254,819,846 vectors, 9.17 GB, recall@10 = 94.3%.**

1. **94.3% > the 89.6% the 5M test predicted.** Why: **density.** At 254M vectors in 10-D the space
   is extremely dense — a query and its true neighbors land in the same coarse cell reliably, so
   the IVF finds them. The 5M subset was far sparser and lost more to cell boundaries.
2. **Recall is *flat* across nprobe ∈ {64, 128, 256, 512}** (all 94.3%). The true top-10 are
   essentially all in the *nearest* cell, so extra probing finds nothing new; the residual 5.7%
   miss is fp16 rounding on near-ties, not a coverage gap. **Practical win: serve with `nprobe=64`**
   — cheapest, zero recall cost.

**Rejected here:** IVFPQ (PRD default, 29%); IVFSQ8 (68%); IVFFlat (size vs RAM); GPU FAISS
(9 GB ≫ 6 GB VRAM — impossible; and at 10-D CPU IVF search is already ~ms/query, so the GPU buys
nothing for interactive serving); high nprobe (no benefit — recall is flat).

---

## 6. Why FAISS serving is CPU-only (asked explicitly)

The full index (9 GB) **cannot fit in the 6 GB VRAM**, and faiss-gpu requires the *entire* index
resident in VRAM (no mmap/spill). Of all the candidates, only IVFPQ (~5.6 GB) would even
*marginally* fit in 6 GB VRAM — and that's the 29%-recall one we rejected; everything with usable
recall (SQ8 ~6.6 GB, fp16 9.2 GB, Flat ~14 GB) exceeds it. And it doesn't matter: at 10-D, CPU IVF
search scans ~1–4M tiny vectors per query → single-digit milliseconds, which is plenty for a
user-triggered "similar tracks" feature. The GPU did its job in training + the 254M-track encode;
serving is legitimately CPU-shaped. (GPU would only help for an offline *all-pairs* precompute, or
on a ≥16 GB-VRAM card.)

---

## 7. Roads not taken — consolidated

| Alternative | Why rejected |
|---|---|
| Neural net for genre classification (pre-F6) | Plateaued ~41% < RF 45.9%; GBDT won. (F5 history, context for F6.) |
| Train SAE on F5 *predicted* labels | Circular (same 13 features) + ~45% wrong → distorts distances. |
| `genre_scaler_v2` (20-feature) | Abandoned experiment; would break the prep→train→embed feature-space invariant. |
| Plain (unsupervised) autoencoder | No genre coherence — neighborhoods smear across genres. |
| Deep classifier head | Absorbs genre signal; bottleneck no longer forced to be separable. |
| Separate post-hoc embedding standardizer | Replaced by BatchNorm at the bottleneck — same effect, no extra artifact to sync. |
| Full-RAM training data load | No headroom on 15 GB; mmap + page cache is safe. |
| Block-shuffled prep | Unnecessary — data is well-mixed (max run 4,605 « batch). |
| α = 0.01 / 0.1 | Less genre coherence; reconstruction was already near-lossless so nothing gained by lowering α. |
| α = 1.0 | Diminishing genre gain, rising recon error, over-separation risk. |
| Raw-L2 / Euclidean metric | Arbitrary per-dim scale would dominate distance; cosine is stateless & robust. |
| Semi-supervised reconstruction | Deferred — 124M labels suffice for v1. |
| IVFPQ (PRD default) | 29% recall@10 — PQ is wrong at 10-D. |
| IVFSQ8 | 68% — 8-bit/dim too coarse for cosine near-ties. |
| IVFFlat | ~14 GB won't fit 15 GB RAM comfortably; ~2–3% recall edge is imperceptible. |
| GPU-resident FAISS | 9 GB ≫ 6 GB VRAM; CPU search is already ms-scale. |
| nprobe > 64 | Recall is flat past 64 — extra probing is wasted cost. |

---

## 8. Lessons learned (the transferable bits)

1. **Don't trust a-priori loss-scale math for a novel architecture — measure, then decide.** My
   α=0.1 rationale was backwards; a 20-minute sweep replaced a confident wrong guess with α=0.5.
2. **PQ is the wrong tool in low dimensions.** Below ~32-D, prefer fp16/SQ/Flat; PQ's compression
   is pointless and its distortion is fatal to close-neighbor ranking.
3. **ANN recall *improves* with scale when the space is dense** — small-subset benchmarks can
   *under*-predict full-scale recall. (Ours went 89.6% → 94.3%.)
4. **On a memory-tight box, in-RAM artifact size has a hard ceiling** (~9 GB here before swap
   thrash), and you must **persist expensive build results to disk *before* any memory-heavy
   validation.** A 6-line reorder saved an 85-minute build from being throwaway.
5. **One scaler, one feature order, end to end.** The prep→train→embed invariant is what makes the
   embedding space meaningful; everything reuses `genre_scaler.joblib`.

---

## 9. Final artifacts & how to use them

| Artifact | What it is |
|---|---|
| `prep_sae_data.py`, `train_sae.py`, `embed_tracks.py`, `build_faiss.py` | The four-stage pipeline (each resumable/idempotent where it matters). |
| `sae_encoder.pt` + `sae_config.json` | The trained encoder (state_dict) + reproducible config (α=0.5, widths, metric, scaler ref). |
| `ml_10d_embeddings` (table) | 254,819,846 rows: `track_id`, raw 40-byte fp32 BLOB, `model_version='autoencoder_v1'`. Source of truth. |
| `embeddings.faiss` | IVFSQfp16, 9.17 GB, 94.3% recall@10, serve at `nprobe=64`. |
| `embeddings.f32` / `embed_ids.i64` | Build inputs (12.2 GB total) — **deletable** now that the index is built and Flat is ruled out. |
| `sae_curves*.png`, `sae_config_a*.json` | Training curves + the α-sweep configs (kept for the record). |

**Inference contract (for the encoder / serving):**
1. Gather the 13 features in the exact `FEATURE_COLS` order (`duration_ms` from `tracks`).
2. `genre_scaler.joblib.transform(x)` → the encoder (eval mode, BN running stats) → 10-D vector.
3. To query FAISS: **L2-normalize** the vector, `index.nprobe = 64`, `index.search(q, k)` →
   returns `track_id`s directly (IndexIDMap2).

---

## 10. Deferred / next (not part of F6)
- Wire `embeddings.faiss` into the FastAPI `/similar` endpoint (normalize query, nprobe=64).
- Reclaim ~12 GB by deleting the flat files (Flat is ruled out, so they're no longer needed).
- Mood detection (separate multi-label classifier) — future.
- Possible v2: semi-supervised reconstruction on the unlabeled tracks; revisit α with a
  product-quality (not proxy) metric once the search feature is live and measurable.
