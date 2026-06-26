# Backend Implementation Plan

Build order is **engine-first, vertical slices**: each milestone is shippable and testable on
its own. Read `CLAUDE.md` in this directory for the architecture rules this plan assumes.

Target box: 15 GB RAM / 6 GB VRAM (~9 GB resident = swap-thrash threshold). The mmap-based
design is what keeps us under that line.

---

## M0 — `track_search`: the filter + re-rank hot table  *(prerequisite for everything)*  ✅ DONE
> Built 254,819,856 rows (124.7M source_b-truth / 49.2M ml / 80.9M no-genre) + `idx_search`.
> F1 filters 0.4–1.9 ms, F6 point lookups 0.6 ms. Genre map extended 30→77 raw Source B genres.
> Builder: `backend/build_track_search.py` (memory-safe: batched + on-disk index sort).

A compact, denormalized, one-row-per-track table. It is the **filter source for F1/F2** and
the **re-rank source for F6**. Built once after the ETL (data is static — no write-sync).

### Schema
```sql
CREATE TABLE track_search (
    track_id     INTEGER PRIMARY KEY,   -- = tracks.track_id; F6 re-rank point lookups
    popularity   INTEGER NOT NULL DEFAULT 0,
    genre_id     INTEGER,               -- 0–19 canonical macro genre (NULL if unknown)
    genre_source INTEGER,               -- 0 = source_b ground truth, 1 = ml predicted, NULL
    release_year INTEGER,               -- parsed from tracks.release_date
    danceability INTEGER, energy INTEGER, valence INTEGER,
    acousticness INTEGER, instrumentalness INTEGER, speechiness INTEGER,
    liveness INTEGER, tempo INTEGER, loudness INTEGER
);  -- audio features scaled to ints: [0,1] → 0–1000; tempo = BPM; loudness = dB (rounded)

CREATE TABLE genres (genre_id INTEGER PRIMARY KEY, name TEXT NOT NULL);  -- 0–19 dimension

-- Build AFTER the bulk load (one index, leading on popularity → ORDER BY + early-stop):
CREATE INDEX idx_search ON track_search(
    popularity,                              -- leading: drives ordering + early termination
    energy, valence, danceability,           -- hottest filters first (locality only)
    acousticness, instrumentalness,
    tempo, loudness, speechiness, liveness,
    genre_id, release_year);
```

### Decisions baked in
- **Genre** = `COALESCE(consolidate(source_b_genres.genre), ml_genre_predictions.predicted_genre)`
  — ground truth preferred, both mapped into the same 20-genre macro space. `genre_source`
  records which one won (lets F6 trust truth over prediction later).
- **Features as scaled ints (0–1000)** — for RAM/page-cache locality, not disk. 0.001
  precision is plenty for mood filtering; raw lossless REALs stay in `track_audio_features`.
- **Full-covering index** — every filterable column is in `idx_search`, so F1 scans (including
  open-vocabulary LLM filters) never touch the base table. Order after `popularity` does NOT
  change the query plan (only marginal page locality); **presence** is what matters.
- `track_id` is the PK/rowid → appended to every index entry automatically → covered for free;
  also serves F6's point lookups by `track_id`.

### Build steps
1. `INSERT INTO track_search SELECT ...` joining `tracks` + `track_audio_features`
   `LEFT JOIN source_b_genres LEFT JOIN ml_genre_predictions`, casting features to ints.
2. Fill `genre_id` via the **shared Python consolidation map** (the same map the ML layer
   uses) — likely a follow-up `UPDATE` pass, since consolidation is cleaner in Python.
3. `CREATE INDEX idx_search` last.
4. Sanity-check counts vs `model_training/CLAUDE.md` (254.82M featured tracks).

**Done when:** the popularity-led `WHERE … ORDER BY popularity DESC LIMIT 300` returns in
single-digit ms for a common filter, and never hits the base table (verify with `EXPLAIN
QUERY PLAN` → covering index, no table access).

---

## M1 — Engine skeleton  ✅ DONE
> `backend/engine/{config,db,index,hydrate,app,main}.py`. `sonic serve` boots, mmaps the
> 254.8M-vector index, warms in ~14s, serves `/health` + `/track/{id}` (21 ms). Idle-shutdown
> + auth seam + CORS in place. (TUI spawn-or-connect deferred to M6.)


FastAPI + uvicorn, single worker. Startup loads the shared singletons **once**.

- `faiss.read_index(path, faiss.IO_FLAG_MMAP)`; set `nprobe = 64`.
- Open `master.db` read-only, WAL, with `PRAGMA mmap_size`; **one connection per thread**.
- App state holds: index, a connection factory, the LRU caches, the rules vocabulary.
- **Config-driven** host/port/db-path/index-path (env or config file) — never hardcode.
- Leave an empty **pass-through auth-middleware seam** and a **CORS allowlist** (localhost).
- `GET /health` — liveness + triggers startup **warmup** (fire ~5–10 dummy FAISS searches so
  the first real query isn't cold).
- `GET /track/{id}` — the full two-tier hydration query (the reusable display join).
- **Lifecycle:** `sonic serve` runs foreground (Ctrl-C quits); implement the **idle
  auto-shutdown** timer (~15 min of no requests → exit).

**Done when:** `sonic serve` starts in <1 s (mmap, not load), `/health` warms, `/track/{id}`
returns hydrated metadata.

---

## M2 — F6 Similar (vector)  `GET /search/similar/{track_id}?k=20`  ✅ DONE
> `backend/engine/similar.py`: seed blob → FAISS top-100 → 5-term re-rank via track_search →
> hydrate top-k. Warm ~40–60 ms (FAISS ~54 ms), cold ~240 ms. Validated over HTTP.
> Lever for later: nprobe 64→32 to ~halve FAISS time (recall held flat at 64).


1. `SELECT vector_blob FROM ml_10d_embeddings WHERE track_id = ?` → decode 10×f32 → L2-normalize.
2. `index.search(q, 100)` (IndexIDMap2 → returns `track_id`s directly).
3. Re-rank the 100 via one `track_search WHERE track_id IN (...)` batch, scoring:
   `0.50·embedding_sim + 0.20·genre_match + 0.15·tempo_prox + 0.10·popularity + 0.05·era_prox`
   (needs the seed's own row for the proximity terms) → keep top-K.
4. Two-tier hydrate the final K (titles/artists/art/preview).
5. Cache recent results (LRU).

**Done when:** warm latency 15–50 ms; results are sane for known tracks; FAISS step <100 ms.

---

## M3 — F1 Semantic, rules path  `GET /search?q=...`  ✅ DONE
> `rules.py` (mood+genre vocab, bigram-aware, coverage) → `filters.py` (whitelisted SQL
> builder, the shared trust boundary) → `search.py`. 3–28 ms. Sub-threshold queries return
> `llm_fallback_recommended` (M4 hook). Validated: "sad rock songs for studying" → Pink Floyd.


- Rules parser: tokenize → match meaningful words against the **rules vocabulary** → compute
  coverage. (Vocabulary table below.)
- Translate matched filters → parameterized `track_search` query:
  `... WHERE <filters> ORDER BY popularity DESC LIMIT 300` → two-tier hydrate.
- (No LLM yet — sub-50%-coverage queries return rule-only results for now.)

**Done when:** "sad", "chill workout", "acoustic" etc. return good pools in <50 ms.

---

## M4 — F1 LLM fallback + caching  ✅ DONE
> `llm.py`: provider-agnostic (Groq/Gemini/OpenAI-compatible, **stdlib REST, no SDK**),
> chosen by `SONIC_LLM_PROVIDER`. Output whitelisted via `filters.py` (injection-safe),
> success-only LRU cache, graceful degrade to rules on any failure. Live-tested on Groq
> `llama-3.1-8b-instant` (273–606 ms): "warm summer sunset" → Chappell Roan / bôa.
> Gotcha fixed: Groq is behind Cloudflare → must send a real `User-Agent` (else 403/1010).


- When coverage < 50%: call **Claude Haiku** with a **static, prompt-cached** instruction to
  emit a **constrained JSON filter spec** (keys = whitelisted feature columns; ops `<`/`>`/`between`).
- **Safety:** the model never emits SQL. Whitelist keys + ops, bounds-check values, then bind
  as **parameters**. Reject anything off-list.
- **Cache** the filter spec keyed by the normalized query (LRU) → repeat queries skip the call.
- Same downstream `track_search` scan + hydration as M3.

**Done when:** an abstract query ("rainy 3am drive") returns sensible tracks; cache-miss
≤ ~3 s (LLM-bound), cache-hit ≈ instant; malformed/hostile LLM output is rejected safely.

---

## M5 — F2 Playlist & F3 Artist  ✅ DONE
> `playlist.py` (F1 pool → 70/30 popular/deep → greedy transition-ordered walk, Camelot
> key_penalty from track_audio_features) + `artist.py` (catalog aggregation: avg features,
> dominant genre, top tracks). 14–22 ms playlist; artist 151 ms warm. Routes wired (POST
> /playlist via pydantic, GET /artist/{name}).


- **F2** `POST /playlist`: run the F1 retrieve for a candidate pool (70% popular / 30% deep
  cuts via a low-popularity window), then order to minimize
  `3·tempo_diff + 2·energy_diff + 1.5·key_penalty + 1·valence_diff` (greedy nearest-neighbor
  over ~50 tracks is fine), then hydrate.
- **F3** `GET /artist/{name}`: `idx_artists_name` → `track_artists` reverse-join → aggregate
  features, dominant genre, top tracks. F4 (`/track/{id}`) gains an embedded F6 "similar" block.

**Done when:** a playlist flows smoothly tempo/energy-wise; artist pages aggregate correctly.

---

## M6 — Rust TUI (`ratatui`)  ⏳ built, pending interactive verify
> `tui/` — ratatui + ureq (no-TLS) thin HTTP client, compiles clean. Menu →
> { Query (search), Scan local library (F7), Transition Playlist }. Results → track detail;
> `s` similar; `--vim` adds hjkl; `--spawn` launches the engine (kills on exit). Rust devshell
> in flake.nix (`nix develop .#tui`). Needs a human to drive the UI.
>
> **Transition Playlist** (menu option 3): `Mode::Playlist` → describe a mood → `POST /playlist`
> → numbered, transition-ordered list (reuses the F2 engine; single-mood smooth ordering). The
> A→B "mood arc" (e.g. sad→hopeful) is intentionally NOT built — deferred, see Deferred list.


- Thin HTTP client to the configured API URL. Links **no** FAISS/SQLite.
- **Spawn-or-connect** lifecycle: spawn the engine child if the URL is dead (kill on exit),
  else connect to the running one.
- Search box, results list, track detail, "similar" action, queue. Arrow-keys + enter by
  default; **optional Vim (hjkl)** behind a config flag.

**Done when:** keyboard-driven search/similar/detail works end-to-end; engine lifecycle is
clean (no orphaned process after quit).

---

## M7 — Web client (lean)

- Svelte or vanilla; runs in the browser, hits the same JSON API (used with `sonic serve`).
- Album art, mouse-driven search, track/artist detail.

**Done when:** feature parity with the TUI for search/similar/detail in a browser.

---

## F7 — Local Library Scan  ✅ DONE  → see `local_library.md`

> Scan a folder → identify catalog tracks by tags (ISRC + fuzzy title/artist) → taste
> recommendations + genre/era breakdown. Identity matching, NOT audio analysis (Source A's 13
> features aren't reproducible from a raw file). **92.6%** coverage on a real 726-file library.

- `POST /library/scan` and `POST /library/diagnose`; engine `library.py` + shared normalizer
  `textnorm.py`; fuzzy sidecar `track_match.db` (22 GB, `build_track_match.py`, ATTACHed as `m`).
- TUI `Mode::Library` + the `diagnose` bin (`cargo run --bin diagnose -- <dir> [out.json]`).
- **Open follow-ups** (detailed in `local_library.md` §7): multi-ISRC tag split (cheap),
  rec-quality clustering, owned-dupe exclusion, all-artists sidecar rebuild, M7.3 acoustic
  fingerprinting (Chromaprint/AcoustID/MusicBrainz), romanization alias table.

---

## The rules vocabulary (M3 seed — extensible)

Filter values are on the scaled-int scale (`[0,1] → 0–1000`).

| Word(s) | Filters |
|---|---|
| sad / melancholy | `valence<300, energy<500` |
| happy / cheerful | `valence>600` |
| energetic / hype | `energy>700` |
| chill / relaxing / calm | `energy<400` |
| aggressive / intense | `energy>800, loudness>-5` |
| dance / party | `danceability>700, energy>600` |
| upbeat | `valence>600, energy>600, danceability>600` |
| dark / moody | `valence<400, energy<600` |
| mellow / soft | `energy<400, loudness<-8` |
| acoustic | `acousticness>600` |
| lo-fi | `acousticness>500, energy<400` |
| instrumental | `instrumentalness>500` |
| studying / focus | `instrumentalness>500, energy<500, speechiness<100` |
| workout / gym | `energy>800, tempo>120, danceability>600` |
| sleep / ambient | `energy<200, instrumentalness>600, acousticness>500` |
| live | `liveness>800` |
| fast / slow | `tempo>140` / `tempo<80` |
| loud / quiet | `loudness>-5` / `loudness<-15` |

Frequency drives the index order: `energy` ≫ `valence` > `danceability`/`acousticness`/
`instrumentalness` > `tempo`/`loudness` > `speechiness`/`liveness`.

---

## Deferred (don't build until needed)
- **Mood-arc playlist (A→B journey)** — current Transition Playlist does single-mood smooth
  ordering. A real "sad → hopeful" arc needs a valence/energy gradient + path-ordered
  selection across the playlist (new engine path + a 2-mood TUI input). Chosen NOT to build now.
- Web framework final pick (M7).
- IVFFlat rebuild — only if recall@10 (94.3% now) feels weak; `build_faiss.py --flat`, ~10 GB.
- Hosting/auth/rate-limiting/accounts + data-redistribution legal — only if going public.
- Mood classifier (PRD Phase 2).
