"""
train_genre_gbdt.py — LightGBM Genre Classifier
═══════════════════════════════════════════════════
Replaces the neural network approach for genre classification.

Why GBDT over Neural Networks:
  - 13 audio features is low-dimensional tabular data
  - GBDTs (XGBoost/LightGBM) are the gold standard for this regime
  - NNs plateaued at ~41% (below RF's 45.9%) after 3 architecture iterations
  - GBDTs handle feature interactions natively (no engineering needed)
  - Trains in ~2 min vs ~30+ min for NN

Reuses RF artifacts: genre_scaler.joblib, genre_label_encoder.joblib

Produces:
  - genre_gbdt.txt            → trained LightGBM model (text format)
  - genre_gbdt_confusion.png  → visual confusion matrix
  - genre_gbdt_importances.png → feature importances
  - genre_gbdt_curves.png     → training curves (log-loss)

Usage: python train_genre_gbdt.py
"""

import time, sys, os
import pandas as pd
import numpy as np
import lightgbm as lgb
from sklearn.model_selection import train_test_split
from sklearn.metrics import (
    classification_report, confusion_matrix, log_loss, accuracy_score
)
import joblib
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


# ── Config ────────────────────────────────────────────────────────────
CSV_PATH = "labeled_tracks.csv"
SAMPLE_CAP_PER_GENRE = 100_000
CHUNK_SIZE = 1_000_000
TEST_SIZE = 0.2
RANDOM_STATE = 42

FEATURE_COLS = [
    "danceability", "energy", "key", "loudness", "mode",
    "speechiness", "acousticness", "instrumentalness",
    "liveness", "valence", "tempo", "duration_ms", "time_signature",
]

# ── LightGBM Hyperparameters ─────────────────────────────────────────
# Tuned for 20-class genre classification on 13 audio features.
#
# Key design choices:
#   - num_leaves=127: complex enough for 20-class interactions
#   - max_depth=12: controls overfitting (RF used max_depth=20 and
#     overfit by 28%; limiting depth is critical)
#   - min_data_in_leaf=100: prevents leaves from memorizing tiny groups
#   - feature_fraction=0.8 + bagging: RF-style randomization to reduce
#     variance, while boosting handles the bias reduction
#   - learning_rate=0.05 with 2000 rounds + early stopping: slow
#     learning with many rounds generalizes better than fast/few
#   - lambda_l1 + lambda_l2: light regularization
LGBM_PARAMS = {
    "objective": "multiclass",
    "num_class": 20,
    "metric": "multi_logloss",
    "boosting_type": "gbdt",
    "num_leaves": 127,
    "max_depth": 12,
    "learning_rate": 0.05,
    "n_estimators": 2000,
    "min_data_in_leaf": 100,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 5,
    "lambda_l1": 0.1,
    "lambda_l2": 0.1,
    "verbose": -1,
    "n_jobs": 4,
    "random_state": RANDOM_STATE,
}

EARLY_STOPPING_ROUNDS = 50


# ── Step 1: Load ──────────────────────────────────────────────────────
def load_stratified_sample():
    print("══════════════════════════════════════════════════════")
    print("  STEP 1: Loading stratified sample from CSV")
    print("══════════════════════════════════════════════════════")
    genre_buckets, genre_counts, total_read = {}, {}, 0
    start = time.time()
    for chunk in pd.read_csv(CSV_PATH, chunksize=CHUNK_SIZE):
        total_read += len(chunk)
        for genre, group in chunk.groupby("genre"):
            current = genre_counts.get(genre, 0)
            remaining = SAMPLE_CAP_PER_GENRE - current
            if remaining <= 0:
                continue
            take = group.head(remaining)
            genre_buckets.setdefault(genre, []).append(take)
            genre_counts[genre] = current + len(take)
        elapsed = time.time() - start
        capped = sum(1 for c in genre_counts.values() if c >= SAMPLE_CAP_PER_GENRE)
        print(f"  Read {total_read:>12,} rows | {elapsed:6.1f}s | "
              f"Capped: {capped}/{len(genre_counts)}")
        if all(c >= SAMPLE_CAP_PER_GENRE for c in genre_counts.values()):
            print("  ✓ All genres capped.")
            break
    sample = pd.concat([pd.concat(p, ignore_index=True)
                        for p in genre_buckets.values()], ignore_index=True)
    print(f"\n  Total: {len(sample):,} rows, {len(genre_counts)} genres\n")
    return sample


# ── Step 2: Preprocess ────────────────────────────────────────────────
def preprocess(sample):
    print("══════════════════════════════════════════════════════")
    print("  STEP 2: Preprocessing (reusing RF artifacts)")
    print("══════════════════════════════════════════════════════")
    scaler = joblib.load("genre_scaler.joblib")
    le = joblib.load("genre_label_encoder.joblib")
    print(f"  ✓ Loaded scaler + encoder ({len(le.classes_)} genres)")

    X = sample[FEATURE_COLS].values.astype(np.float32)
    y = le.transform(sample["genre"].values)

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=TEST_SIZE, stratify=y, random_state=RANDOM_STATE
    )
    X_train = scaler.transform(X_train).astype(np.float32)
    X_test = scaler.transform(X_test).astype(np.float32)
    print(f"  Train: {X_train.shape[0]:,} | Test: {X_test.shape[0]:,}")
    print(f"  Features: {X_train.shape[1]}\n")
    return X_train, X_test, y_train, y_test, le


# ── Step 3: Train ────────────────────────────────────────────────────
def train_gbdt(X_train, y_train, X_test, y_test, n_genres):
    print("══════════════════════════════════════════════════════")
    print("  STEP 3: Training LightGBM (GBDT)")
    print("══════════════════════════════════════════════════════")

    params = LGBM_PARAMS.copy()
    params["num_class"] = n_genres

    print(f"\n  Key hyperparameters:")
    print(f"    num_leaves:      {params['num_leaves']}")
    print(f"    max_depth:       {params['max_depth']}")
    print(f"    learning_rate:   {params['learning_rate']}")
    print(f"    n_estimators:    {params['n_estimators']} (max, early stopping @ {EARLY_STOPPING_ROUNDS})")
    print(f"    min_data_leaf:   {params['min_data_in_leaf']}")
    print(f"    feature_frac:    {params['feature_fraction']}")
    print(f"    bagging_frac:    {params['bagging_fraction']}")
    print(f"    L1/L2 reg:       {params['lambda_l1']} / {params['lambda_l2']}")
    print(f"    n_jobs:          {params['n_jobs']}")
    print()

    # Create LightGBM datasets
    train_data = lgb.Dataset(X_train, label=y_train, feature_name=FEATURE_COLS)
    valid_data = lgb.Dataset(X_test, label=y_test, feature_name=FEATURE_COLS,
                             reference=train_data)

    # Track eval results for plotting
    evals_result = {}

    callbacks = [
        lgb.early_stopping(stopping_rounds=EARLY_STOPPING_ROUNDS),
        lgb.log_evaluation(period=50),
        lgb.record_evaluation(evals_result),
    ]

    start = time.time()
    model = lgb.train(
        params,
        train_data,
        num_boost_round=params["n_estimators"],
        valid_sets=[train_data, valid_data],
        valid_names=["train", "valid"],
        callbacks=callbacks,
    )
    elapsed = time.time() - start

    best_iter = model.best_iteration
    print(f"\n  ✓ Training complete in {elapsed:.1f}s ({elapsed/60:.1f} min)")
    print(f"  ✓ Best iteration: {best_iter}")
    print(f"  ✓ Best valid logloss: {model.best_score['valid']['multi_logloss']:.4f}")
    print()

    return model, evals_result


# ── Step 4: Evaluate ─────────────────────────────────────────────────
def evaluate(model, X_train, X_test, y_train, y_test, le, evals_result):
    print("══════════════════════════════════════════════════════")
    print("  STEP 4: Evaluation")
    print("══════════════════════════════════════════════════════")

    y_pred_proba = model.predict(X_test)
    y_pred = y_pred_proba.argmax(axis=1)

    test_acc = accuracy_score(y_test, y_pred)
    test_logloss = log_loss(y_test, y_pred_proba)

    # Train accuracy for overfitting check
    y_train_pred = model.predict(X_train).argmax(axis=1)
    train_acc = accuracy_score(y_train, y_train_pred)
    gap = train_acc - test_acc

    print(f"\n  Train: {train_acc*100:.1f}% | Test: {test_acc*100:.1f}% | "
          f"Gap: {gap*100:.1f}%")
    print(f"  Test log-loss: {test_logloss:.4f}")

    RF_TEST_ACC = 0.459
    delta = test_acc - RF_TEST_ACC
    print(f"\n  RF baseline:  45.9%")
    print(f"  GBDT:         {test_acc*100:.1f}%")
    print(f"  Improvement:  {delta*100:+.1f}% "
          f"({'✓ BEATS RF' if delta > 0 else '✗ BELOW RF'})")
    print(f"  PRD ≥50%:     {'✓ MET' if test_acc >= 0.50 else '✗ NOT MET'}\n")

    print(classification_report(y_test, y_pred, target_names=le.classes_))

    # ── Confusion Matrix ──
    cm = confusion_matrix(y_test, y_pred)
    fig, ax = plt.subplots(figsize=(14, 12))
    im = ax.imshow(cm, interpolation="nearest", cmap="Blues")
    ax.set_title("Genre — LightGBM Confusion Matrix", fontsize=16, pad=15)
    fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    names = le.classes_
    ticks = np.arange(len(names))
    ax.set_xticks(ticks); ax.set_xticklabels(names, rotation=45, ha="right", fontsize=9)
    ax.set_yticks(ticks); ax.set_yticklabels(names, fontsize=9)
    ax.set_xlabel("Predicted"); ax.set_ylabel("True")
    thresh = cm.max() / 2
    for i in range(len(names)):
        for j in range(len(names)):
            v = cm[i, j]
            if v > 0:
                ax.text(j, i, f"{v:,}" if v > 999 else str(v),
                        ha="center", va="center", fontsize=6,
                        color="white" if v > thresh else "black")
    plt.tight_layout()
    plt.savefig("genre_gbdt_confusion.png", dpi=150)
    print("  ✓ genre_gbdt_confusion.png")
    plt.close()

    # ── Feature Importances ──
    importances = model.feature_importance(importance_type="gain")
    imp_normed = importances / importances.sum()
    sorted_idx = np.argsort(imp_normed)[::-1]

    print("\n  Feature Importances (gain):")
    for i in sorted_idx:
        bar = "█" * int(imp_normed[i] * 100)
        print(f"    {FEATURE_COLS[i]:<20s} {imp_normed[i]:.4f}  {bar}")

    fig, ax = plt.subplots(figsize=(10, 6))
    y_pos = np.arange(len(FEATURE_COLS))
    ax.barh(y_pos, imp_normed[sorted_idx], color="#4a90d9", edgecolor="none")
    ax.set_yticks(y_pos)
    ax.set_yticklabels([FEATURE_COLS[i] for i in sorted_idx], fontsize=10)
    ax.set_xlabel("Importance (gain, normalized)", fontsize=12)
    ax.set_title("LightGBM — Feature Importances", fontsize=14, pad=10)
    ax.invert_yaxis()
    plt.tight_layout()
    plt.savefig("genre_gbdt_importances.png", dpi=150)
    print("  ✓ genre_gbdt_importances.png")
    plt.close()

    # ── Training Curves ──
    if evals_result:
        fig, ax = plt.subplots(figsize=(10, 5))
        train_loss = evals_result.get("train", {}).get("multi_logloss", [])
        valid_loss = evals_result.get("valid", {}).get("multi_logloss", [])
        if train_loss:
            ax.plot(range(1, len(train_loss)+1), train_loss, "b-",
                    lw=1.5, label="Train", alpha=0.7)
        if valid_loss:
            ax.plot(range(1, len(valid_loss)+1), valid_loss, "r-",
                    lw=1.5, label="Valid")
        best_iter = model.best_iteration
        if best_iter and valid_loss:
            ax.axvline(x=best_iter, color="green", ls="--", alpha=0.7,
                       label=f"Best iter ({best_iter})")
        ax.set(xlabel="Boosting Round", ylabel="Multi-class Log Loss",
               title="LightGBM Training Curves")
        ax.legend(); ax.grid(True, alpha=0.3)
        plt.tight_layout()
        plt.savefig("genre_gbdt_curves.png", dpi=150)
        print("  ✓ genre_gbdt_curves.png")
        plt.close()

    print()
    return test_acc


# ── Step 5: Save ─────────────────────────────────────────────────────
def save_model(model):
    print("══════════════════════════════════════════════════════")
    print("  STEP 5: Saving Model")
    print("══════════════════════════════════════════════════════")
    # Text format is portable, human-readable, and fast to load
    model.save_model("genre_gbdt.txt")
    print("  ✓ genre_gbdt.txt")
    # Also save as joblib for sklearn-compatible pipeline
    joblib.dump(model, "genre_gbdt.joblib")
    print("  ✓ genre_gbdt.joblib\n")


# ── Main ──────────────────────────────────────────────────────────────
def main():
    wall_start = time.time()
    print("\n╔══════════════════════════════════════════════════════╗")
    print("║  LightGBM — GENRE CLASSIFICATION                   ║")
    print("║  Target: ≥50% (RF: 45.9%)                          ║")
    print("╚══════════════════════════════════════════════════════╝\n")

    for f in ["genre_scaler.joblib", "genre_label_encoder.joblib", CSV_PATH]:
        if not os.path.exists(f):
            print(f"  ✗ Missing {f}"); sys.exit(1)

    sample = load_stratified_sample()
    X_train, X_test, y_train, y_test, le = preprocess(sample)
    del sample

    model, evals_result = train_gbdt(
        X_train, y_train, X_test, y_test, len(le.classes_)
    )
    test_acc = evaluate(model, X_train, X_test, y_train, y_test, le, evals_result)
    save_model(model)

    wall = time.time() - wall_start
    print("╔══════════════════════════════════════════════════════╗")
    print(f"║  DONE — Test: {test_acc*100:.1f}%  "
          f"(RF: 45.9%, Δ: {(test_acc-0.459)*100:+.1f}%)")
    print(f"║  PRD ≥50%: {'✓ MET' if test_acc >= 0.50 else '✗ NOT MET'}")
    print(f"║  Wall time: {wall:.0f}s ({wall/60:.1f} min)")
    print("╚══════════════════════════════════════════════════════╝")


if __name__ == "__main__":
    main()
