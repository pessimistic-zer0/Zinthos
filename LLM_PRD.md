# Distilled PRD: Sonic Something (LLM-Optimized Version)

## 1. Project Overview & Tech Stack
**Sonic Something** is a music search/discovery app allowing users to search by "sound and feel" across ~255M Source A tracks.
*   **Environment**: NixOS. We are using a Nix devshell. Always read `flake.nix` to understand the available dependencies.
*   **Data Pipeline/ETL**: C++, SQLite (Handles ~266GB source data: source_a.sqlite3 117GB, audio_features 39GB, source_b 110GB to build `master.db`)
*   **ML Pipeline**: Python, LightGBM (Genre), PyTorch (Embeddings/Autoencoder), FAISS (Vector search)
*   **Backend API**: Python, FastAPI
*   **Memory constraints**: 16GB RAM limit. Processes must handle 255M rows out-of-core or batched.

## 2. Core Architecture
*   **Master DB (`master.db`)**: 
    *   We have moved to a highly normalized, 10-table schema to improve data integrity (e.g., `albums`, `tracks`, `track_mappings`, `track_audio_features`, `artists`, `track_artists`, `artist_genres`, `source_b_genres`, `ml_genre_predictions`, `ml_10d_embeddings`).
    *   **Crucial Reference**: See `database/schema.sql` for the exact DDL, foreign key relationships, and performance indexes. *Do not assume the old flat schema.*
*   **Vector DB (`embeddings.faiss`)**: 10D FAISS IVFPQ index for nearest-neighbor similarity search.

## 3. Machine Learning Systems
1.  **Genre Prediction (LightGBM)**:
    *   Predicts genre for ~113.33M unlabeled tracks based on 13 audio features.
    *   Trained on 141.67M Source B-labeled tracks (20 consolidated genres).
    *   *Constraint*: Do NOT use Neural Networks (they failed to beat the 45.9% Random Forest baseline). Use LightGBM (target ≥50%).
2.  **Embeddings (Supervised Autoencoder + FAISS)**:
    *   Compresses 13 features → 10D bottleneck.
    *   *Supervised constraint*: Bottleneck has 2 heads: Reconstruction (MSE) + Genre Classification (CrossEntropy). Forces same-genre tracks to cluster together.
    *   FAISS: `IndexIVFPQ`, `nlist=16000`, 10 sub-quantizers (8-bit).
3.  **Mood Classifier (Deferred/Phase 2)**:
    *   Multi-label classification using `nn.BCEWithLogitsLoss()` and Sigmoid activation (NOT Softmax).

## 4. API & Feature Specifications (FastAPI)
1.  **F1: Semantic Search (`GET /search`)**:
    *   Rules-based keyword parser maps words to audio features (e.g., "sad" -> `valence < 0.3, energy < 0.5`). 
    *   *LLM Fallback*: If <50% of query words match rules, use Claude/GPT to output a JSON of feature filters.
2.  **F2: Playlist Generator (`POST /playlist`)**:
    *   Select candidates (70% popular / 30% deep cuts) matching filters.
    *   *Transition Optimization*: Order tracks to minimize `transition_score = 3*(tempo_diff) + 2*(energy_diff) + 1.5*(key_penalty) + 1*(valence_diff)`.
3.  **F3/F4: Artist & Track Details (`GET /artist/{name}`, `GET /track/{id}`)**:
    *   Artist: Returns avg features, dominant genre, top tracks.
    *   Track: Returns all metadata + similar tracks.
4.  **F6: Similar Tracks (`GET /search/similar/{track_id}`)**:
    *   *Two-Stage Retrieval*: FAISS fetches top 100 fast. Memory re-ranker picks top 20 based on: `0.5*embedding_sim + 0.2*genre_match + 0.15*tempo_prox + 0.1*popularity + 0.05*era_prox`.

## 5. Development Guidelines for LLMs
*   **C++ ETL**: Prioritize streaming and batching to avoid OOM crashes on 255M rows. Compile with `-O3`.
*   **Python ML**: Always use `joblib` for persisting models/scalers. Use `batched` updates for SQLite inference.
*   **Python API**: All endpoints must respond under <2s (except LLM fallback <5s). FAISS similarity must take <100ms.

## 6. Current Status & Checklist (Hard Reset)
**Note**: We are currently undergoing a **hard reset** of the data pipeline. We need to rebuild the master database from scratch.

### ✅ Achieved (Pre-Reset / Data Gathering)
- [x] Raw data successfully gathered (`source_a.sqlite3` 117GB, `source_a_audio_features.sqlite3` 39GB).
- [x] Source B genre data acquired (`source_b.csv` 110GB, yielding 141.67M labeled rows).
- [x] Random Forest baseline trained (achieved 45.9% accuracy) on an old database.
- [x] LLM-optimized PRD and hierarchical `CLAUDE.md` context system established.

### ⏳ To Be Achieved (Current Focus)
- [ ] **Data Pipeline (C++)**: Rewrite `master_db_builder.cpp` to combine all 266GB of raw data into a new, optimized `master.db`.
- [ ] **Data Pipeline (C++)**: Generate proper indexes on the new `master.db` without OOM errors.
- [ ] **Machine Learning (Python)**: Train LightGBM genre classifier on the 141.67M labeled tracks (Target: ≥50%).
- [ ] **Machine Learning (Python)**: Predict genres for the remaining ~113.33M tracks and write them to `master.db`.
- [ ] **Machine Learning (PyTorch)**: Train supervised autoencoder and generate 10D embeddings for all 255M tracks.
- [ ] **Vector Search (FAISS)**: Build the `embeddings.faiss` IVFPQ index.
- [ ] **Backend API (FastAPI)**: Implement semantic search, playlist generation, and similarity endpoints.
- [ ] **LLM Integration**: Implement LLM query fallback for abstract natural language search.
