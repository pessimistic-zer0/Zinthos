---
title: Zinthos
emoji: 🎧
colorFrom: purple
colorTo: indigo
sdk: gradio
sdk_version: 5.50.0
app_file: app.py
pinned: false
license: other
short_description: Search 255M tracks by how they sound and feel
---

# Zinthos

A music search engine over a catalogue of **~255 million tracks**. You describe how something
should *sound* — `rainy 3am drive, warm bass, nothing cheerful` — and it finds tracks that
match the feel, not the words.

**This Space runs the real engine against a slice of the catalogue.** The full `master.db` is
162 GB and the FAISS index 9 GB; neither fits a free host. The slice keeps the most popular
few million tracks, with every genre and regional family still represented, and every track
id unchanged — so the code paths, the ranking, and the results are the production ones. The
banner at the foot of the page states the live count.

| | |
|---|---|
| Portal | `/` — the React front end |
| API | `/api` — the FastAPI engine (`/api/health`, `/api/search`, `/api/search/similar/{id}`, …) |
| Fallback panel | `/gradio` — a plain search box |

## What it does

- **Vibe search** — a rules parser maps words to audio-feature filters, with an LLM fallback
  for anything the rules do not cover.
- **Similar by name** — name a track, pick the right catalogue copy, and get its nearest
  neighbours from a learned 10-D embedding space, re-ranked on tempo, era, genre and the
  artist's stated region/sonic family.
- **Playlist builder** — the same filters, then sequenced to minimise the jump between
  consecutive tracks (tempo, energy, Camelot key, valence).
- **Local library** — paste what you own; it matches by ISRC then fuzzily, maps your taste,
  and recommends from there.

## How it was built

A 266 GB C++ ETL into a normalised SQLite catalogue, a LightGBM genre classifier over 255M
rows, a supervised autoencoder compressing 13 audio features to a 10-D bottleneck, and a
FAISS index over the result — all on a single 15 GB-RAM laptop.

No GPU is used here. The workload is SQLite and a brute-force FAISS scan, both CPU.

## Data

Built from large, publicly-available music datasets compiled and distributed by third
parties. Nothing is scraped, and the underlying data is not redistributed — this Space
serves derived features, ids and metadata only.
