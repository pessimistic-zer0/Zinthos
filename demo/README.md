# Deploying the Zinthos demo

A free, always-on, public demo of the real engine — portal, API and all four modes — on a
Hugging Face ZeroGPU Space.

Everything here runs on the free tier. Nothing needs a payment method.

---

## The problem, and the shape of the answer

The code was never the obstacle. The engine is ~2,300 lines of Python and the portal is a
1.1 MB static bundle; both fit anywhere. The obstacle is that they read a **162 GB
`master.db`** and a **9 GB FAISS index**, and a free Space is 2 vCPU / 16 GB RAM / 50 GB of
**non-persistent** disk.

So the demo ships a **slice**: some millions of tracks carrying every column the engine reads,
with an exact FAISS index over their embeddings. Three things make that a demo rather than a
mock-up:

1. **No code is forked.** `backend/engine/config.py` already made every artifact path
   env-overridable, so the Space runs the same modules the local engine does — same
   `search.py`, same `similar.py` re-rank, same dedupe — with `SONIC_DB` and friends pointed
   at smaller files.
2. **Track ids are not remapped.** The slice keeps `master.db`'s rowids, so a `track_id` means
   the same thing in the slice, in FAISS, and in the tag selectors. A subset, not a
   translation.
3. **The slice is not just chart pop.** Measured against the tag bitmaps, a cut at
   `popularity >= 30` is 2,063,150 tracks in which every one of the 22 region/sonic families
   still has tens of thousands of members — so the F6 family gate, the thing that pulls a
   Bollywood seed back out of Thai pop, still has something to bite on:

   | family | in the cut | | family | in the cut |
   |---|---|---|---|---|
   | region/south_asian | 70,457 | | sonic/metal | 49,562 |
   | region/mena | 56,042 | | sonic/classical | 29,284 |
   | region/african | 46,999 | | sonic/jazz | 82,672 |
   | region/latin | 222,790 | | sonic/hip_hop | 284,570 |

   The gate needs ≥ `max(40, 2k)` same-family candidates in a pool (`CONFIG.tag_gate_min`).
   These clear that by three orders of magnitude.

---

## How big can it be?

**~535 bytes per track**, measured on a built slice (`demo.db` + index + match sidecar + tag
ids). So:

| `--min-pop` | candidates | kept after dedupe | bundle |
|---|---|---|---|
| 40 | 699k | ~600k | 0.32 GB |
| **30** (default) | 2,063,150 | **1,796,361** | **0.96 GB** |
| 25 | 3.32M | ~2.85M | 1.5 GB |
| 20 | 5,203,818 | **4,449,753** | 2.4 GB |
| 15 | 8.03M | ~6.9M | 3.7 GB |
| 10 | 12.4M | ~10.6M | 5.7 GB |
| 5 | 20.3M | ~17.4M | 9.3 GB |
| 1 | 45.1M | ~38.6M | **20.6 GB** |

Bold rows are measured; the rest apply the measured ~86% dedupe survival rate.

**A popularity threshold bottoms out at `--min-pop 1`.** 210M of the catalogue's 255M featured
tracks sit at popularity 0, so that cut already takes everything anyone has ever played. To go
past it — or to keep the long tail's *character* rather than only its famous end — use
`--sample-tail N`, which draws N more uniformly from below the floor. A 50k test draw
contained all 22 tag families (thinnest 415, fattest 3,742), so the tail is diverse, not noise.

```sh
# ~30 GB: everything with any popularity, plus 20M from the zero-popularity tail
python demo/build_demo_slice.py --min-pop 1 --sample-tail 20000000
```

Free-tier ceilings for context: HF gives a free account **100 GB of private storage** (public
is best-effort), covering datasets and buckets alike, and a Space has **50 GB of ephemeral
disk**. Neither is what bites first — see *Getting a large slice onto the Space* below.

---

## Build the slice

Run these on the machine that holds `master.db`.

```sh
cd /path/to/zinthos

# 1. the slice itself → demo/dist/demo.db
python demo/build_demo_slice.py

# 2. the FAISS index over its embeddings → demo.faiss
python demo/build_demo_index.py

# 3. the name-resolution sidecar → demo_match.db
SONIC_DB=demo/dist/demo.db SONIC_MATCH_DB=demo/dist/demo_match.db \
  python backend/build_track_match.py

# 4. the F6 family selectors → demo/dist/tag_bitmaps/
SONIC_DB=demo/dist/demo.db SONIC_BITMAP_DIR=demo/dist/tag_bitmaps \
  python backend/build_tag_bitmaps.py --ids
```

Step 1 is ~7 minutes plus ~1 min per million tracks; `--sample-tail` adds a fixed ~3.5 min
scan. `--smoke` builds a ~30k-track slice — **not much faster**, because phase 3 scans
`master.db` once per table regardless of how many rows it keeps — but it proves the whole path.

### Knobs

| flag | effect |
|---|---|
| `--min-pop N` | popularity floor. See the table above |
| `--sample-tail N` | also draw N tracks uniformly from **below** the floor |
| `--family-floor N` | top up any tag family under N members. Default 15,000; insurance for a raised `--min-pop` |
| `--no-dedupe` | keep catalog copies. Bigger, and mega-hits get pools full of themselves |
| `--no-compact` | write the naive layout (~751 B/track instead of ~535), to A/B a suspicion |
| `--limit N` | cap the candidate count, most popular first |

---

## Why the build is shaped the way it is

**Phase 3 scans, it does not probe.** A cold random probe on the drive `master.db` lives on
costs 4.2 ms, so probing per kept id would be ~1.6 hours *per table*. A rowid-range scan reads
the same table forward: 107 s for all of `track_search`, 52 s for `track_audio_features`, 55 s
for `track_artists`. The cost is set by `master.db`'s size, not the slice's. `CROSS JOIN` is
load-bearing there — with a plain `JOIN`, SQLite inverts the loop straight back to one random
probe per id. Phase 2 (dedupe) *does* probe, in track_id-ascending chunks, because sorted PK
probes measure 0.068 ms.

**The compaction.** `dbstat` on a naive build showed where the bytes go, and three of them
turned out to be pure waste — ~751 B/track down to ~535 with no loss of function:

| what | saved | why it was waste |
|---|---|---|
| `track_audio_features` → `camelot_code` only | 94 B | 17% of the database, and `playlist.py:69` reads exactly one column of it. Tempo/energy/valence come from `track_search` in the same SELECT |
| `preview_url` → 20-byte blob | 85 B | 107 bytes of which 67 never vary. `unhex()` packs the middle at build time; `hydrate.init_preview` rebuilds from the template in `demo_meta` |
| `ml_10d_embeddings` → dropped | 66 B | Redundant against an **exact** index. `index.reconstruct()` returns the stored vector with **max error 0.0** — verified on every build |

That last one is why `build_demo_index.py` prints a reconstruct round-trip check: dropping the
table is only safe on a flat or IVFFlat index, so the builder measures it rather than assuming
it, and tells you to rebuild `--no-compact` if it ever fails. The vectors travel to the index
builder as a flat sidecar (`demo_vectors.f32` / `demo_vector_ids.i64`) which is **not**
uploaded — it is a build artifact, not a runtime one.

Nothing about this is demo-only trickery: the compacted slice returns **byte-identical**
results to the naive one. Same seeds, same scores, same order.

---

## Deploy

```sh
pip install huggingface_hub          # once
hf auth login                        # once

# the slice → a PRIVATE dataset repo
python demo/upload_artifacts.py --repo <you>/zinthos-demo-slice --private

# the engine → a public Space, with the slice mounted; the portal → a static Space
python demo/push_space.py \
    --repo <you>/zinthos \
    --dataset <you>/zinthos-demo-slice \
    --mount \
    --split <you>/zinthos-portal
```

**Public Space, private dataset.** The Space is public — that is the demo, and `create_repo`
leaves it so. The dataset behind it is private, for two reasons: this project's own rule that
the third-party data is never redistributed (`README.md`, `backend/CLAUDE.md`), and the fact
that HF's free tier documents a hard 100 GB *private* allowance while public storage is
"best-effort" and explicitly asks people not to park large datasets there. A private dataset
mounts into your own Space exactly the same way; it only costs an `HF_TOKEN` read secret.

`push_space.py` assembles the Space tree in a temp dir and uploads it — `app.py`,
`requirements.txt`, the Space card, a verbatim copy of `backend/engine/`, and a fresh
`VITE_DEMO=1` build of the portal. Nothing is committed to this repo and `frontend/dist` is
left alone.

Then, in the Space's settings on the Hub:

| setting | value | why |
|---|---|---|
| Hardware | **ZeroGPU** | set for you at creation — see below. Nothing here touches the GPU |
| Variable `ZINTHOS_DATASET` | `<you>/zinthos-demo-slice` | set for you by `--dataset` |
| Secret `SONIC_LLM_PROVIDER` | `groq` | the F1 LLM fallback |
| Secret `GROQ_API_KEY` | your key | Groq's free tier rate-limits rather than bills |
| Secret `SONIC_LLM_MODEL` | `llama-3.3-70b-versatile` | |
| Secret `HF_TOKEN` | a read token | **only if** the dataset repo is private |

Leave the three LLM secrets unset and search falls back to the rules parser — no crash, and
`llm_fallback_recommended` shows up in the response instead.

### Hardware is chosen at creation, not afterwards

`push_space.py` passes `--hardware` (default `zero-a10g`) to `create_repo`. That is not a
convenience — it is the only order that works on a free account:

> `402 Payment Required` — *"Static Spaces are free for everyone, but hosting Gradio and
> Docker Spaces on free cpu-basic requires a PRO subscription."*

A Gradio Space defaults to `cpu-basic`, which needs PRO, so the **create** is what gets
refused. There is no create-then-upgrade path: the free allowance is specifically ZeroGPU (up
to 2 Spaces, personal account in good standing), and it has to be asked for up front. The
static portal Space is unaffected — static Spaces are free for everyone.

### What the Space serves

| path | |
|---|---|
| `/` | the React portal |
| `/api/*` | the FastAPI engine, unmodified |
| `/gradio` | a plain search box — fallback UI, and the Gradio SDK's foothold |
| `/manifest.json` | what this deployment is serving, plus live boot state |

A free Space must be `sdk: gradio` (Gradio and Docker Spaces need a paid plan, and ZeroGPU is
Gradio-only), but the project's front end is React. So Gradio is mounted *inside* a FastAPI
app rather than being the app. `gr.mount_gradio_app` composes onto the existing lifespan
rather than replacing it, which keeps the engine's own startup — FAISS mmap, warmup, tag-table
drift checks — running exactly as it does locally.

If that ever breaks on a platform change, set `ZINTHOS_UI=gradio` in the Space variables: the
app falls back to a bare `demo.launch()`. The portal goes away; the demo keeps working.

---

## Getting a large slice onto the Space

The Space's disk is wiped whenever it restarts, and a free Space sleeps after 48 hours without
visitors. So a downloaded slice is re-pulled on every wake. Two ways to handle that, and the
right answer depends on a number nobody has measured yet.

### Download (default) — fast reads, a cold-start wait

`snapshot_download` puts the slice on the container's **local** disk, so queries run at local
speeds. The cost is the pull on each wake.

**Deferred boot** removes the risk that would otherwise create. Importing the engine touches
no files — only its *lifespan* opens the database and mmaps the index — so when the artifacts
are absent, `app.py` binds port 7860 immediately and runs the download plus the engine startup
on a background task. Measured: **port bound in 2 s with nothing on disk**. Meanwhile:

- `/` serves the portal normally (it is static)
- `/api/*` returns `503` with a live progress line — `warming up — downloading, 412 MB so far (37s)`
- `/manifest.json` carries the same state under `"boot"`
- the portal retries `/health` every 4 s, so its track count appears the moment the engine is up

### Mount (`--mount`) — no pull at all, unknown read latency

```sh
python demo/push_space.py --repo <you>/zinthos --dataset <you>/slice --mount
```

This attaches the dataset repo as a read-only volume at `/data` and points
`ZINTHOS_ARTIFACTS` there. No download, no ephemeral-disk usage, nothing to re-pull on wake,
and the 50 GB disk stops being a ceiling.

**The catch, stated plainly: this is untested for read latency.** The engine's workload —
random 4 KB SQLite page reads and an mmap'd index — is the worst case for network-backed
storage, and the docs say nothing about how a mounted repo performs. It could be transparently
cached and perfect, or far slower than local disk.

**So deploy with the download path first**, because its performance is known, then flip the
mount on and compare. It is one API call to set and one to undo; a handful of
`/api/search/similar` calls against the live Space will settle it in a minute.

---

## Why the portal is split out (`--split`)

A free Space sleeps after 48 hours idle, and waking it takes time — container start, plus the
mount or the download. If the portal is served *from inside* that Space, none of it is
reachable until the wake finishes, so a cold visitor gets Hugging Face's "Space is starting"
screen and nothing to look at.

A **static** Space never sleeps and is free for everyone. With `--split`:

```
  <you>-zinthos-portal.hf.space     static, always on    the React portal
              │  fetch /health on mount  ─────────────┐
              ▼                                        ▼
  <you>-zinthos.hf.space/api        ZeroGPU, sleeps    the engine
```

The landing page paints immediately; `frontend/src/lib/demo.ts` calls `/health` the moment
`DemoNotice` mounts, and **that request is what wakes the engine**. It retries every 4 s for
up to 12 minutes — the old 4-minute ceiling was *shorter than the cold start it was waiting
for*, so the poll could give up while the engine was still downloading.

The wait is not silent. While the engine warms, `/health` answers 200 with `status != "ok"`
plus `stage`, `downloaded_mb` and `total_mb`, and `DemoNotice` moves to the top right and
reports it — "loading the catalogue slice — 9.6 of 27.0 GB" with a bar, then "opening the
index", then back to the foot of the screen as the track count. `total_mb` is read from the
dataset repo on the Hub at boot rather than hardcoded, so it cannot drift when the slice is
rebuilt; if that call fails the readout simply loses its denominator.

Two consequences worth knowing:

- **The engine URL is baked in at build time.** `--split` sets `VITE_ENGINE_BASE` to
  `https://<owner>-<name>.hf.space/api`; `api.ts` already reads it and falls back to a
  same-origin `/api` without it. Change the engine's repo id and you must re-push the portal.
- **CORS stops being a safety net — and on a Space it is not a boundary either.** The
  browser now calls the engine cross-origin, so `--split` sets `SONIC_CORS` to the portal's
  origin on the engine Space, and `engine/app.py` passes it to `CORSMiddleware` as an
  explicit allowlist. That allowlist is real when you run the engine yourself. **On a public
  Space it is not enforced**: Hugging Face's edge proxy answers preflights itself and
  reflects whatever `Origin` it is sent. Measured 2026-09-21 — `OPTIONS` against a path that
  does not exist in the app returned `200` with `access-control-allow-origin` echoing an
  arbitrary origin, and with no `server: uvicorn` header, so the request never reached
  uvicorn at all.

  That costs nothing here and should not be "fixed": the engine is public, unauthenticated
  and read-only, there are no cookies or credentials, and anyone can `curl` the same public
  data directly. CORS only ever governed *browser* reads of exactly what is already open. It
  is recorded because the setting looks like protection and is not — do not put anything
  behind `SONIC_CORS` on a hosted Space that you would not put on an open endpoint.

  Set it correctly anyway: the value must be the portal's real origin, read from the Hub by
  `space_origin` rather than derived, and `--origin` overrides it.

Without `--split` everything is served from the one Space at `/`, `/api` and `/gradio`, which
is simpler and fine for a Space that stays warm.

## Keeping it warm

The 4½-minute cold start above is avoidable, and it is worth avoiding: measured on a real
boot, 27 GB at ~115 MB/s is 249 s, plus ~15 s to open the index.

`fetch_artifacts()` short-circuits on `os.path.exists(demo.db)`, so the pull only happens when
the container's ephemeral disk is empty — which is to say, on a *fresh container*. The Space
sleeps after 48 idle hours (the Hub reports `gcTimeout: 172800`) and waking it starts a fresh
one. **A single request resets that idle timer**, so a periodic ping keeps the container, and
with it the slice already on its local disk.

`/health` is the right thing to ping: `backend/engine/app.py` answers it with three field
reads off a loaded index — no SQLite query, no vector search, and it never touches the GPU, so
it costs nothing against the ZeroGPU allowance.

**This repo does not schedule that ping.** It is a cron job at
[cron-job.org](https://cron-job.org) hitting `https://<owner>-<name>.hf.space/api/health` every
6 hours, with failure notifications on — a keep-warm that has quietly stopped is
indistinguishable from one that is working, right up until a visitor pays for it. GitHub
Actions was the other candidate and was the worse one: it disables scheduled workflows on a
public repo after 60 days of inactivity, and its cron is best-effort.

What a ping cannot prevent is a restart you did not ask for — a push, a platform reschedule,
an OOM. Those still cost the full pull, which is what the warm-up readout is for.

A mounted dataset volume (`--mount`) would remove the pull from *every* cause rather than just
the common one, but it trades a boot cost for a per-query one: this engine does random 4 KB
SQLite reads and mmaps its index, which is the worst case for network-backed storage. Both
paths ship; the trade is unmeasured. See the `--mount` notes above.

## Run it locally first

```sh
pip install "gradio>=5,<6" huggingface_hub
cd frontend && VITE_DEMO=1 npx vite build --outDir ../demo/static --emptyOutDir && cd ..
ZINTHOS_ARTIFACTS=$PWD/demo/dist ZINTHOS_STATIC=$PWD/demo/static PORT=7860 \
  python demo/app.py
```

Then open <http://127.0.0.1:7860/>. This is the same process the Space runs.

---

## How the demo differs from production

| | production | demo |
|---|---|---|
| Catalog | 254.8M tracks | the slice |
| Index | `IndexIVFPQ`, nlist 32768, 9 GB, nprobe 64 | `IndexFlatIP`, 48 B/vector, **exact** |
| Family filter | `IDSelectorBitmap`, 22 × 32 MB | `IDSelectorBatch`, ~0.3 MB total |
| Seed vectors | `ml_10d_embeddings` BLOB | `index.reconstruct()` — exact, verified per build |
| `preview_url` | full 107-char URL | 20 packed bytes, rebuilt on hydrate |
| Idle behaviour | watchdog SIGTERMs after 15 min | disabled (`SONIC_IDLE_SECS=0`) |
| Threads | 8 | 4 (2 vCPU) |

Two are worth knowing when comparing the two by ear:

- **The demo's neighbours are exact.** At this scale there is nothing to compress and nothing
  to approximate, so a result the demo finds and the big index misses is an IVFPQ recall miss,
  not a difference in the slice. Measured on 2 threads: 10M vectors at k=1500 is 18 ms.
- **The selector format changed, not the feature.** A bitmap is sized by the *id space*, so
  describing 70k South Asian tracks that still carry ids up to 256M costs 32 MB of almost
  entirely zeroes — 700 MB for 22 families. `faiss.IDSelectorBatch` is sized by *membership*
  and hashes the ids, so lookup stays O(1). `backend/engine/bitmaps.py` reads both formats and
  `manifest.json` says which is present; both hit the same drift guard against
  `engine.tagfamily`.

`SONIC_IDLE_SECS=0` is not optional. `backend/engine/main.py` ships a watchdog that SIGTERMs
the process after 15 idle minutes — right for an on-demand local engine, fatal for a hosted
one, which would kill itself between visitors.

---

## Honesty about the catalogue

Every string in the portal was written for the full 255M-track catalogue, and the demo serves
a slice. Rather than rewrite the voice of the site for one deployment, `VITE_DEMO=1` turns on
`frontend/src/lib/demo.ts`: a one-line notice at the foot of the screen states what this is
and gives **the live count from `/api/health`**, not a constant, so it cannot drift from what
is really searchable. Two claims that describe what you are searching *right now* soften with
the same flag. With the flag unset — every local build — none of it renders and the app is
byte-for-byte what it was.

---

## Troubleshooting

**`no demo.db under … and ZINTHOS_DATASET is unset`** — add `ZINTHOS_DATASET` in the Space's
settings, or re-run `push_space.py --dataset`.

**Stuck on "warming up"** — `GET /manifest.json` and read `"boot"`. `stage: "downloading"` with
a rising `downloaded_mb` is healthy; `stage: "failed"` carries the real exception.

**Search works, "similar" 404s** — that track has no embedding. `embed_tracks.py` skips rows
with any NULL feature; the slice report prints the coverage.

**Every "similar" 404s, and startup said the index cannot reconstruct** — the slice dropped
`ml_10d_embeddings` but the index cannot give vectors back. Rebuild the slice with
`--no-compact`, or the index without `--ivf`.

**Neighbours look unfiltered** (`"filter": ""`) — either the seed carries no region/sonic
family, or step 4 was skipped. The startup log says which: `tag id-sets ready (22 families …)`
versus `tag bitmaps absent`.

**`track_tag_family DISAGREES with engine.tagfamily`** — the family dicts moved after the tags
were built. Rebuild step 4. The engine refuses the terms rather than decoding 93.7M masks
against the wrong families.

**Previews are 20-byte blobs in the JSON** — `hydrate.init_preview()` did not find
`demo_meta.preview_template`. It runs from the engine's lifespan; if you are driving
`hydrate` directly in a script, call it first.
