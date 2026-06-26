# F7 — Local Library Scan

> "Scan your Local Library and Do Something" — point Zinthos at a folder of audio files; it
> identifies which catalog tracks they are, profiles your taste, and recommends more.
>
> Status: **shipped & working end-to-end.** Coverage on a real ~726-file library: **92.6%**
> (ISRC 77% → +fuzzy 92.6%). Remaining misses are a genuine ceiling (see §6).

This is a distinct feature from the deferred "M7 — Web client" in `implementation_plan.md`.
Code comments/log labels use phase tags **M7.1 / M7.2 / M7.2.1**; they map to §3 below.

---

## 1. The core constraint (why "scan" = identity matching, not audio analysis)

The whole ML stack (GBDT genre + SAE 10-D embeddings + FAISS) is trained on Source A's **13
proprietary audio features** (`danceability`, `energy`, `valence`, …). Those are **not**
reproducible from a raw MP3/FLAC — Source A computed them with their own models. So we cannot
"analyse a local file and embed it." Instead we **identify** which catalog track each file is
(by tags), then reuse everything already built (embeddings → recommendations, genres → taste
breakdown).

## 2. Architecture (respects the backend rules)

```
TUI (Rust)                                   Engine (Python / FastAPI)
─ walks the folder (walkdir)                  POST /library/scan
─ reads tags (lofty): title, artist,  ──JSON──▶  1. match()      → track_ids  (ISRC, then fuzzy)
  album, duration, ISRC                          2. recommend()  → taste centroid → FAISS, excl. owned
─ renders Library screen          ◀──JSON──      3. breakdown()  → genre + era histograms
```

- **Only small JSON crosses the wire**; audio never leaves the client (works the same if the
  engine ever goes remote). Clients never open SQLite/FAISS — matching stays server-side.
- TUI screen: `Mode::Library` (header counts + taste-breakdown bars + recommendations list
  that reuses the normal detail/similar navigation).

## 3. What shipped (phases)

| Phase | What | Key files |
|---|---|---|
| **M7.1** | ISRC exact match (uses existing `idx_tracks_isrc`, no new index) + taste recs + breakdown; full TUI wiring | `engine/library.py`, `engine/app.py`, `tui/src/main.rs` |
| **M7.2** | Fuzzy title+artist via a **sidecar** `track_match.db` (normalized-key table); engine ATTACHes it read-only as `m` iff present | `build_track_match.py`, `engine/textnorm.py`, `engine/db.py`, `engine/config.py` |
| **M7.2.1** | Query-side **candidate-key** matching: probe every credited artist + title variants (no rebuild) | `engine/textnorm.py` (`candidate_keys`), `engine/library.py` |
| **Diag** | Per-file match report tool | `engine/library.py` (`diagnose`), `engine/app.py`, `tui/src/bin/diagnose.rs` |

### Endpoints
- `POST /library/scan` — body `{tracks:[{title,artist,album,duration_ms,isrc}], size}` →
  `{total, matched, unmatched, methods:{isrc,fuzzy}, breakdown, recommendations}`.
- `POST /library/diagnose` — same body → per-file `{input, method, track_id, catalog_title,
  catalog_artists}` (no recs). For coverage triage.

### The match pipeline (`engine/library.py::match`)
1. **ISRC** — normalize each tag's ISRC (strip punctuation, require exactly 12 alnum), batch
   `WHERE isrc IN (…)`, pick most-popular on collision. `method="isrc"`.
2. **Fuzzy** (if `track_match.db` present) — for each still-unmatched file,
   `textnorm.candidate_keys(title, artist)` yields keys for **every** artist the tag lists
   (the sidecar stores only one arbitrary artist per track — `track_artists.artist_position`
   is ascending-artist_id, **not** credited order, so we must try all) **×** title variants
   (plain + `explicit`/`clean` stripped). Look them all up; take the most-popular hit per
   file. `method="fuzzy"`.

### The sidecar (`track_match.db`)
- One row per track: `(norm_key TEXT, track_id, popularity)`, `norm_key = "<norm title>|<norm
  primary-artist>"`. Index `idx_track_match_key` on `norm_key`.
- **Shared normalizer** `engine/textnorm.py` is LOAD-BEARING — build-time and query-time must
  compute the identical key or nothing matches. Preserves non-ASCII (CJK/accents); folds
  case, `(brackets)`, `feat.`, `- Remaster/Live/…` tails, punctuation.
- ~255.9M rows, **22 GB**, builds in ~43 min. Memory-safe (streamed cursor, 200k insert
  sub-batches, `journal_mode=OFF`, index sort spills to disk). RAM peak ~330 MB.
- **Rebuild:** `cd backend && python build_track_match.py`  (`--smoke` for a small id range).

## 4. Running it

```bash
# Engine (auto-attaches track_match.db if it exists)
cd backend && python -m engine.main

# In the TUI: menu → "Scan your Local Library" → type a folder path → Enter
cd tui && cargo run            # or: cargo run -- --spawn   (auto-starts the engine)
```

## 5. The diagnostic tool

```bash
# engine must be running
cd tui && cargo run --bin diagnose -- ~/Music report.json
```
Writes a JSON report (`*.json` is gitignored) with one row per file
`{path,title,artist,isrc,method,track_id,catalog_title,catalog_artists}`, **sorted
misses-first then fuzzy then isrc** so the rows worth eyeballing are on top.

```bash
jq '.tracks[] | select(.method=="none") | .path' report.json   # all misses
jq '.tracks[] | select(.method=="fuzzy")' report.json          # sanity-check fuzzy hits
```

**Probing the catalog by hand** (why a song didn't match): query `track_match.db` with an
**indexed range**, NOT `LIKE`:
```python
# from backend/ so `import engine.textnorm` resolves
nt = _norm(title); rows = db.execute(
  "SELECT norm_key,popularity FROM track_match WHERE norm_key>=? AND norm_key<? "
  "ORDER BY popularity DESC LIMIT 6", (nt+'|', nt+'|￿'))
```
`LIKE 'x%'` can't use the index (case-insensitive LIKE) → full scan of 256M rows → hangs.

## 6. Coverage & the residual ceiling

| Stage | Matched / 726 | % |
|---|---|---|
| ISRC only | 559 | 77.0 |
| + fuzzy (full key) | 656 | 90.4 |
| + first-artist | 664 | 91.5 |
| + candidate keys (all artists + edition strip) | **674** | **92.6** |

The remaining ~54 are **not bugs** — tag matching can't reach them:
1. **Romanization** — catalog stores CJK titles/artists in native script or different romaji
   (Fly-Day Chinatown = `泰葉`; Cruel Angel's Thesis / Fukashigi only as covers).
2. **Genuinely absent** — post-Sept-2025 releases (catalog cutoff), NCS/vocaloid
   (DEAD/Unknown Brain), YouTube/sped-up/mashup rips, niche regional indie.
3. **Artist-name spelling variants** — Miserlou "…and the Del-Tones" vs catalog "Dick Dale".

---

## 7. Follow-up fixes (deferred — pick up later)

Ordered cheapest → heaviest. Each notes whether it needs a sidecar rebuild.

### 7a. Multi-ISRC tags  *(cheap, no rebuild, high confidence)*
Some tags carry several ISRCs: `"USRC10200345;USRC16305834"`, `"CDNOW95 / 0889853691426"`.
`normalize_isrc` requires exactly 12 chars, so these are rejected and fall to fuzzy.
**Fix:** in `library.py`, split the tag's `isrc` on `;`/`/`/whitespace, normalize each to a
candidate, and match if **any** resolves. Reclaims a handful as exact ISRC hits (more precise
than fuzzy). Touch: `normalize_isrc` → return a list, `match()` ISRC phase → try each.

### 7b. Recommendation quality — cluster the taste centroid  *(no rebuild)*
`recommend()` mean-pools ALL matched embeddings into one centroid; on a diverse library that
lands in a muddy middle and FAISS returns obscure (pop-0) neighbours. **Fix:** k-means the
matched embeddings into a few centroids, FAISS-search each, merge. Or weight by genre/recency.

### 7c. Owned-duplicate exclusion  *(no rebuild)*
Recs only exclude owned *track_ids*; a different-id catalog copy of a song you own can still
appear. **Fix:** also exclude by normalized (title, artists) of the owned set during dedupe.

### 7d. All-artists sidecar  *(REBUILD)*
7a/M7.2.1 work because the tag usually lists the artist the sidecar happened to store. The
robust fix: build **one row per (track, EACH artist)**, not just one arbitrary artist.
Then a single-artist tag matches even when it names the "other" collaborator. Cost: ~348M
rows (~+36%, ~30 GB) — drive from `track_artists` joined to `artists` instead of `min()`.
Largely subsumes the query-side artist splitting.

### 7e. Acoustic fingerprinting (M7.3)  *(heaviest — the only path to §6.1/§6.3)*
For untagged/mislabeled/romanized files: Chromaprint (`fpcalc`, nixpkgs `chromaprint`) on the
client → fingerprint → **AcoustID** API → MusicBrainz Recording ID → **ISRC** → catalog.
- Offline ISRC resolution preferred (MusicBrainz **ISRC dump**, modest size) over the
  rate-limited MB API.
- Drops into `match()` as an alternate ISRC source; nothing else changes.
- Will NOT recover §6.2 (tracks genuinely not in the Source A/Source B catalog).

### 7f. Romanization alias table  *(data-gathering)*
A title/artist alias map (romaji ↔ native script) would catch the CJK misses without audio.
Source from MusicBrainz aliases. Lower ROI than 7e; listed for completeness.
