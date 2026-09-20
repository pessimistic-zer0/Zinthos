"""
build_demo_index.py — the FAISS index for a demo slice
═══════════════════════════════════════════════════════════════════════════════
Reads the slice's 10-D vectors — from the flat sidecar a compact build writes, or from
`ml_10d_embeddings` when the slice carries it — and writes `demo.faiss` next to them.

WHY FLAT, WHEN PRODUCTION IS IVFPQ
  model_training/build_faiss.py builds IVFPQ(nlist=32768, m=5) because 254.8M × 10-D
  vectors do not fit anywhere at full precision — PQ takes it to ~5 bytes/vector, and the
  9 GB result is mmap'd and scanned at nprobe=64, touching ~0.4% of the lists.

  A slice is three orders of magnitude smaller. 1.5M × 10 × float32 is 60 MB — it fits in
  RAM with room to spare, so there is nothing to compress and nothing to approximate. Flat
  inner-product over normalized vectors is EXACT cosine: the demo's nearest neighbours are
  the true nearest neighbours of its catalog, with no PQ rounding and no IVF recall loss.

  This is the one place the demo is strictly BETTER than production, and it is worth knowing
  when comparing the two by ear: a result the demo gets and the big index misses is an IVFPQ
  recall miss, not a difference in the slice.

  --ivf is there for slices big enough that a brute-force scan per query stops being free.
  The crossover on 2 vCPU is somewhere past a few million vectors; the printed benchmark at
  the end is what to judge it on, not a rule of thumb.

RECONSTRUCTION
  A compact slice drops `ml_10d_embeddings` (66 B/track) because an EXACT index can hand the
  seed vector back itself. That is only true of flat and IVFFlat, so this does not assume it:
  verify_reconstruct() samples the built index and reports the round-trip error, and an IVF
  gets the direct map it needs. If the check fails, the slice must be rebuilt --no-compact.

IDS
  IndexIDMap2, so search returns track_ids directly — the same contract engine/index.py
  expects, and the same contract the tag-id selectors (engine/bitmaps.py) are written
  against. Track ids are never remapped anywhere in the demo path.

Usage:
  python demo/build_demo_index.py                 # flat (exact), the default
  python demo/build_demo_index.py --ivf           # IVFFlat for a very large slice
  python demo/build_demo_index.py --bench 200     # more benchmark queries
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time

import faiss
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.environ.get("ZINTHOS_DEMO_DIR", os.path.join(HERE, "dist"))
DIM = 10
READ_CHUNK = 500_000


def load_sidecar(dir_: str) -> tuple[np.ndarray, np.ndarray] | None:
    """(ids, vectors) from the flat pair build_demo_slice.py writes in compact mode.

    Two aligned arrays, same shape as model_training's embeddings.f32 / embed_ids.i64. They
    exist so demo.db never has to carry 66 B/track of vectors that the exact index already
    holds — see the COMPACTION note in demo/build_demo_slice.py.
    """
    vec_path, ids_path = (os.path.join(dir_, "demo_vectors.f32"),
                          os.path.join(dir_, "demo_vector_ids.i64"))
    if not (os.path.exists(vec_path) and os.path.exists(ids_path)):
        return None
    ids = np.fromfile(ids_path, dtype=np.int64)
    vecs = np.fromfile(vec_path, dtype=np.float32).reshape(-1, DIM)
    if len(ids) != len(vecs):
        raise SystemExit(f"sidecar mismatch: {len(ids):,} ids vs {len(vecs):,} vectors — "
                         "rebuild the slice")
    print(f"  read {len(ids):,} vectors from the sidecar ({vecs.nbytes / 1e6:.0f} MB)")
    return ids, vecs


def load_vectors(db_path: str) -> tuple[np.ndarray, np.ndarray]:
    """(ids, vectors) from ml_10d_embeddings, read in one forward pass over the PK."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.execute("PRAGMA mmap_size=4000000000")
    n = con.execute("SELECT count(*) FROM ml_10d_embeddings").fetchone()[0]
    ids = np.empty(n, dtype=np.int64)
    vecs = np.empty((n, DIM), dtype=np.float32)
    cur = con.execute("SELECT track_id, vector_blob FROM ml_10d_embeddings ORDER BY track_id")
    i, t = 0, time.time()
    while rows := cur.fetchmany(READ_CHUNK):
        for tid, blob in rows:
            ids[i] = tid
            # '<f4' — the blobs are little-endian float32 exactly as embed_tracks.py wrote
            # them, RAW (unnormalized). Normalization happens once, below.
            vecs[i] = np.frombuffer(blob, dtype="<f4", count=DIM)
            i += 1
        print(f"  read {i:,}/{n:,} vectors [{time.time() - t:.0f}s]", end="\r", flush=True)
    con.close()
    print(f"  read {i:,} vectors in {time.time() - t:.0f}s"
          f"  ({vecs.nbytes / 1e6:.0f} MB){' ' * 20}")
    return ids[:i], vecs[:i]


def normed(a: np.ndarray) -> np.ndarray:
    """L2-normalize rows so inner product == cosine. Mirrors build_faiss.normed / index.normalize."""
    a = np.ascontiguousarray(a, dtype=np.float32)
    n = np.linalg.norm(a, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return a / n


def build(vecs: np.ndarray, ids: np.ndarray, ivf: bool, nlist: int) -> faiss.Index:
    if ivf:
        nlist = nlist or max(64, int(2 * np.sqrt(len(vecs))))
        quant = faiss.IndexFlatIP(DIM)
        base = faiss.IndexIVFFlat(quant, DIM, nlist, faiss.METRIC_INNER_PRODUCT)
        t = time.time()
        # Train on a sample: IVF only needs enough points to place nlist centroids.
        sample = vecs[np.random.default_rng(0).choice(len(vecs),
                                                      min(len(vecs), 100 * nlist), replace=False)]
        base.train(sample)
        print(f"  trained IVFFlat nlist={nlist} on {len(sample):,} vectors "
              f"[{time.time() - t:.0f}s]")
    else:
        base = faiss.IndexFlatIP(DIM)
    index = faiss.IndexIDMap2(base)
    t = time.time()
    index.add_with_ids(vecs, ids)
    print(f"  added {index.ntotal:,} vectors [{time.time() - t:.0f}s]")
    if ivf:
        # reconstruct() is how the engine gets a seed vector when the slice dropped
        # ml_10d_embeddings, and an IVF cannot do it without a direct map. Costs ~8 B/vector
        # of RAM at query time; a flat index needs nothing.
        faiss.extract_index_ivf(index).make_direct_map()
        print("  built the IVF direct map (needed for seed-vector reconstruction)")
    return index


def verify_reconstruct(index: faiss.Index, ids: np.ndarray, vecs: np.ndarray) -> bool:
    """Check the index hands back the vectors it was given, for a sample of ids.

    This is what licenses demo.db dropping ml_10d_embeddings: similar.get_embedding falls
    back to index.reconstruct() when the table is absent, and that is only equivalent on an
    EXACT index. Rather than reason about which index types qualify, measure it — and if it
    fails, say plainly that the slice needs --no-compact.
    """
    rng = np.random.default_rng(2)
    sample = rng.choice(len(ids), min(512, len(ids)), replace=False)
    try:
        got = np.vstack([index.reconstruct(int(ids[i])) for i in sample])
    except RuntimeError as e:
        print(f"\n  ⚠ reconstruct() failed ({e.__class__.__name__}): the engine cannot recover "
              "seed vectors from this index.\n    Rebuild the slice with --no-compact so "
              "ml_10d_embeddings ships in demo.db.")
        return False
    err = float(np.abs(got - vecs[sample]).max())
    ok = err < 1e-6
    mark = "✓" if ok else "⚠"
    print(f"  {mark} reconstruct round-trip: max error {err:.2e} over {len(sample)} vectors"
          + ("" if ok else "  — LOSSY, rebuild the slice with --no-compact"))
    return ok


def bench(index: faiss.Index, vecs: np.ndarray, n_queries: int, topk: int) -> dict:
    """Latency at the pool width the engine actually asks for (CONFIG.faiss_topk).

    This is the number that decides SONIC_FAISS_TOPK on the Space: a free Space is 2 vCPU,
    so a pool that is free on a 12-thread desktop may not be there.
    """
    rng = np.random.default_rng(1)
    q = vecs[rng.choice(len(vecs), min(n_queries, len(vecs)), replace=False)]
    index.search(q[:8], topk)                      # warm
    out = {}
    for k in (topk, max(topk // 2, 50), 100):
        t = time.time()
        index.search(q, k)
        ms = (time.time() - t) / len(q) * 1000
        out[k] = round(ms, 2)
        print(f"  k={k:<6} {ms:7.2f} ms/query  ({len(q)} queries, "
              f"{faiss.omp_get_max_threads()} threads)")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--dir", default=OUT_DIR, help=f"slice directory (default {OUT_DIR})")
    ap.add_argument("--ivf", action="store_true", help="IVFFlat instead of exact flat")
    ap.add_argument("--nlist", type=int, default=0, help="IVF cells (default 2·sqrt(N))")
    ap.add_argument("--bench", type=int, default=100, help="benchmark queries (0 to skip)")
    ap.add_argument("--topk", type=int, default=int(os.environ.get("SONIC_FAISS_TOPK", "600")),
                    help="pool width to benchmark — the engine's CONFIG.faiss_topk")
    ap.add_argument("--threads", type=int, default=0,
                    help="cap FAISS threads to model the Space's 2 vCPU")
    args = ap.parse_args()

    if args.threads:
        faiss.omp_set_num_threads(args.threads)
    db_path = os.path.join(args.dir, "demo.db")
    out = os.path.join(args.dir, "demo.faiss")
    if not os.path.exists(db_path):
        raise SystemExit(f"{db_path} not found — run demo/build_demo_slice.py first")

    print(f"zinthos demo index\n  source {db_path}\n  output {out}\n")
    # Compact slices ship the vectors as a flat sidecar and keep them out of demo.db; older
    # or --no-compact slices carry ml_10d_embeddings. Accept either.
    loaded = load_sidecar(args.dir)
    ids, vecs = loaded if loaded else load_vectors(db_path)
    if not len(ids):
        raise SystemExit("no embeddings in the slice — nothing to index")
    vecs = normed(vecs)
    index = build(vecs, ids, args.ivf, args.nlist)
    faiss.write_index(index, out)
    size = os.path.getsize(out)
    print(f"  wrote {size / 1e6:.0f} MB ({size / index.ntotal:.1f} bytes/vector)")

    exact = verify_reconstruct(index, ids, vecs)

    timings = {}
    if args.bench:
        print("\n  search latency")
        timings = bench(index, vecs, args.bench, args.topk)

    man_path = os.path.join(args.dir, "manifest.json")
    man = json.load(open(man_path)) if os.path.exists(man_path) else {}
    man["index"] = {"type": "IVFFlat" if args.ivf else "FlatIP", "vectors": int(index.ntotal),
                    "bytes": size, "exact": not args.ivf, "ms_per_query": timings,
                    "reconstruct_ok": exact}
    with open(man_path, "w") as fh:
        json.dump(man, fh, indent=1)
    print(f"\n✓ {out}")


if __name__ == "__main__":
    main()
