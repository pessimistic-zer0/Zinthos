"""
train_genre_rf.py — Random Forest Baseline for Genre Classification
═══════════════════════════════════════════════════════════════════════
Trains a Random Forest on a stratified sample from labeled_tracks.csv
(124M rows, 13 audio features, 20 genre labels).

Produces:
  - genre_rf.joblib            → trained RF model
  - genre_scaler.joblib        → StandardScaler (reused by NN)
  - genre_label_encoder.joblib → LabelEncoder  (reused by NN)
  - confusion_matrix.png       → visual confusion matrix
  - feature_importances.png    → bar chart of feature importances

Usage:
  python train_genre_rf.py
"""

import time
import pandas as pd
import numpy as np
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler, LabelEncoder
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import classification_report, confusion_matrix
import joblib
import matplotlib
matplotlib.use("Agg")  # non-interactive backend (no display needed)
import matplotlib.pyplot as plt

# ═══════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════

CSV_PATH = "labeled_tracks.csv"
SAMPLE_CAP_PER_GENRE = 100_000   # max rows per genre (keeps memory ~3 GB)
CHUNK_SIZE = 1_000_000           # rows per CSV read chunk
TEST_SIZE = 0.2
RANDOM_STATE = 42

FEATURE_COLS = [
    "danceability", "energy", "key", "loudness", "mode",
    "speechiness", "acousticness", "instrumentalness",
    "liveness", "valence", "tempo", "duration_ms", "time_signature",
]

RF_PARAMS = {
    "n_estimators": 300,
    "max_depth": 20,
    "n_jobs": 4,              # -1 = all cores, but forks full data copies → OOM on 16 GB
    "random_state": RANDOM_STATE,
    "verbose": 1,
}


# ═══════════════════════════════════════════════════════════════════════
# STEP 1: LOAD & SAMPLE (chunked, stratified cap)
# ═══════════════════════════════════════════════════════════════════════

def load_stratified_sample():
    """
    Stream the CSV in chunks and collect up to SAMPLE_CAP_PER_GENRE rows
    per genre. This avoids loading the full 124M rows into memory.
    """
    print("══════════════════════════════════════════════════════")
    print("  STEP 1: Loading stratified sample from CSV")
    print("══════════════════════════════════════════════════════")
    
    genre_buckets = {}   # genre -> list of DataFrames
    genre_counts = {}    # genre -> total rows collected so far
    total_read = 0
    start = time.time()
    
    for chunk in pd.read_csv(CSV_PATH, chunksize=CHUNK_SIZE):
        total_read += len(chunk)
        
        for genre, group in chunk.groupby("genre"):
            current = genre_counts.get(genre, 0)
            remaining = SAMPLE_CAP_PER_GENRE - current
            
            if remaining <= 0:
                continue  # this genre is already full
            
            take = group.head(remaining)
            
            if genre not in genre_buckets:
                genre_buckets[genre] = []
            genre_buckets[genre].append(take)
            genre_counts[genre] = current + len(take)
        
        elapsed = time.time() - start
        print(f"  Read {total_read:>12,} rows | {elapsed:6.1f}s | "
              f"Genres capped: {sum(1 for c in genre_counts.values() if c >= SAMPLE_CAP_PER_GENRE)}/{len(genre_counts)}")
        
        # Early stop: if ALL genres are capped, no point reading more
        if all(c >= SAMPLE_CAP_PER_GENRE for c in genre_counts.values()):
            print("  ✓ All genres reached cap — stopping early.")
            break
    
    # Combine
    sampled_parts = []
    for genre, parts in genre_buckets.items():
        combined = pd.concat(parts, ignore_index=True)
        sampled_parts.append(combined)
    
    sample = pd.concat(sampled_parts, ignore_index=True)
    
    print(f"\n  Total sample size: {len(sample):,} rows")
    print(f"  Genre distribution:")
    for genre, count in sorted(genre_counts.items(), key=lambda x: -x[1]):
        print(f"    {genre:<15s} {count:>8,}")
    print()
    
    return sample


# ═══════════════════════════════════════════════════════════════════════
# STEP 2: PREPROCESS
# ═══════════════════════════════════════════════════════════════════════

def preprocess(sample):
    """
    Extract features and labels, encode labels, split, and scale.
    Returns X_train, X_test, y_train, y_test, scaler, label_encoder.
    """
    print("══════════════════════════════════════════════════════")
    print("  STEP 2: Preprocessing")
    print("══════════════════════════════════════════════════════")
    
    X = sample[FEATURE_COLS].values.astype(np.float32)
    y_raw = sample["genre"].values
    
    # Encode genre strings → integers
    le = LabelEncoder()
    y = le.fit_transform(y_raw)
    
    print(f"  Features shape: {X.shape}")
    print(f"  Genres encoded: {len(le.classes_)} classes")
    print(f"    {list(le.classes_)}")
    
    # Train/test split (stratified)
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=TEST_SIZE, stratify=y, random_state=RANDOM_STATE
    )
    print(f"  Train: {X_train.shape[0]:,} | Test: {X_test.shape[0]:,}")
    
    # Scale features — fit on train only
    scaler = StandardScaler()
    X_train = scaler.fit_transform(X_train)
    X_test = scaler.transform(X_test)
    
    print(f"  ✓ StandardScaler fit on training data\n")
    
    return X_train, X_test, y_train, y_test, scaler, le


# ═══════════════════════════════════════════════════════════════════════
# STEP 3: TRAIN
# ═══════════════════════════════════════════════════════════════════════

def train_rf(X_train, y_train):
    """Train a Random Forest classifier."""
    print("══════════════════════════════════════════════════════")
    print("  STEP 3: Training Random Forest")
    print(f"  Params: {RF_PARAMS}")
    print("══════════════════════════════════════════════════════")
    
    start = time.time()
    clf = RandomForestClassifier(**RF_PARAMS)
    clf.fit(X_train, y_train)
    elapsed = time.time() - start
    
    print(f"\n  ✓ Training complete in {elapsed:.1f}s ({elapsed/60:.1f} min)\n")
    return clf


# ═══════════════════════════════════════════════════════════════════════
# STEP 4: EVALUATE
# ═══════════════════════════════════════════════════════════════════════

def evaluate(clf, X_train, X_test, y_train, y_test, le):
    """Print classification report, plot confusion matrix & feature importances."""
    print("══════════════════════════════════════════════════════")
    print("  STEP 4: Evaluation")
    print("══════════════════════════════════════════════════════")
    
    # ── Accuracy ───────────────────────────────────────────────
    train_acc = clf.score(X_train, y_train)
    test_acc = clf.score(X_test, y_test)
    
    print(f"\n  Train accuracy: {train_acc:.4f}  ({train_acc*100:.1f}%)")
    print(f"  Test accuracy:  {test_acc:.4f}  ({test_acc*100:.1f}%)")
    
    if train_acc - test_acc > 0.15:
        print("  ⚠  Gap > 15% — possible overfitting. Consider lowering max_depth.")
    print()
    
    # ── Classification Report ──────────────────────────────────
    y_pred = clf.predict(X_test)
    report = classification_report(y_test, y_pred, target_names=le.classes_)
    print(report)
    
    # ── Confusion Matrix ───────────────────────────────────────
    cm = confusion_matrix(y_test, y_pred)
    
    fig, ax = plt.subplots(figsize=(14, 12))
    im = ax.imshow(cm, interpolation="nearest", cmap="Blues")
    ax.set_title("Genre Classification — Confusion Matrix", fontsize=16, pad=15)
    fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    
    genre_names = le.classes_
    tick_marks = np.arange(len(genre_names))
    ax.set_xticks(tick_marks)
    ax.set_xticklabels(genre_names, rotation=45, ha="right", fontsize=9)
    ax.set_yticks(tick_marks)
    ax.set_yticklabels(genre_names, fontsize=9)
    ax.set_xlabel("Predicted", fontsize=12)
    ax.set_ylabel("True", fontsize=12)
    
    # Annotate cells with counts
    thresh = cm.max() / 2
    for i in range(len(genre_names)):
        for j in range(len(genre_names)):
            val = cm[i, j]
            if val > 0:
                ax.text(j, i, f"{val:,}" if val > 999 else str(val),
                        ha="center", va="center", fontsize=6,
                        color="white" if val > thresh else "black")
    
    plt.tight_layout()
    plt.savefig("confusion_matrix.png", dpi=150)
    print("  ✓ Saved confusion_matrix.png")
    plt.close()
    
    # ── Feature Importances ────────────────────────────────────
    importances = clf.feature_importances_
    sorted_idx = np.argsort(importances)[::-1]
    
    print("\n  Feature Importances:")
    for i in sorted_idx:
        bar = "█" * int(importances[i] * 100)
        print(f"    {FEATURE_COLS[i]:<20s} {importances[i]:.4f}  {bar}")
    
    fig, ax = plt.subplots(figsize=(10, 6))
    y_pos = np.arange(len(FEATURE_COLS))
    ax.barh(y_pos, importances[sorted_idx], color="#4a90d9", edgecolor="none")
    ax.set_yticks(y_pos)
    ax.set_yticklabels([FEATURE_COLS[i] for i in sorted_idx], fontsize=10)
    ax.set_xlabel("Importance (Gini)", fontsize=12)
    ax.set_title("Random Forest — Feature Importances", fontsize=14, pad=10)
    ax.invert_yaxis()
    plt.tight_layout()
    plt.savefig("feature_importances.png", dpi=150)
    print("  ✓ Saved feature_importances.png\n")
    plt.close()
    
    return test_acc


# ═══════════════════════════════════════════════════════════════════════
# STEP 5: SAVE ARTIFACTS
# ═══════════════════════════════════════════════════════════════════════

def save_artifacts(clf, scaler, le):
    """Save model, scaler, and label encoder for reuse by the NN."""
    print("══════════════════════════════════════════════════════")
    print("  STEP 5: Saving Artifacts")
    print("══════════════════════════════════════════════════════")
    
    joblib.dump(clf, "genre_rf.joblib")
    print("  ✓ genre_rf.joblib")
    
    joblib.dump(scaler, "genre_scaler.joblib")
    print("  ✓ genre_scaler.joblib")
    
    joblib.dump(le, "genre_label_encoder.joblib")
    print("  ✓ genre_label_encoder.joblib")
    print()


# ═══════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════

def main():
    wall_start = time.time()
    
    print()
    print("╔══════════════════════════════════════════════════════╗")
    print("║  RANDOM FOREST — GENRE CLASSIFICATION BASELINE      ║")
    print("╚══════════════════════════════════════════════════════╝")
    print()
    
    # 1. Load data
    sample = load_stratified_sample()
    
    # 2. Preprocess
    X_train, X_test, y_train, y_test, scaler, le = preprocess(sample)
    
    # Free the DataFrame — we only need the numpy arrays now
    del sample
    
    # 3. Train
    clf = train_rf(X_train, y_train)
    
    # 4. Evaluate
    test_acc = evaluate(clf, X_train, X_test, y_train, y_test, le)
    
    # 5. Save
    save_artifacts(clf, scaler, le)
    
    # Summary
    wall_elapsed = time.time() - wall_start
    print("╔══════════════════════════════════════════════════════╗")
    print(f"║  DONE — Test Accuracy: {test_acc*100:.1f}%")
    print(f"║  Total wall time: {wall_elapsed:.0f}s ({wall_elapsed/60:.1f} min)")
    print("╚══════════════════════════════════════════════════════╝")


if __name__ == "__main__":
    main()
