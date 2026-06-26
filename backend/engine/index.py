"""FAISS vector index — mmap'd once at startup, shared across all requests.

The 9 GB IVFSQfp16 index is memory-mapped (IO_FLAG_MMAP), NOT loaded resident: a query
at nprobe=64 touches ~0.4% of the inverted lists, so the OS pages in only what's hot.
IndexIDMap2 means search returns track_ids directly. Metric is cosine via normalized IP.
"""
from __future__ import annotations

import numpy as np

from .config import CONFIG


class VectorIndex:
    def __init__(self) -> None:
        import faiss

        self._faiss = faiss
        self.index = faiss.read_index(CONFIG.index_path, faiss.IO_FLAG_MMAP)
        # nprobe lives on the underlying IVF; set via ParameterSpace to be wrapper-agnostic.
        faiss.ParameterSpace().set_index_parameter(self.index, "nprobe", CONFIG.nprobe)
        self.ntotal: int = self.index.ntotal

    @staticmethod
    def normalize(vec: np.ndarray) -> np.ndarray:
        """L2-normalize rows to float32 (cosine == inner product on normalized vectors)."""
        v = np.ascontiguousarray(vec, dtype=np.float32).reshape(-1, CONFIG.embed_dim)
        n = np.linalg.norm(v, axis=1, keepdims=True)
        n[n == 0] = 1.0
        return v / n

    def search(self, vec: np.ndarray, k: int) -> list[tuple[int, float]]:
        """Return [(track_id, cosine_score), …] for one already-normalized query vector."""
        scores, ids = self.index.search(self.normalize(vec), k)
        return [(int(i), float(s)) for i, s in zip(ids[0], scores[0]) if i != -1]

    def warmup(self, n: int) -> None:
        """Fault in hot inverted lists so the first real query isn't cold."""
        if n <= 0:
            return
        rng = np.random.default_rng(0)
        self.index.search(self.normalize(rng.standard_normal((n, CONFIG.embed_dim))), CONFIG.faiss_topk)
