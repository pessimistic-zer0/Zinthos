"""
train_sae.py — Stage 2 of F6: train the Supervised Autoencoder
═══════════════════════════════════════════════════════════════════════════════
Trains a tiny joint model on the Stage-1 arrays (sae_X.npy / sae_y.npy):

    x(13) ─► ENCODER ─► [10-D + BatchNorm] ─┬─► DECODER ─► x̂   reconstruction (MSE)
                                            └─► Linear  ─► ŷ   genre (weighted CE)

    loss = MSE_meanperfeat(x̂, x) + α · CE_weighted(ŷ, y)

After training we keep ONLY the encoder (sae_encoder.pt). See
model_training/F6_implementation_plan.md §1/§3/§7 for the full rationale.

Decisions baked in (the "knobs"):
  • α = 0.1            — reconstruction is the PRIMARY objective; genre is a regularizer.
                         (recon settles ~0.1-0.2, 20-class CE ~1.3-1.8 → α≈0.1 ≈ gradient parity)
  • BatchNorm1d(10) at the bottleneck, NO activation — gives the 10 dims ≈unit variance for
                         free (deterministic via running stats at inference); conditions the
                         space for COSINE similarity (FAISS normalizes downstream; DB keeps raw).
  • shallow classifier (Linear(10→20)) — forces the *bottleneck* to be linearly genre-separable.
  • class weights = sqrt-inverse-freq, normalized so the frequency-weighted mean weight = 1
                         (preserves the CE scale / effective α), capped at 10×. Handles the ~75×
                         genre imbalance so rare genres aren't swallowed by acoustic neighbours.

Load-bearing: features were scaled in Stage 1 with genre_scaler.joblib; we do NOT touch raw
features here. FEATURE_COLS / class order must match Stage 1 (read from sae_prep_meta.json).

Usage:
  python train_sae.py                      # full run (α=0.1, ≤5 epochs, early stop)
  python train_sae.py --alpha 0.05         # sweep the recon/genre balance
  python train_sae.py --no-class-weights   # ablate the imbalance handling
  python train_sae.py --limit-steps 50     # GPU smoke test (under a minute)
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
from datetime import datetime, timezone

import numpy as np
import torch
import torch.nn as nn

# ── Paths (resolved relative to this file) ────────────────────────────────────
HERE = os.path.dirname(os.path.abspath(__file__))
X_PATH = os.path.join(HERE, "sae_X.npy")
Y_PATH = os.path.join(HERE, "sae_y.npy")
META_PATH = os.path.join(HERE, "sae_prep_meta.json")
ENCODER_OUT = os.path.join(HERE, "sae_encoder.pt")
CONFIG_OUT = os.path.join(HERE, "sae_config.json")
CURVES_OUT = os.path.join(HERE, "sae_curves.png")

MODEL_VERSION = "autoencoder_v1"

# ── Hyperparameters (defaults; the consequential ones are CLI-overridable) ────
EMB_DIM = 10            # fixed by PRD
ALPHA = 0.1            # recon ↔ genre balance (knob)
BATCH = 16_384
EPOCHS_MAX = 5
LR = 1e-3
WEIGHT_DECAY = 1e-5
VAL_MODULO = 100        # deterministic holdout: row_index % 100 == 0  → ~1% val
WEIGHT_CAP = 10.0       # cap on per-class CE weight (× the freq-weighted mean of 1)
EVAL_EVERY = 2_000      # optimizer steps between val evaluations
PATIENCE = 3            # early-stop after this many evals without val-loss improvement
LOG_EVERY = 500
SEED = 42


# ── Model ─────────────────────────────────────────────────────────────────────
class SAE(nn.Module):
    """Supervised autoencoder. Encoder ends in BatchNorm (no activation) at the
    10-D bottleneck; classifier is a single linear layer off that bottleneck."""

    def __init__(self, in_dim: int = 13, emb_dim: int = EMB_DIM, n_classes: int = 20):
        super().__init__()
        self.encoder = nn.Sequential(
            nn.Linear(in_dim, 64), nn.BatchNorm1d(64), nn.ReLU(),
            nn.Linear(64, 32), nn.BatchNorm1d(32), nn.ReLU(),
            nn.Linear(32, emb_dim), nn.BatchNorm1d(emb_dim),   # bottleneck: BN, NO activation
        )
        self.decoder = nn.Sequential(
            nn.Linear(emb_dim, 32), nn.BatchNorm1d(32), nn.ReLU(),
            nn.Linear(32, 64), nn.BatchNorm1d(64), nn.ReLU(),
            nn.Linear(64, in_dim),
        )
        self.classifier = nn.Linear(emb_dim, n_classes)

    def forward(self, x):
        z = self.encoder(x)
        return z, self.decoder(z), self.classifier(z)


# ── Class weights: sqrt-inverse-freq, freq-weighted-mean-normalized, capped ───
def compute_class_weights(y: np.ndarray, n_classes: int, cap: float) -> np.ndarray:
    counts = np.bincount(np.asarray(y, dtype=np.int64), minlength=n_classes).astype(np.float64)
    counts = np.maximum(counts, 1.0)                  # guard empty class
    w = 1.0 / np.sqrt(counts)                         # sqrt-inverse-frequency
    p = counts / counts.sum()
    w = w / float(p @ w)                              # E_p[w] = 1 → preserves CE scale / effective α
    w = np.minimum(w, cap)                            # cap rare-class emphasis
    return w.astype(np.float32)


# ── Evaluation over the held-out val rows ─────────────────────────────────────
@torch.no_grad()
def evaluate(model, X, y, val_idx, device, alpha, ce_loss, n_classes):
    model.eval()
    mse = nn.MSELoss(reduction="sum")
    recon_sum = clf_sum = n_seen = 0.0
    correct = np.zeros(n_classes, dtype=np.int64)
    total = np.zeros(n_classes, dtype=np.int64)
    for s in range(0, len(val_idx), BATCH):
        idx = val_idx[s:s + BATCH]
        xb = torch.from_numpy(np.ascontiguousarray(X[idx])).to(device)
        yt = y[idx].astype(np.int64)
        yb = torch.from_numpy(yt).to(device)
        z, xhat, logits = model(xb)
        recon_sum += mse(xhat, xb).item() / xb.shape[1]          # → mean-per-feature units
        clf_sum += ce_loss(logits, yb).item() * len(idx)         # ce is 'mean' → ×n for sum
        n_seen += len(idx)
        preds = logits.argmax(1).cpu().numpy()
        total += np.bincount(yt, minlength=n_classes)
        correct += np.bincount(yt[preds == yt], minlength=n_classes)
    model.train()
    recon = recon_sum / n_seen
    clf = clf_sum / n_seen
    overall_acc = correct.sum() / max(total.sum(), 1)
    per_class = correct / np.maximum(total, 1)
    return recon, clf, recon + alpha * clf, overall_acc, per_class


def run(args: argparse.Namespace) -> None:
    for p in (X_PATH, Y_PATH, META_PATH):
        if not os.path.exists(p):
            sys.exit(f"✗ Missing {p} — run prep_sae_data.py (Stage 1) first.")

    torch.manual_seed(SEED)
    np.random.seed(SEED)

    # Distinct output names per run so an α-sweep doesn't clobber the canonical artifacts.
    tag = f"_{args.tag}" if args.tag else ""
    enc_out = os.path.join(HERE, f"sae_encoder{tag}.pt")
    cfg_out = os.path.join(HERE, f"sae_config{tag}.json")
    curves_out = os.path.join(HERE, f"sae_curves{tag}.png")

    meta = json.load(open(META_PATH))
    classes = meta["classes"]
    n_classes = len(classes)
    feature_cols = meta["feature_cols"]
    in_dim = len(feature_cols)

    X = np.load(X_PATH, mmap_mode="r")
    y = np.load(Y_PATH, mmap_mode="r")
    N = X.shape[0]
    assert X.shape == (N, in_dim) and y.shape == (N,), "array/meta shape mismatch"
    assert meta["n"] == N, f"meta n={meta['n']} but sae_X.npy has {N} rows"

    device = "cuda" if torch.cuda.is_available() else "cpu"
    if device == "cpu":
        print("⚠ CUDA not available — training on CPU (fine for --limit-steps smoke tests only).")

    # ── Deterministic 1% modulo holdout ───────────────────────────────────────
    all_idx = np.arange(N, dtype=np.int64)
    is_val = (all_idx % VAL_MODULO) == 0
    val_idx = all_idx[is_val]
    train_idx = all_idx[~is_val]                       # ~1 GB; shuffled IN PLACE each epoch
    del all_idx, is_val

    # ── Class weights ─────────────────────────────────────────────────────────
    if args.no_class_weights:
        class_w = None
        print("  class weighting: OFF (ablation)")
    else:
        w = compute_class_weights(y, n_classes, WEIGHT_CAP)
        class_w = torch.from_numpy(w).to(device)
        top = np.argsort(w)[::-1][:3]
        print("  class weighting: sqrt-inv-freq, E_p[w]=1, cap "
              f"{WEIGHT_CAP:g}× | heaviest: "
              + ", ".join(f"{classes[i]}×{w[i]:.1f}" for i in top))

    model = SAE(in_dim=in_dim, emb_dim=EMB_DIM, n_classes=n_classes).to(device)
    n_params = sum(p.numel() for p in model.parameters())
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=WEIGHT_DECAY)
    steps_per_epoch = (len(train_idx) + BATCH - 1) // BATCH
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(
        opt, T_max=max(args.epochs * steps_per_epoch, 1))
    mse_loss = nn.MSELoss()                                       # mean over all elements
    ce_loss = nn.CrossEntropyLoss(weight=class_w)

    print(f"\n╔═══ TRAIN SAE ═══╗  N={N:,}  train={len(train_idx):,}  val={len(val_idx):,}")
    print(f"  params={n_params:,} | α={args.alpha} | batch={BATCH:,} | lr={args.lr} | "
          f"epochs≤{args.epochs} | device={device} | steps/epoch≈{steps_per_epoch:,}")

    history: list[dict] = []
    best_val = float("inf")
    best_state = None
    best_epoch = -1
    no_improve = 0
    gstep = 0
    run_recon = run_clf = run_n = 0.0
    wall = time.time()
    stop = False

    def pin(a):
        t = torch.from_numpy(a)
        return t.pin_memory() if device == "cuda" else t

    for epoch in range(args.epochs):
        if stop:
            break
        np.random.shuffle(train_idx)                              # in-place; ~1 GB, no extra alloc
        for s in range(0, len(train_idx), BATCH):
            idx = train_idx[s:s + BATCH]
            xb = pin(np.ascontiguousarray(X[idx])).to(device, non_blocking=True)
            yb = pin(y[idx].astype(np.int64)).to(device, non_blocking=True)

            z, xhat, logits = model(xb)
            recon = mse_loss(xhat, xb)
            clf = ce_loss(logits, yb)
            loss = recon + args.alpha * clf

            opt.zero_grad(set_to_none=True)
            loss.backward()
            opt.step()
            sched.step()
            gstep += 1

            run_recon += recon.item() * len(idx)
            run_clf += clf.item() * len(idx)
            run_n += len(idx)

            if gstep % LOG_EVERY == 0:
                print(f"  e{epoch} step {gstep:>6,} | "
                      f"recon {run_recon/run_n:.4f} clf {run_clf/run_n:.4f} | "
                      f"lr {sched.get_last_lr()[0]:.2e} | {run_n/(time.time()-wall):,.0f} smpl/s",
                      end="\r", flush=True)

            if gstep % EVAL_EVERY == 0:
                v_recon, v_clf, v_total, v_acc, v_pc = evaluate(
                    model, X, y, val_idx, device, args.alpha, ce_loss, n_classes)
                print(f"\n  [eval @ {gstep:>6,}] val recon {v_recon:.4f} | clf {v_clf:.4f} | "
                      f"total {v_total:.4f} | genre-acc {v_acc*100:.1f}%")
                history.append({"step": gstep, "epoch": epoch,
                                "train_recon": run_recon/run_n, "train_clf": run_clf/run_n,
                                "val_recon": v_recon, "val_clf": v_clf, "val_total": v_total,
                                "val_acc": float(v_acc)})
                run_recon = run_clf = run_n = 0.0
                if v_total < best_val - 1e-4:
                    best_val, best_epoch, no_improve = v_total, epoch, 0
                    best_state = copy.deepcopy(model.encoder.state_dict())
                else:
                    no_improve += 1
                    if no_improve >= PATIENCE:
                        print(f"  ⏹ early stop: no val improvement in {PATIENCE} evals.")
                        stop = True
                        break
            if args.limit_steps and gstep >= args.limit_steps:
                print(f"\n  [smoke] stopping at {gstep} steps.")
                stop = True
                break

    # Final eval + ensure we have a best snapshot (e.g. limit-steps ran < EVAL_EVERY)
    v_recon, v_clf, v_total, v_acc, v_pc = evaluate(
        model, X, y, val_idx, device, args.alpha, ce_loss, n_classes)
    if best_state is None or v_total < best_val:
        best_val, best_epoch = v_total, epoch
        best_state = copy.deepcopy(model.encoder.state_dict())

    # ── Save encoder + config FIRST (so plotting can't lose the model) ────────
    torch.save(best_state, enc_out)
    config = {
        "model_version": MODEL_VERSION,
        "in_dim": in_dim, "emb_dim": EMB_DIM, "n_classes": n_classes,
        "encoder_widths": [in_dim, 64, 32, EMB_DIM],
        "decoder_widths": [EMB_DIM, 32, 64, in_dim],
        "bottleneck_batchnorm": True, "classifier": "linear",
        "feature_cols": feature_cols, "classes": classes,
        "scaler_path": meta["scaler_path"],
        "alpha": args.alpha, "batch": BATCH, "lr": args.lr,
        "epochs_max": args.epochs, "best_epoch": best_epoch,
        "class_weighting": ("off" if args.no_class_weights
                            else f"sqrt_inv_freq_Ep1_cap{WEIGHT_CAP:g}"),
        "val_modulo": VAL_MODULO,
        "embedding_metric": "cosine", "blob_dtype": "<f4", "blob_stores": "raw",
        "val_recon_mse": v_recon, "val_clf_ce": v_clf,
        "val_total": v_total, "val_genre_acc": float(v_acc),
        "val_per_class_acc": {classes[i]: float(v_pc[i]) for i in range(n_classes)},
        "n_params": int(n_params), "device": device,
        "smoke": bool(args.limit_steps), "seed": SEED,
        "trained_utc": datetime.now(timezone.utc).isoformat(),
        "wall_sec": round(time.time() - wall, 1),
    }
    json.dump(config, open(cfg_out, "w"), indent=2)

    # ── Curves ────────────────────────────────────────────────────────────────
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        if history:
            steps = [h["step"] for h in history]
            fig, ax = plt.subplots(1, 3, figsize=(16, 4.5))
            ax[0].plot(steps, [h["train_recon"] for h in history], label="train")
            ax[0].plot(steps, [h["val_recon"] for h in history], label="val")
            ax[0].set(title="Reconstruction MSE (mean/feat)", xlabel="step"); ax[0].legend(); ax[0].grid(alpha=.3)
            ax[1].plot(steps, [h["train_clf"] for h in history], label="train")
            ax[1].plot(steps, [h["val_clf"] for h in history], label="val")
            ax[1].set(title="Classification CE", xlabel="step"); ax[1].legend(); ax[1].grid(alpha=.3)
            ax[2].plot(steps, [h["val_acc"] * 100 for h in history], color="green")
            ax[2].axhline(50, ls="--", color="gray", alpha=.6, label="GBDT ~50%")
            ax[2].set(title="Val genre accuracy (%)", xlabel="step"); ax[2].legend(); ax[2].grid(alpha=.3)
            plt.tight_layout(); plt.savefig(curves_out, dpi=130); plt.close()
            print(f"  ✓ {os.path.basename(curves_out)}")
    except Exception as e:  # noqa: BLE001 — never lose the model over a plotting hiccup
        print(f"  ⚠ curve plotting skipped: {e!r}")

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n══════════════════ STAGE 2 DONE ══════════════════")
    print(f"  Best epoch       : {best_epoch}  (val total {best_val:.4f})")
    print(f"  Val recon MSE    : {v_recon:.4f}")
    print(f"  Val genre acc    : {v_acc*100:.1f}%  (GBDT ref ~50%)")
    print(f"  Wall time        : {(time.time()-wall)/60:.1f} min")
    print("  Per-class val acc (sorted):")
    for i in np.argsort(v_pc)[::-1]:
        bar = "█" * int(v_pc[i] * 30)
        print(f"    {classes[i]:<12s} {v_pc[i]*100:5.1f}% {bar}")
    print(f"\n  ✓ encoder → {os.path.basename(enc_out)}")
    print(f"  ✓ config  → {os.path.basename(cfg_out)}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="F6 Stage 2: train the supervised autoencoder")
    p.add_argument("--alpha", type=float, default=ALPHA, help="recon ↔ genre weight (default 0.1)")
    p.add_argument("--epochs", type=int, default=EPOCHS_MAX, help="max epochs (early stop usually wins)")
    p.add_argument("--lr", type=float, default=LR)
    p.add_argument("--no-class-weights", action="store_true", help="ablate imbalance handling")
    p.add_argument("--tag", type=str, default="", help="suffix output files (e.g. a001) so sweeps don't clobber")
    p.add_argument("--limit-steps", type=int, default=0, help="stop after N steps (GPU smoke test)")
    return p.parse_args()


if __name__ == "__main__":
    run(parse_args())
