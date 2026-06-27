# Zinthos — Complete Project Context

> **What this document is and how to use it.**
> This is a **self-contained context dump** of an entire personal project, written so it can be
> pasted into a fresh chat (e.g. claude.ai) that has **no access to the code** and still give that
> assistant everything it needs to help write résumé bullets, prep interview talking points, or
> describe the work. It is deliberately exhaustive: it covers *what* was built, *why* each decision
> was made, *what we started with vs. ended up with*, the hard problems, the dead-ends, and the
> measurable outcomes. There is a dedicated **§14 Résumé & interview material** at the end with
> quantified achievements, a skills inventory, and candidate bullet points.
>
> **Scope & authorship context:** built as a solo project over roughly **May–June 2026** by one
> developer (Prince), on a single consumer laptop. Codenamed **"Zinthos."** The assistant
> reading this can assume the author did all of the architecture, ETL, ML, backend, and client
> work themselves.

---

## 0. TL;DR — the one-paragraph version

Zinthos is a music search/discovery engine over **~255 million Source A tracks** that lets
you find music by *how it sounds and feels* rather than just by name. The author took **~266 GB**
of raw third-party SQLite/CSV dumps, built a single normalized **145 GB SQLite `master.db`** with a
high-performance **C++** ETL pipeline, trained a **LightGBM** genre classifier and a **PyTorch
supervised autoencoder** that compresses 13 audio features into a 10-dimensional, genre-coherent
embedding, indexed all **254.8 million** of those embeddings in a **FAISS** vector index (9.2 GB,
**94.3% recall@10**), and wrapped the whole thing in a **Python/FastAPI** "engine" serving semantic
search, perceptual similar-track lookup, playlist generation, artist pages, and local-library taste
analysis — consumed by a thin **Rust/ratatui** terminal client. The defining constraint throughout
was a **15 GB RAM / 6 GB VRAM laptop**: almost every architectural choice exists to stay under a
~9 GB resident-memory swap-thrash ceiling while still operating on a quarter-billion rows.

---

## 1. What the project is (and is not)

**Is:** a local-first "search music by sound and feel" system. You can ask for *"sad rock songs
for studying"* or *"rainy 3am drive"* and get a real pool of tracks; pick a track and get
perceptually similar ones; generate a smoothly-transitioning playlist; or point it at your own
music folder and have it identify your tracks, profile your taste, and recommend more.

**Is not (yet):** a hosted/public product, an audio-analysis engine that "listens" to raw files,
or a mood-vector search. Those are deliberately deferred (see §13). It runs on one machine today,
but every interface was built **host-agnostic**, so going public is a config change + a milestone,
not a rewrite.

### The three pillars
1. **Query understanding** — rule-based word→audio-feature mapping, with an **LLM fallback** for
   abstract phrasing.
2. **Machine learning** — genre prediction (fills the unlabeled half of the catalog) and learned
   10-D embeddings (powers "sounds-like" search).
3. **A massive, carefully-engineered data substrate** — 255M tracks / ~348M artist associations in
   one queryable SQLite file plus a FAISS vector index.

### Feature catalog (the "F-numbers" used throughout the codebase)
- **F1 — Semantic search:** natural-language query → audio-feature filters → ranked tracks.
- **F2 — Playlist generator:** a mood pool, ordered for smooth track-to-track transitions.
- **F3 — Artist page:** aggregate audio profile, dominant genre, top tracks.
- **F4 — Track detail:** full metadata + an embedded "similar tracks" block.
- **F5 — Genre prediction:** ML genre for every track that lacks a ground-truth label.
- **F6 — Similar tracks:** vector nearest-neighbor "sounds-like" search.
- **F7 — Local library scan:** identify a user's local files and analyze/recommend from them.

---

## 2. Where we started

### The raw material (~266 GB of third-party data)
| Source | Size | What it provided |
|---|---|---|
| `source_a.sqlite3` | 117 GB | tracks, artists, albums, track↔artist junctions |
| `source_a_audio_features.sqlite3` | 39 GB | the **13 audio features** per track (danceability, energy, valence, tempo, …) |
| `source_b.csv` | 110 GB | Source B ground-truth **genre** labels keyed by ISRC |

These were heterogeneous (two SQLite DBs + a giant CSV), keyed differently (Source A base-62 text
IDs vs. ISRC barcodes), and lived on a **slow exfat/FUSE mount** where random reads are ~35× slower
than ext4. None of it was directly queryable as a unit.

### The hardware envelope (not incidental — it shaped *everything*)
- **RTX 4050 Laptop GPU — 6.05 GB VRAM**
- **~15 GB RAM** (16 nominal), **19 GB swap**, 12 CPU cores, ~160 GB free NVMe.

> **The single most important fact about this project:** anything resident above **~9 GB** pushes
> the box into swap-thrashing. The fp16 FAISS index (9 GB, not Flat's 14 GB), the mmap-don't-load
> serving model, the out-of-core ETL, the streamed training prep, and one painful 85-minute build
> all trace directly back to this ceiling. Whenever a design choice looks conservative, the answer
> is almost always *9 GB*. The headline engineering story of the project is **operating on a
> quarter-billion rows inside a 15 GB / 6 GB consumer laptop.**

### What existed before the "hard reset"
Early work produced a **Random Forest** genre baseline (**45.9%** accuracy) on an *old, flat*
database, plus the product spec (PRD) and a hierarchical `CLAUDE.md` context system. The data
pipeline was then deliberately **hard-reset** to rebuild `master.db` from scratch on a clean,
normalized schema. The git history ("tried to combine two very big databases but got OOM", "made
the merger program", "final version for the scanner and db builder") is the archaeology of that
rebuild.

---

## 3. Architecture at a glance

```
 RAW (266 GB, exfat)                  ETL (C++)                 MASTER SUBSTRATE
 ┌───────────────────┐          ┌──────────────────┐      ┌──────────────────────────┐
 │ source_a           │          │ master_db_builder │      │ master.db (145 GB, ro/WAL)│
 │ audio_features     │ ───────▶ │ ATTACH + INSERT…  │ ───▶ │ 10 normalized tables      │
 │ source_b.csv       │          │ SELECT, streamed  │      │ + track_search (hot)      │
 └───────────────────┘          └──────────────────┘      │ + ml_10d_embeddings        │
                                                            └────────────┬─────────────┘
                                          ML (Python)                    │
       ┌────────────────────────────────────────────────────────────────┤
       ▼                                    ▼                             ▼
 LightGBM genre              Supervised Autoencoder (PyTorch)    FAISS index build
 (train → predict            (13→10-D, genre-coherent;           (IVFSQfp16, 9.2 GB,
  130M unlabeled)             embed all 254.8M tracks)            94.3% recall@10)
       │                                    │                             │
       └────────────────────────────┬───────┴─────────────────────────────┘
                                     ▼
                       ENGINE (Python · FastAPI · one uvicorn worker · on-demand)
                       mmap FAISS + read-only master.db + track_search + LRU caches
                       route → retrieve → score/order → hydrate → JSON
                                     │  HTTP/JSON (config-driven URL)
                       ┌─────────────┴──────────────┐
                       ▼                              ▼
                 Rust ratatui TUI            Web client (deferred)
```

Two **deliberately distinct** retrieval modes:
- **F6 Similar (vector):** seed embedding → FAISS top-100 → re-rank → hydrate top-K.
- **F1 Semantic (attribute):** rules/LLM → feature filters → `track_search` scan → hydrate.
  F1 is attribute-based, **not** vector-based, because the 10-D space is *genre*-supervised, not
  *mood*-supervised — "sad" is not a clean point in it. (This distinction is itself a design
  insight worth being able to articulate.)

---

## 4. Phase 1 — The data pipeline (C++ ETL)

**Goal:** turn 266 GB of mismatched sources into one normalized, integer-keyed, queryable
`master.db` — without ever OOM-ing on a 15 GB box.

### Key decision: integer surrogate keys (reuse source `rowid`s)
A 22-character Source A text ID repeated across 5 tables at 256M tracks / 348M associations is
enormous. Instead, each source table's `rowid` is reused as the integer primary key
(`tracks.track_id`, `artists.artist_id`, `albums.album_id`); the original text IDs are kept only
where needed for external linking (`track_mappings` — the "Rosetta Stone" — and `source_a_id`
columns). This is **the single biggest space/perf win** in the substrate: an 8-byte (often
varint-packed) int instead of a 22-char string, five times over.

### The 10-table normalized schema
`albums`, `tracks`, `track_mappings`, `track_audio_features`, `artists`, `track_artists`,
`artist_genres`, `source_b_genres`, `ml_genre_predictions`, `ml_10d_embeddings`. Moved off the
legacy flat schema specifically for data integrity (real foreign keys) and query performance.
Notable schema choices, several learned the hard way:

- **`WITHOUT ROWID` was *removed*** from `track_mappings` and `track_artists` after it made the
  256M-row bulk load **38× slower** (8,812 s vs. 230 s). The space saving was not worth it. (Great
  interview anecdote: a textbook "optimization" that was catastrophic at scale.)
- **`track_audio_features.track_id` is a plain column, not the rowid** — the loader streams audio
  features in the *source's* order and can't sort by `track_id` at insert time; a unique index is
  built *after* the bulk load instead.
- **Denormalized `release_date` onto `tracks`** (from albums) so F6's era-proximity term and other
  reads avoid a join.
- **Pre-computed `camelot_code`** on audio features for harmonic mixing (the playlist key penalty).
- **Indexes built AFTER the bulk load, one at a time** — except `idx_tracks_isrc`, which must exist
  *before* the Source B phase because it drives the ISRC → track-id join.

### The ETL strategy (`master_db_builder.cpp`, C++17, compiled `-O3`)
- **Governing performance rule:** the source filesystem is exfat/FUSE (~35× slower random reads),
  so source tables are read **sequentially only**; every random-access join targets a `main.*`
  table on ext4. Source DBs are never random-probed.
- **Move bytes with SQL, not heap:** `ATTACH` both source SQLite DBs to the master connection and
  do `INSERT…SELECT` — IO-speed, near-zero heap. Only the artist-position counter and the CSV parse
  run as C++ prepared-statement loops.
- **Out-of-core throughout:** streamed reads, batched inserts (`COMMIT_EVERY = 2M` rows), resumable
  via a `_build_progress` table, with `--dry-run N`, `--reset`, and `--validate` modes.
- **Multiple-artists-per-track** support was an explicit fix (early versions supported only one).

### Verified dataset counts (straight from `master.db`)
Earlier project docs cited 141.67M / 113.33M — **those were wrong.** The real numbers (verified
2026-06-20), which is itself a useful lesson in validating your own data:

| Quantity | Count |
|---|---|
| Tracks with audio features (the working universe) | **254,819,856** |
| Source B-labeled (ground truth) | 125,256,383 |
| Labeled **and** featured (trainable) | 124,702,033 |
| Labeled but **no** features (un-embeddable) | 554,350 |
| Unlabeled but featured → **needs genre prediction** | **130,117,823** |
| `labeled_tracks.csv` (actual training set after drops) | 124,254,624 |

---

## 5. Phase 2 — Genre classification (ML, "F5")

**Goal:** every track needs a genre. Source B provided ground truth for ~125M; the other ~130M
featured tracks need one predicted from their 13 audio features.

### The model journey: RF → NN (failed) → LightGBM
1. **Random Forest baseline:** 45.9% accuracy. Useful as a floor; overfit badly (max_depth=20).
2. **Neural networks:** tried across **3 architecture iterations**, **plateaued at ~41.3%** — never
   even crossed the RF baseline. The lesson: **13 audio features is low-dimensional tabular data**,
   and on tabular data **gradient-boosted decision trees are state of the art**, not deep nets.
   (This became a hard project constraint: do *not* reach for a NN for genre.)
3. **LightGBM (chosen):** handles feature interactions natively, trains in ~2 min vs. ~30+ for a
   NN, and beat the baseline. Config: 20-class multiclass, `num_leaves=127`, `max_depth=12` (the RF
   overfit lesson — depth control is critical), `learning_rate=0.05` with **2000 boosting rounds**
   + early stopping, feature/bagging fractions 0.8 (RF-style variance reduction), light L1/L2.
   Reuses the RF's `genre_scaler.joblib` and `genre_label_encoder.joblib`. Target was ≥50%.

The **20 macro genres** (alphabetical / label-encoder order): african, alternative, asian,
christian, classical, country, dance, electronic, folk, hip-hop, jazz, kids, latin, metal, other,
pop, r&b, reggae, rock, soundtrack. Source B's ~30–77 raw genre strings are consolidated into these
20 in the **Python layer** (the raw string stays in `source_b_genres`, so the mapping is
auditable).

### Inference: the "Two-Brain" cascade (`predict_genres.py`)
A precision-oriented cascade per unlabeled track:
- **Gate 1 — acoustic:** GBDT top probability ≥ threshold → accept (`tiebreaker_applied=0`).
- **Gate 2 — cultural:** below threshold, but an artist micro-genre in `artist_genres` keyword-
  matches the predicted macro → accept (`tiebreaker_applied=1`). The micro→macro map is
  intentionally **hardcoded keyword rules** used as a *validation gate*, not a second classifier.
- **Gate 3 — fall-through:** low confidence + no cultural support → **skip** (left NULL).
  *Better a missing genre than a wrong one* — a deliberate precision-over-coverage choice.

**Outcome of the full run** (130,117,823 rows scanned, **54.7 min on GPU**):
- Gate 1 acoustic: 43,262,352
- Gate 2 cultural: 5,978,203
- Gate 3 skipped: 80,877,261
- **Written: 49,240,555** (≈38% of the unlabeled set accepted; the rest deliberately left NULL).

### Engineering details that matter
- The model is a **native `lightgbm.Booster`** → call `.predict(X)` (returns the `(n,20)` proba
  matrix); there is no `predict_proba`.
- Features **must** be scaled with `genre_scaler.joblib` (13-feature) — not `genre_scaler_v2.joblib`
  (a 20-feature abandoned experiment that burned the project twice).
- Resumable via a checkpoint row; **keyset pagination** on `track_id` (never `LIMIT/OFFSET`, which
  is O(n²) at this scale); `master.db` in WAL mode.
- **GPU inference via RAPIDS cuML FIL** (`--gpu`) loads the *same* model file and is **~145× faster**
  than CPU predict (~2.3 s vs. ~330 s per 250k rows), turning a **3–4 day** CPU job into hours.
  Verified **99.94%** argmax agreement with the CPU model. (CPU is slow because the full
  2000-round / ~40k-tree model is genuinely large; trimming rounds hurts accuracy, so it wasn't.)

---

## 6. Phase 3 — Embeddings + vector search (ML, "F6")

**Goal:** "perceptually similar tracks." Compress the 13 features into a **10-D** vector that is
both acoustically faithful *and* genre-coherent, write it for all 254.8M tracks, and index it for
fast nearest-neighbor search.

### The model: a Supervised Autoencoder (SAE), in PyTorch
```
                  ┌─► DECODER (10→32→64→13) ─► x̂   reconstruction loss (MSE)
 x(13) ─► ENCODER ─► [10-D + BatchNorm]
                  └─► CLASSIFIER (Linear 10→20) ─► ŷ  classification loss (weighted CE)

 loss = MSE(x̂, x) + α·CE_weighted(ŷ, y)        α = 0.5     (keep only the encoder afterward)
```
- **Why supervised?** A plain autoencoder only preserves *sound*; the genre head forces the
  bottleneck to also *separate genres*, so neighbors are genre-coherent ("sounds-like" returns
  same-genre results).
- **BatchNorm at the bottleneck, no nonlinearity** — gives the 10 dims ≈unit variance for free, so
  no single dim dominates cosine distance, **and no separate embedding-scaler artifact to version or
  misapply** (the project had been burned by `genre_scaler_v2`).
- **Shallow `Linear(10→20)` classifier** — forces the *bottleneck itself* to be linearly
  genre-separable (a deep head would absorb the signal and let the bottleneck off the hook).
- **Tiny net (~7.2k params)** at 124M rows → overfitting is physically impossible; the only risk is
  underfitting, which the validation curves ruled out.
- **Trained on ground-truth Source B labels only** — training on F5 *predicted* labels would be
  circular (same 13 features) and ~45% wrong, distorting the very distances being optimized.

### The α saga (the most instructive single decision in the project)
α weights genre loss vs. reconstruction. The *a-priori* guess (α≈0.1, "reconstruction primary")
was **wrong**: after training, recon MSE converged to ~0.0046 (≈30× lower than guessed —
reconstructing 13 *correlated* features from 10 dims is easy), while CE stuck at ~2.07 (a *linear*
head has a lower ceiling than the full GBDT). So genre actually dominated the gradient ~45×. A
20-minute empirical sweep settled it:

| α | val recon MSE | val genre-acc | verdict |
|---|---|---|---|
| 0.01 | 0.0016 | 40.9% | weakest genre structure |
| 0.1 | 0.0046 | 43.2% | — |
| **0.5** | **0.0136** | **45.0%** | **chosen** |

The insight: reconstruction stays near-lossless at *every* α (the two objectives barely compete),
so acoustic fidelity is essentially free and α just controls *how much genre structure to add on
top*. Per-class accuracy confirmed it behaves like a real acoustic embedding: acoustically distinct
genres separate cleanly (hip-hop 76%, classical 73%, jazz 59%), while overlapping ones blend (r&b,
country) — **which is correct**, because r&b genuinely sits between pop and hip-hop acoustically, so
a sounds-like search *should* place them near each other. (Lesson: *measure, don't theorize, for a
novel architecture* — a confident wrong guess was replaced by a cheap sweep.)

### The four-stage embedding pipeline
1. **`prep_sae_data.py`** — stream the 9.4 GB `labeled_tracks.csv`, scale with the *same*
   `genre_scaler.joblib`, write memory-mappable `sae_X.npy` (124.25M × 13, 6.46 GB) + `sae_y.npy`
   (int8). Peak RAM < 2 GB. A genre-contiguity diagnostic (max single-genre run = 4,605 « batch
   16,384) *proved* the CSV is well-mixed → no expensive block-shuffle needed.
2. **`train_sae.py`** — GPU, batch 16384, AdamW, cosine LR decay, ≤5 epochs + early stop,
   sqrt-inverse-frequency class weights (the catalog is ~75× imbalanced: pop 19.1% → *other* 0.3%).
   Best model: ~6 min, ~595k samples/s.
3. **`embed_tracks.py`** — run the encoder over **every** featured track (the label is irrelevant
   at inference). Wrote **254,819,846** vectors (exactly **10** skipped for a NULL feature) in
   **21.7 min** (~220k rows/s). Stored as raw little-endian fp32×10 (40-byte BLOB) in
   `ml_10d_embeddings`, and streamed aligned flat files (`embeddings.f32`, `embed_ids.i64`) so the
   index build never re-scans the 145 GB DB. Resumable; flats are written before the DB commit so a
   crash is always recoverable by truncation.
4. **`build_faiss.py`** — see below.

### The FAISS quantizer hunt (the spec's choice was wrong)
The PRD specified **IVFPQ**. It was tested and *failed*, then the field was swept on a 5M subset:

| Index | recall@10 (5M) | full-scale size | verdict |
|---|---|---|---|
| IVFPQ (m=5, nbits=8) | **29.5%** | ~5.6 GB | **dead** — PQ is structurally wrong at 10-D |
| IVFSQ8 (8-bit/dim) | 68.6% | ~6.6 GB | too coarse — int8 rounding reorders cosine near-ties |
| **IVFSQfp16 (half-precision)** | **89.6%** | **9.17 GB** | **chosen** |
| IVFFlat (no quantization) | 91.0% | ~14 GB | best recall, but won't fit 15 GB RAM comfortably |

**Why PQ dies at 10-D:** Product Quantization exists to compress *high*-dimensional vectors. At
10-D the vectors are already tiny (40 bytes), so PQ saves almost nothing while its distance
*estimates* get distorted enough to reorder the very-close cosine neighbors. fp16 keeps enough
precision to preserve rankings; Flat's extra ~5 GB of RAM buys an imperceptible recall gain on the
15 GB box, so it was staged but skipped. (Lesson: *PQ is the wrong tool below ~32-D.*)

### Two pleasant surprises at full scale
- **Recall *rose* from 89.6% (5M) to 94.3% (254M).** Reason: **density** — at 254M vectors in 10-D
  the space is so dense that a query and its true neighbors reliably land in the same coarse cell.
  (Lesson: small-subset ANN benchmarks can *under*-predict full-scale recall.)
- **Recall is flat across nprobe ∈ {64,128,256,512}** (all 94.3%) — the true top-10 are essentially
  all in the nearest cell. **So the engine serves at `nprobe=64`** — cheapest, zero recall cost.

### The swap-thrash incident (and the fix it forced)
The full build's `add` phase took **85 minutes** instead of the estimated ~15: the ~9 GB index +
the 10 GB `embeddings.f32` memmap, read simultaneously, overflowed 15 GB RAM and maxed all 19 GB of
swap. Two permanent fixes baked in: **(1)** write the index to disk *before* validation (so a
validation-phase OOM can't discard the whole expensive build), and **(2)** `--no-validate` is the
recommended mode on this box, validating separately afterward. A six-line reorder saved an
85-minute build from being throwaway.

### Final F6 artifacts & serving facts
- **`ml_10d_embeddings`** — 254,819,846 rows, raw 40-byte fp32 BLOB, `model_version='autoencoder_v1'`.
- **`embeddings.faiss`** — IVFSQfp16, nlist=16384, **9.17 GB, 94.3% recall@10**, served at nprobe=64.
- **`sae_encoder.pt` + `sae_config.json`** — encoder + reproducible config (α=0.5, widths, scaler).
- **Serving is CPU-only by design** — the 9 GB index can't fit 6 GB VRAM (faiss-gpu needs the whole
  index resident), and at 10-D a CPU IVF scan is already single-digit ms/query — plenty for an
  interactive feature. The GPU did its real job in *training* and the 254M-track *encode*.

> **The end-to-end invariant that makes the whole ML stack coherent:** one scaler, one feature
> order, prep → train → embed → predict. Everything reuses `genre_scaler.joblib` with the 13
> features in the exact same order (`duration_ms` comes from `tracks`, the other 12 from
> `track_audio_features`). Break this and the embedding space stops meaning anything.

---

## 7. Phase 4 — The backend engine (Python / FastAPI)

**Goal:** one headless "Global Brain" that holds all the data and ML and exposes a local JSON API;
thin clients that hold *nothing*. Built **engine-first, in vertical slices** (milestones M0–M7),
each shippable and testable on its own.

### Non-negotiable design rules (and why they're good to articulate)
1. **The data never leaves the server.** `master.db` (145 GB) and `embeddings.faiss` (9 GB) are
   never shipped to clients — too big, and shipping them would leak the Source A/Source B-derived data.
   Clients get only per-query JSON.
2. **The API is the only client boundary.** TUI *and* web go through HTTP/JSON; no client ever opens
   SQLite or FAISS directly.
3. **Local-first, host-agnostic.** Config-driven base URL (never hardcode `localhost`), stateless
   engine, an empty pass-through **auth-middleware seam**, a CORS allowlist. Going public is a config
   change + a milestone, not a rewrite. (Auth, rate-limiting, accounts, and data-redistribution
   legal work are explicitly **YAGNI** for now — a conscious scope decision.)
4. **On-demand lifecycle — no systemd, no background service.** Pure read-only request/response;
   it must not linger when unused.
5. **mmap, don't load.** The 9 GB index is mmap'd (`IO_FLAG_MMAP`) so the OS pages in only the
   inverted lists a query touches (nprobe=64 → ~0.4% of lists). Loading it resident would blow the
   ~9 GB swap-thrash ceiling. The SQLite page cache is kept tiny (~48 MB/connection) for the same
   reason — mmap is file-backed and reclaimable, but the page cache is anonymous heap and *swappable*.

### Lifecycle — "last one out turns off the lights"
- **`sonic serve`** runs the engine foreground (Ctrl-C quits) — for the web client / power users.
- **The TUI** checks the configured URL on launch: dead → spawn the engine as a child and kill it on
  exit; alive → just connect (never two 9 GB mmaps).
- **Idle auto-shutdown** after ~15 min of zero requests. Nothing survives the user walking away.

### Why Python is fine here (the speed model — a good interview point)
Python is the **conductor, not the orchestra.** The hot paths — `faiss.search()` (C++) and SQLite
reads (C) — run as native code and **release the GIL**; Python only orchestrates, scores ~100
candidates, and serializes JSON (~2–4 ms of overhead). One uvicorn worker, sync handlers in the
threadpool, **one SQLite connection per thread** (connections aren't safe to share). Warm
`/search/similar` ≈ 15–50 ms.

### Two-tier hydration (the core efficiency principle)
Do the cheap numeric work (re-rank / filter) on the ~100–300 candidates using the compact
`track_search` hot table; do the expensive string joins (titles, artist names, album art, preview
URLs against `tracks`/`track_artists`/`artists`/`albums`) **only for the final ~20**.

### The HTTP API (actual endpoints)
| Method & path | Feature | Notes |
|---|---|---|
| `GET /health` | — | liveness; reports vector count + nprobe; triggers warmup |
| `GET /track/{id}` | F4 | full two-tier hydrated metadata |
| `GET /search?q=&limit=&offset=` | F1 | rules → (LLM fallback) → `track_search` scan |
| `POST /playlist` `{q,size}` | F2 | mood pool → transition-ordered list |
| `GET /artist/{name}` | F3 | aggregate features, dominant genre, top tracks |
| `GET /search/similar/{id}?k=20` | F6 | FAISS top-100 → re-rank → hydrate |
| `POST /library/scan` `{tracks[],size}` | F7 | identity-match → taste recs + breakdown |
| `POST /library/diagnose` | F7 | per-file match report (isrc/fuzzy/none) for coverage triage |

All routes sit behind a pass-through `require_auth` dependency (the seam) and a CORS allowlist;
all config is env-overridable (`SONIC_DB`, `SONIC_INDEX`, `SONIC_NPROBE`, `SONIC_LLM_PROVIDER`, …).

### Milestones (M0–M6 + F7 done; M7 web deferred)
| M | Feature | What shipped | Latency |
|---|---|---|---|
| **M0** | `track_search` hot table | 254.8M-row denormalized one-row-per-track table; features scaled to ints (0–1000), genre as a 0–19 macro id, full-covering `idx_search`. Filter source for F1/F2; re-rank source for F6. | F1 filters 0.4–1.9 ms |
| **M1** | Engine skeleton | FastAPI + uvicorn, mmap index, read-only WAL DB, LRU caches, config + auth seam + CORS, `/health` warmup, idle shutdown, `/track/{id}`. | boot <1 s, warm ~14 s |
| **M2** | F6 Similar (vector) | seed blob → FAISS top-100 → 5-term re-rank → hydrate top-K. | warm 40–60 ms |
| **M3** | F1 Semantic (rules) | mood+genre vocab (bigram-aware, coverage scoring) → whitelisted SQL builder → search. | 3–28 ms |
| **M4** | F1 LLM fallback | provider-agnostic (Groq/Gemini/OpenAI-compatible/Anthropic) via **stdlib REST, no SDK**; output whitelisted/injection-safe; success-only LRU cache; graceful degrade to rules. | 273–606 ms (Groq) |
| **M5** | F2 Playlist + F3 Artist | greedy transition-ordered walk (Camelot key penalty); artist catalog aggregation. | playlist 14–22 ms |
| **M6** | Rust TUI | ratatui + ureq thin client (links no FAISS/SQLite); spawn-or-connect lifecycle. | — |
| **F7** | Local Library Scan | identity-match local files → taste recs + breakdown. | — |

### The scoring formulas
- **F6 similar re-rank** (top-100 → top-K):
  `0.50·embedding_sim + 0.20·genre_match + 0.15·tempo_prox + 0.10·popularity + 0.05·era_prox`.
- **F2 playlist transition cost** (greedy nearest-neighbor over ~50 tracks):
  `3·tempo_diff + 2·energy_diff + 1.5·key_penalty + 1·valence_diff`, over a 70%-popular /
  30%-deep-cut candidate mix.

### Query understanding (the F1 rules + LLM design)
- **Rules vocabulary:** words map to scaled-int feature filters, e.g. `sad → valence<300,
  energy<500`; `workout/gym → energy>800, tempo>120, danceability>600`; `acoustic →
  acousticness>600`; `studying/focus → instrumentalness>500, energy<500, speechiness<100`.
- **LLM fallback:** when fewer than ~50% of meaningful query words match, an LLM emits a
  *constrained JSON filter spec* — `{"filters":[{"col","op","value"}]}` with whitelisted columns
  and ops (`<`,`>`,`between`), bounds-checked and **bound as parameters**. **The model never emits
  SQL** — a real injection-safety boundary, with graceful degradation to the rules result on any
  failure (missing key/SDK, bad JSON, network error). Validated live: "sad rock songs for studying"
  → Pink Floyd; "warm summer sunset" → Chappell Roan / bôa.
- **Provider-agnostic with zero hard SDK dependency:** Anthropic uses the SDK (with **prompt
  caching** on the static system instruction); Groq/Gemini/OpenAI-compatible go over **stdlib
  `urllib` REST** — chosen by an env var, lazily imported. A real gotcha solved: Groq sits behind
  Cloudflare and 403/1010-blocks requests with no real `User-Agent`, so the REST client spoofs a
  browser UA.

---

## 8. Phase 5 — The Rust TUI client

A thin **ratatui + ureq (no-TLS)** terminal client — links **no** FAISS/SQLite, holds no data.
Menu → { Query (semantic search) · Scan local library (F7) · Transition Playlist }; results → track
detail; `s` for similar; `--vim` adds hjkl navigation; `--spawn` launches the engine and kills it on
exit. Built in its own Nix devshell (`nix develop .#tui`). The Zinthos banner is generated by
`figlet` from a vendored "Delta Corps Priest 1" font. Compiles clean; the one piece still wanting a
human to drive the interactive UI for final sign-off. Demonstrates a **polyglot architecture** with
a strict language boundary (Python brain / Rust client, talking only over HTTP/JSON).

The **Transition Playlist** menu option does single-mood smooth ordering (reuses the F2 engine). A
true A→B "mood arc" (sad → hopeful) is intentionally **not** built — it needs a valence/energy
gradient + path-ordered selection, and was deferred.

---

## 9. Phase 6 — Local Library Scan (F7)

**The core constraint that defines the feature:** the entire ML stack is trained on Source A's
*proprietary* 13 audio features, which are **not reproducible from a raw MP3/FLAC**. So the system
cannot "analyze a local file and embed it." Instead it **identifies** which catalog track each file
is (by its tags), then reuses everything already built (embeddings → recommendations, genres → taste
breakdown). Recognizing this constraint *up front* and reframing the feature around it is itself the
key design insight.

**Flow:** the TUI walks a folder (`walkdir`), reads tags (`lofty`: title/artist/album/duration/ISRC),
and sends *only small JSON* to `POST /library/scan`. The engine then:
1. **match()** — ISRC exact match first (uses the existing `idx_tracks_isrc`), then fuzzy
   title+artist via a **sidecar** `track_match.db` (a ~22 GB normalized-key table, ATTACHed
   read-only).
2. **recommend()** — mean-pool matched embeddings into a taste centroid → FAISS, excluding owned.
3. **breakdown()** — genre + era histograms.

**The load-bearing piece** is the shared text normalizer (`textnorm.py`): build-time and query-time
must compute the *identical* normalized key (`"<norm title>|<norm artist>"`) or nothing matches. It
preserves non-ASCII (CJK/accents) while folding case, brackets, `feat.`, `- Remaster/Live` tails,
and punctuation. Query-side it probes *every* credited artist + title variants, because the sidecar
stores only one arbitrary artist per track. The sidecar itself (~255.9M rows, 22 GB, built in
~43 min) is another out-of-core build: streamed cursor, 200k insert sub-batches, `journal_mode=OFF`,
on-disk index sort, RAM peak ~330 MB.

**Coverage on a real 726-file personal library: 92.6%** (ISRC 77.0% → +full-key fuzzy 90.4% →
+first-artist 91.5% → +candidate-keys 92.6%). The remaining ~54 are a genuine ceiling, not bugs:
romanization (CJK titles in native script), genuinely-absent tracks (post-catalog-cutoff,
NCS/vocaloid, sped-up/mashup rips), and artist-name spelling variants. A `diagnose` tool produces a
per-file, misses-first JSON report for triage. (A future acoustic-fingerprinting path —
Chromaprint → AcoustID → MusicBrainz → ISRC — is specced but deferred.)

---

## 10. Cross-cutting decisions & recurring themes

- **The 9 GB ceiling drove the architecture.** fp16 over Flat; mmap over load; out-of-core ETL;
  streamed training prep; persist-before-validate; tiny SQLite page cache. Every "why not the bigger
  obvious thing" answer is *9 GB*.
- **Measure, don't theorize, for novel work.** The α=0.1 guess was confidently backwards; the PRD's
  IVFPQ choice was confidently wrong. Cheap empirical sweeps beat confident priors both times.
- **Right tool for the data regime.** GBDTs beat NNs on 13-D tabular data; PQ is the wrong quantizer
  below ~32-D.
- **One scaler / one feature order, end to end.** The single invariant that makes the embedding
  space meaningful; the `genre_scaler_v2` near-misses are the cautionary tale.
- **Keyset pagination, never LIMIT/OFFSET** at 255M-row scale (O(n) vs. O(n²)).
- **Precision over coverage where it counts.** Genre prediction *skips* low-confidence tracks rather
  than guessing; F7 leaves hard misses unmatched rather than wrong-matching.
- **Forward-compat for free, not features for free.** Config-driven URLs, auth seam, CORS, stateless
  engine — cheap to leave in now, expensive to retrofit; everything else is YAGNI until going public.
- **Resumability everywhere.** Every multi-hour job (ETL, genre prediction, embedding, sidecar
  build) checkpoints and resumes — non-negotiable when a single pass is hours long on one machine.

---

## 11. Where we ended up — status

| Layer | Status | Headline result |
|---|---|---|
| Data ETL (C++) | ✅ Done | 145 GB normalized `master.db`, 254.8M featured tracks, ~348M associations |
| Genre prediction (LightGBM) | ✅ Done | beat RF 45.9% baseline; 49.2M new genres written via two-brain cascade |
| Embeddings (PyTorch SAE) | ✅ Done | 254.8M × 10-D, genre-coherent, α=0.5 |
| Vector index (FAISS) | ✅ Done | IVFSQfp16, 9.17 GB, **94.3% recall@10**, nprobe=64 |
| Engine (FastAPI) | ✅ Done | F1/F2/F3/F4/F6 + F7; warm latencies single-/double-digit ms |
| LLM fallback | ✅ Done | provider-agnostic, injection-safe, cached |
| Rust TUI | ✅ Built | compiles clean; pending interactive human verify |
| Local library scan (F7) | ✅ Done | 92.6% coverage on a real 726-file library |
| Web client (M7) | ⏳ Deferred | lean Svelte/vanilla, same JSON API |

**From** 266 GB of three mismatched raw dumps and a 45.9% Random Forest on an old flat DB,
**to** a queryable 255M-track engine with genre-coherent vector search, semantic + LLM query
understanding, playlist generation, and local-library taste analysis — all running inside a 15 GB
RAM / 6 GB VRAM laptop.

---

## 12. Tech stack & full repository map

### Tech stack
- **Environment:** NixOS + Nix flake devshells (default C++/Python · `.#tui` Rust · `.#frontend`
  Node). Python via a `--system-site-packages` venv; CUDA libs wired through `LD_LIBRARY_PATH`.
- **Data engineering:** C++17 (`-O3`), SQLite (C API), WAL, `ATTACH` + `INSERT…SELECT`.
- **Machine learning:** Python — LightGBM, scikit-learn, PyTorch, RAPIDS cuML/FIL (GPU inference),
  NumPy, pandas; FAISS (`faiss-cpu`); joblib for artifact persistence.
- **Backend:** Python, FastAPI + uvicorn, Pydantic, stdlib `sqlite3`, `faiss-cpu`, stdlib-`urllib`
  REST LLM client (+ optional Anthropic SDK with prompt caching).
- **Client:** Rust — ratatui, crossterm, ureq (no-TLS), serde, walkdir, lofty.
- **Vector/ANN concepts:** IVF, scalar/product quantization, cosine via L2-normalized inner product,
  recall@10, nprobe tuning.

### Repository map
```
sonic_something/
├── CLAUDE.md / LLM_PRD.md / PRD_Version2.md   project spec + AI-context system
├── flake.nix                                  Nix devshells (default / tui / frontend)
├── master.db                                  145 GB normalized substrate (gitignored)
├── track_match.db                             22 GB F7 fuzzy-match sidecar (gitignored)
├── database/                                  C++ ETL
│   ├── master_db_builder.cpp                  streaming ATTACH+INSERT…SELECT builder
│   ├── schema.sql                             authoritative 10-table DDL
│   └── implementation_plan_v2.md, schema_er.md, db_validation_checklist.md
├── model_training/                            Python ML
│   ├── train_genre_gbdt.py, predict_genres.py            (F5 genre)
│   ├── prep_sae_data.py, train_sae.py, embed_tracks.py, build_faiss.py  (F6)
│   ├── genre_gbdt.{txt,joblib}, genre_scaler.joblib, sae_encoder.pt     (artifacts)
│   ├── embeddings.faiss                       9.17 GB vector index
│   └── F6_design_decisions.md, F6_implementation_plan.md
├── backend/                                   the engine + clients
│   ├── engine/  (app, config, db, index, hydrate, rules, filters,
│   │             search, llm, similar, playlist, artist, library, textnorm)
│   ├── build_track_search.py                  M0 hot-table builder
│   ├── build_track_match.py                   F7 sidecar builder
│   └── implementation_plan.md, local_library.md, CLAUDE.md
└── tui/                                       Rust ratatui client (src/main.rs, src/bin/diagnose.rs)
```

### Key numbers cheat-sheet
- Raw input **266 GB** (117 + 39 + 110) → output `master.db` **145 GB**.
- **254,819,856** featured tracks; ~**348M** artist associations; **20** macro genres.
- Genre ground truth **125.26M**; ML-predicted & written **49.24M** (of 130.12M unlabeled), 54.7 min on GPU.
- Embeddings **254,819,846 × 10-D**; FAISS **9.17 GB**, **94.3% recall@10**, nprobe=64, ~ms/query.
- GPU genre inference **~145×** faster than CPU; **99.94%** argmax agreement.
- F7 local-library coverage **92.6%** on a 726-file library.
- Hardware **15 GB RAM / 6 GB VRAM**; ~9 GB resident = swap-thrash ceiling.

---

## 13. Deferred / next (intentionally not built — useful to know the boundaries)

- **Mood-arc playlists** (A→B journey) — needs a valence/energy gradient + path-ordered selection.
- **Web client (M7)** — lean Svelte/vanilla on the same JSON API.
- **Mood classifier (PRD Phase 2)** — multi-label, sigmoid / `BCEWithLogitsLoss`, likely on
  MusicBrainz folksonomy tags.
- **F7 follow-ups** — multi-ISRC tag split, taste-centroid clustering for better recs, owned-dupe
  exclusion, an all-artists sidecar rebuild (~348M rows), and **acoustic fingerprinting**
  (Chromaprint → AcoustID → MusicBrainz → ISRC) for untagged/romanized files.
- **IVFFlat rebuild** — only if 94.3% recall ever feels weak.
- **Hosting / auth / rate-limiting / accounts** — only if going public.

---

## 14. Résumé & interview material

> This section is the point of the document for the resume conversation. The assistant can draw on
> everything above; this just pre-digests it into resume-shaped pieces. Treat the bullets as **raw
> material to refine**, not finished copy — pick the 3–5 strongest for the actual resume and tune
> the verbs/metrics to the target role (data engineering vs. ML vs. backend vs. full-stack).

### 14.1 Project one-liner (several framings)
- *Music search engine over 255M tracks that finds songs by how they sound and feel, combining a
  C++ ETL pipeline, LightGBM/PyTorch ML, FAISS vector search, and a FastAPI backend — built to run
  on a 15 GB-RAM laptop.*
- *(ML-leaning)* *Built a genre-coherent audio-embedding + vector-search recommender over a
  quarter-billion tracks, with a supervised autoencoder and a 9 GB FAISS index at 94.3% recall@10.*
- *(Data-eng-leaning)* *Designed and built a memory-bounded C++ ETL that consolidates 266 GB of
  heterogeneous music data into a single normalized 145 GB SQLite database of 255M tracks.*

### 14.2 Quantified achievements (the metrics worth featuring)
- Consolidated **266 GB** of heterogeneous sources into a normalized **145 GB** SQLite DB of
  **255M tracks / ~348M associations**, on a **15 GB-RAM** machine (fully out-of-core).
- Cut bulk-load time **38×** by removing a mis-applied `WITHOUT ROWID` optimization (8,812 s → 230 s).
- Trained a **LightGBM** genre classifier on **124M** rows that beat a 45.9% Random Forest baseline
  (after NNs plateaued ~41%); predicted genres for **130M** unlabeled tracks, writing **49M**
  high-confidence labels via a precision-gated cascade in **54.7 min on GPU**.
- Achieved a **~145× speedup** moving genre inference to GPU (RAPIDS cuML FIL), turning a 3–4 day job
  into hours, at **99.94%** agreement with the CPU model.
- Trained a **PyTorch supervised autoencoder** (13→10-D) and generated embeddings for **254.8M**
  tracks in **21.7 min** (~220k rows/s).
- Built a **FAISS** vector index of **254.8M × 10-D** vectors achieving **94.3% recall@10** in
  **9.2 GB** (selected via a quantizer sweep that rejected the originally-specified IVFPQ).
- Delivered a **FastAPI** engine serving 8 endpoints with **warm latencies of 0.4–60 ms** (FAISS
  similar-track search in <100 ms), including an injection-safe, provider-agnostic LLM query
  fallback.
- Shipped a local-library scanner reaching **92.6%** identity-match coverage on a real 726-file
  library via ISRC + fuzzy normalized-key matching over a 22 GB sidecar index.

### 14.3 Skills / technologies demonstrated (inventory for keyword matching)
- **Languages:** C++ (17), Python, Rust, SQL, Bash; Nix.
- **Data engineering:** large-scale ETL, SQLite internals (WAL, `ATTACH`, covering indexes,
  `WITHOUT ROWID` tradeoffs), schema normalization, out-of-core / streaming processing, keyset
  pagination, resumable/checkpointed batch jobs, data validation.
- **Machine learning:** LightGBM / gradient-boosted trees, scikit-learn, PyTorch, autoencoders &
  representation learning, supervised/multi-task losses, class-imbalance handling, GPU inference
  (RAPIDS cuML FIL), model/scaler artifact management, empirical hyperparameter tuning.
- **Vector search / IR:** FAISS, IVF indexes, scalar/product quantization, cosine similarity,
  recall@k evaluation, nprobe tuning, two-stage retrieve-then-rerank.
- **Backend:** FastAPI, uvicorn, Pydantic, REST API design, LRU caching, prompt-injection-safe LLM
  integration, prompt caching, config-driven/host-agnostic design, CORS/auth seams, process
  lifecycle management.
- **Systems / performance:** memory-bounded design under hard RAM limits, `mmap` vs. resident
  tradeoffs, swap behavior, GIL-aware concurrency, filesystem-aware IO (exfat vs. ext4).
- **LLM integration:** provider-agnostic clients (Anthropic SDK + stdlib REST for Groq/Gemini/OpenAI),
  constrained JSON output, structured-output validation/whitelisting.
- **Tooling:** Nix flakes/devshells, Git, CMake.

### 14.4 Candidate resume bullets (pick & refine)
*Data engineering angle*
- Architected a memory-bounded **C++17** ETL pipeline that consolidated **266 GB** of heterogeneous
  Source A/Source B SQLite + CSV sources into a normalized, integer-keyed **145 GB SQLite** database of
  **255M tracks**, using streamed `INSERT…SELECT` and post-load indexing to stay within **15 GB RAM**.
- Diagnosed and removed a `WITHOUT ROWID` mis-optimization that was inflating a 256M-row load **38×**
  (cut 8,812 s → 230 s).

*Machine learning angle*
- Trained a **20-class LightGBM** genre classifier over **124M** labeled tracks (beating a 45.9% RF
  baseline after deep nets plateaued ~41%) and inferred genres for **130M** unlabeled tracks via a
  confidence-gated "two-brain" cascade, accelerated **~145×** with **GPU (RAPIDS cuML FIL)**.
- Designed a **supervised autoencoder (PyTorch)** compressing 13 audio features into a 10-D
  genre-coherent embedding, then indexed **254.8M** vectors in **FAISS** at **94.3% recall@10** in
  **9.2 GB** — selecting IVF-SQ-fp16 over the originally-specified IVF-PQ after an empirical recall
  sweep showed PQ collapses at low dimensionality.

*Backend / full-stack angle*
- Built a **FastAPI** "engine" exposing semantic search, vector similarity, playlist generation, and
  artist/track endpoints over a quarter-billion-row dataset, with **0.4–60 ms** warm latencies via
  `mmap`'d FAISS + a covering-index "hot table" and two-tier hydration.
- Implemented a **provider-agnostic, injection-safe LLM fallback** that translates abstract queries
  ("rainy 3am drive") into whitelisted, parameter-bound audio-feature filters, with graceful
  degradation to a rule-based parser.

### 14.5 Interview talking points ("tell me about a hard problem")
- **Working under a hard memory ceiling at quarter-billion-row scale** — the unifying constraint;
  mmap-don't-load, out-of-core everything, persist-before-validate (the 85-min build incident).
- **Choosing the right model for the data regime** — why GBDTs beat neural nets on 13-D tabular
  data, and why PQ is the wrong vector quantizer below ~32 dimensions.
- **Being empirically humble** — the α=0.1 guess and the IVFPQ spec were both confidently wrong and
  both fixed by cheap sweeps; the ANN recall that *improved* with scale (89.6% → 94.3%) due to
  density.
- **Designing for a constraint instead of fighting it** — F7's "we can't reproduce Source A's audio
  features from a raw file, so identity-match instead" reframing.
- **Safety boundaries with LLMs** — the model proposes structured JSON; the server whitelists and
  parameterizes; the model never emits SQL.
- **Forward-compatibility as a cheap discipline** — config-driven URLs, auth seam, stateless engine
  so "go hosted" is a config change, not a rewrite.

### 14.6 Honest caveats (so the resume stays defensible)
- It's a **solo, personal project**, not production traffic; "users" means the author's own library.
- The genre/embedding models are **moderate-accuracy** by design (genre ~45–50%, which is expected
  and *correct* given acoustically-overlapping genres — be ready to explain why that's not a flaw).
- The web client (M7) is **deferred**; the TUI is built but awaits a final interactive human pass.
- The underlying catalog data is **third-party** (Source A/Source B-derived) and is deliberately never
  redistributed — worth stating plainly if asked about data provenance/licensing.

---

### See also (deeper module docs, if the repo is available)
- `database/schema.sql`, `database/implementation_plan_v2.md` — ETL + schema.
- `model_training/F6_design_decisions.md` — the full embeddings/FAISS retrospective.
- `backend/CLAUDE.md`, `backend/implementation_plan.md`, `backend/local_library.md` — engine + F7.
- `LLM_PRD.md` — product spec (F1–F6) and latency budgets.
