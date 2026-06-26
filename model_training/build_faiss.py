"""
build_faiss.py — Stage 4 of F6: build the FAISS similarity index
═══════════════════════════════════════════════════════════════════════════════
Builds an IVFPQ index over the 254.82M 10-D embeddings (from Stage 3's flat files)
for fast "perceptually similar tracks" search.

Metric = COSINE: vectors are L2-normalized before train/add and queries are normalized
the same way, so inner product = cosine. The DB BLOBs stay RAW (lossless); we normalize
only here. The index is IndexIDMap2 so search returns track_ids directly.

  IVFPQ(d=10, nlist=32768, m=5, nbits=8, METRIC_INNER_PRODUCT)  → ~5 B/vec → index ≈ 1.3 GB
    • nlist ≈ 2·√N  (coarse cells)
    • m=5 → 2 dims per subquantizer (the only sensible split of 10 dims)
    • IVFFlat fallback (--flat): ~10 GB, no PQ loss, higher recall — the escape hatch if
      recall@10 is poor at 10-D (PRD specifies IVFPQ, so that's the default).

Validation: exact brute-force top-10 (chunked over the memmap) for a sample of queries vs.
the index's top-10 → recall@10. This is the gold-standard quality check.

Usage:
  python build_faiss.py                 # full IVFPQ build + recall@10
  python build_faiss.py --flat          # IVFFlat instead (higher recall, ~10 GB)
  python build_faiss.py --nprobe 64     # trade search speed for recall
  python build_faiss.py --limit 5000000 # smoke build on first 5M vecs
"""

from __future__ import annotations

import argparse
import os
import time

import faiss
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
EMB_F32 = os.path.join(HERE, "embeddings.f32")
IDS_I64 = os.path.join(HERE, "embed_ids.i64")
INDEX_OUT = os.path.join(HERE, "embeddings.faiss")

DIM = 10


def normed(a: np.ndarray) -> np.ndarray:
    """L2-normalize rows (a copy); zero-norm rows left as zero (won't match anything)."""
    a = np.ascontiguousarray(a, dtype=np.float32)
    n = np.linalg.norm(a, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return a / n


def exact_topk(q: np.ndarray, vecs: np.ndarray, ids: np.ndarray, k: int,
               chunk: int = 500_000) -> np.ndarray:
    """Exact cosine top-k track_ids for each query, brute-forced over the memmap."""
    Q = q.shape[0]
    best_s = np.full((Q, k), -np.inf, dtype=np.float32)
    best_i = np.zeros((Q, k), dtype=np.int64)
    for s in range(0, len(vecs), chunk):
        block = normed(vecs[s:s + chunk])
        bids = ids[s:s + chunk]
        scores = q @ block.T                                  # (Q, c)
        kk = min(k, scores.shape[1])
        part = np.argpartition(-scores, kk - 1, axis=1)[:, :kk]
        cand_s = np.concatenate([best_s, np.take_along_axis(scores, part, axis=1)], axis=1)
        cand_i = np.concatenate([best_i, bids[part]], axis=1)
        sel = np.argpartition(-cand_s, k - 1, axis=1)[:, :k]
        best_s = np.take_along_axis(cand_s, sel, axis=1)
        best_i = np.take_along_axis(cand_i, sel, axis=1)
    return best_i


def run(args: argparse.Namespace) -> None:
    for p in (EMB_F32, IDS_I64):
        if not os.path.exists(p):
            raise SystemExit(f"✗ Missing {p} — run embed_tracks.py (Stage 3) first.")

    vecs = np.memmap(EMB_F32, dtype="<f4", mode="r").reshape(-1, DIM)
    ids = np.memmap(IDS_I64, dtype="<i8", mode="r")
    assert len(vecs) == len(ids), "embeddings.f32 / embed_ids.i64 row mismatch"
    N = len(vecs) if not args.limit else min(args.limit, len(vecs))
    print(f"  vectors: {N:,} × {DIM}-D  (cosine / inner-product on normalized)")

    quantizer = faiss.IndexFlatIP(DIM)
    if args.flat:
        ivf = faiss.IndexIVFFlat(quantizer, DIM, args.nlist, faiss.METRIC_INNER_PRODUCT)
        kind = f"IVFFlat(nlist={args.nlist})"          # 40 B/vec, no quantization loss
    elif args.fp16:
        ivf = faiss.IndexIVFScalarQuantizer(quantizer, DIM, args.nlist,
                                            faiss.ScalarQuantizer.QT_fp16,
                                            faiss.METRIC_INNER_PRODUCT)
        kind = f"IVFSQfp16(nlist={args.nlist})"        # 20 B/vec, half precision — near-Flat
    elif args.sq:
        ivf = faiss.IndexIVFScalarQuantizer(quantizer, DIM, args.nlist,
                                            faiss.ScalarQuantizer.QT_8bit,
                                            faiss.METRIC_INNER_PRODUCT)
        kind = f"IVFSQ8(nlist={args.nlist})"           # 10 B/vec, per-dim 8-bit — good at low-D
    else:
        ivf = faiss.IndexIVFPQ(quantizer, DIM, args.nlist, args.m, args.nbits,
                               faiss.METRIC_INNER_PRODUCT)
        kind = f"IVFPQ(nlist={args.nlist}, m={args.m}, nbits={args.nbits})"
    index = faiss.IndexIDMap2(ivf)
    faiss.omp_set_num_threads(os.cpu_count() or 8)
    print(f"  index: {kind}, nprobe={args.nprobe}")

    # ── Train on a random sample ──────────────────────────────────────────────
    t = time.time()
    n_train = min(args.sample, N)
    sample_pos = np.random.choice(N, n_train, replace=False)
    sample_pos.sort()                                          # sequential memmap reads
    index.train(normed(vecs[sample_pos]))
    print(f"  ✓ trained on {n_train:,} sampled vectors in {time.time()-t:.0f}s")

    # ── Add everything in batches ─────────────────────────────────────────────
    t = time.time()
    step = 2_000_000
    for s in range(0, N, step):
        e = min(s + step, N)
        index.add_with_ids(normed(vecs[s:e]), np.ascontiguousarray(ids[s:e], dtype=np.int64))
        print(f"    added {e:,}/{N:,} ({(e)/N*100:4.1f}%)  [{time.time()-t:.0f}s]",
              end="\r", flush=True)
    print(f"\n  ✓ added {index.ntotal:,} vectors in {time.time()-t:.0f}s")

    # Persist the index IMMEDIATELY — BEFORE the memory-heavy validation — so a slow or
    # OOM-killed validation can never cost us the (very expensive) build. Learned the hard way.
    ivf.nprobe = args.nprobe
    faiss.write_index(index, args.out)
    print(f"  ✓ wrote {os.path.basename(args.out)} "
          f"({os.path.getsize(args.out)/1e9:.2f} GB), default nprobe={args.nprobe}")

    # ── Recall@10 vs exact brute force (exact computed ONCE, swept over nprobe) ─
    if args.validate:
        t = time.time()
        nq = args.val_queries
        qpos = np.random.choice(N, nq, replace=False)
        q = normed(vecs[qpos])
        exact = [set(row) for row in exact_topk(q, vecs[:N], ids[:N], 10)]
        print(f"  exact top-10 for {nq} queries in {time.time()-t:.0f}s")
        probes = sorted({args.nprobe, 64, 128, 256, 512}) if args.nprobe_sweep else [args.nprobe]
        for npb in probes:
            if npb > args.nlist:
                continue
            ivf.nprobe = npb
            _, approx = index.search(q, 10)
            hits = sum(len(exact[i] & set(approx[i])) for i in range(nq))
            print(f"    nprobe={npb:>4} → recall@10 = {hits/(nq*10)*100:.1f}%")

    print("\n══════════════════ STAGE 4 DONE ══════════════════")
    print(f"  Index: {kind} | vectors {index.ntotal:,} | nprobe {args.nprobe}")
    print(f"  ✓ {os.path.basename(args.out)}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="F6 Stage 4: build the FAISS IVFPQ index")
    p.add_argument("--nlist", type=int, default=32768, help="coarse cells (~2·√N)")
    p.add_argument("--m", type=int, default=5, help="PQ subquantizers (must divide 10)")
    p.add_argument("--nbits", type=int, default=8, help="bits per PQ code")
    p.add_argument("--nprobe", type=int, default=32, help="cells probed at search time")
    p.add_argument("--sample", type=int, default=4_000_000, help="training sample size")
    p.add_argument("--flat", action="store_true", help="IVFFlat (40 B/vec, highest recall)")
    p.add_argument("--sq", action="store_true", help="IVFSQ8 (10 B/vec, 8-bit per dim)")
    p.add_argument("--fp16", action="store_true", help="IVFSQfp16 (20 B/vec, half precision)")
    p.add_argument("--out", type=str, default=INDEX_OUT, help="output index path")
    p.add_argument("--limit", type=int, default=0, help="use only first N vectors (smoke)")
    p.add_argument("--no-validate", dest="validate", action="store_false", help="skip recall@10")
    p.add_argument("--nprobe-sweep", dest="nprobe_sweep", action="store_true",
                   help="report recall@10 across nprobe∈{64,128,256,512} (exact computed once)")
    p.add_argument("--val-queries", type=int, default=200, help="queries for recall@10")
    return p.parse_args()


if __name__ == "__main__":
    run(parse_args())
