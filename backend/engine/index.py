"""FAISS vector index — mmap'd once at startup, shared across all requests.

The 9 GB IVFSQfp16 index is memory-mapped (IO_FLAG_MMAP), NOT loaded resident: a query
at nprobe=64 touches ~0.4% of the inverted lists, so the OS pages in only what's hot.
IndexIDMap2 means search returns track_ids directly. Metric is cosine via normalized IP.

Index TYPE is discovered, not assumed. SONIC_INDEX can point at the production IVF or at
the small exact-flat index a demo slice builds (demo/build_demo_index.py), and nprobe and
the search-parameter class follow from which one it is — see is_ivf below.
"""
from __future__ import annotations

import numpy as np

from .config import CONFIG


class VectorIndex:
    def __init__(self) -> None:
        import faiss

        self._faiss = faiss
        self.index = faiss.read_index(CONFIG.index_path, faiss.IO_FLAG_MMAP)
        # Whether there is an IVF under the wrapper decides BOTH knobs below. The production
        # index is IVFSQfp16; a demo slice (demo/build_demo_index.py) is small enough to be a
        # flat exact index, where nprobe is meaningless and setting it raises.
        try:
            faiss.extract_index_ivf(self.index)
            self.is_ivf = True
        except RuntimeError:
            self.is_ivf = False
        if self.is_ivf:
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

    def search(self, vec: np.ndarray, k: int, sel: object | None = None) -> list[tuple[int, float]]:
        """Return [(track_id, cosine_score), …] for one query vector (normalized here).

        `sel` is an optional faiss.IDSelector (engine.bitmaps): the IVF scan then skips every
        vector outside it, so all k hits come from the selected family. IndexIDMap2 translates
        the selector to user ids, so the bitmap is over track_ids. Measured 37-74 ms for k=1500
        at nprobe=64 against ~20 ms unfiltered.
        """
        if sel is None:
            scores, ids = self.index.search(self.normalize(vec), k)
        else:
            # SearchParametersIVF carries nprobe and only an IVF reads it; a flat index needs
            # the plain base class (see is_ivf in __init__).
            params = (self._faiss.SearchParametersIVF(nprobe=CONFIG.nprobe, sel=sel)
                      if self.is_ivf else self._faiss.SearchParameters(sel=sel))
            scores, ids = self.index.search(self.normalize(vec), k, params=params)
        return [(int(i), float(s)) for i, s in zip(ids[0], scores[0]) if i != -1]

    def reconstruct(self, track_id: int) -> np.ndarray | None:
        """The stored vector for `track_id`, or None if the index cannot give one back.

        Only meaningful for an EXACT index (flat / IVFFlat), where what comes back is the
        vector that was added, normalized. The production index is IVFSQfp16 — reconstruct
        there returns a dequantized approximation, which is why similar.get_embedding prefers
        master.db's lossless BLOB whenever ml_10d_embeddings is present and only falls back to
        this for a compacted demo slice that dropped the table.

        IndexIDMap2 is what makes this addressable by track_id at all (IndexIDMap cannot
        reconstruct). An IVF also needs a direct map, which build_demo_index.py builds.
        """
        try:
            return self.index.reconstruct(int(track_id))
        except RuntimeError:
            return None

    def warmup(self, n: int) -> None:
        """Fault in hot inverted lists so the first real query isn't cold."""
        if n <= 0:
            return
        rng = np.random.default_rng(0)
        self.index.search(self.normalize(rng.standard_normal((n, CONFIG.embed_dim))), CONFIG.faiss_topk)
