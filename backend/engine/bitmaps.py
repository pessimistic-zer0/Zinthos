"""Per-family FAISS ID selectors — filter retrieval to the seed's region/sonic family.

Two on-disk formats, both built by backend/build_tag_bitmaps.py and both producing the same
thing: a faiss.IDSelector over track_ids. `manifest.json` says which one is present.

  "bitmap" (default, production) — one uint8 array per family, bit = track_id. Sized by
      max(track_id), so 32 MB per family / ~700 MB for 22 at the full catalog's 256M ids.
      Loaded mmap'd, so only the families a query touches are paged in.

  "ids" (`--ids`, for a demo slice) — one sorted int64 track_id array per family, sized by
      MEMBERSHIP rather than by id space. A slice keeps master.db's track_ids (they are what
      FAISS and every engine query speak), so the bitmap form would still cost 32 MB per
      family to describe a few tens of thousands of members. The id form costs ~0.5 MB for
      all 22. faiss.IDSelectorBatch hashes them, so the lookup stays O(1) per candidate.

A seed carrying several families of one axis (south_asian+mena via a `sufi` tag) gets the
union of their arrays, computed once per distinct mask and kept in a small cache.

Refuses the files when the manifest's family order disagrees with engine.tagfamily — the
same drift guard as track_tag_family, for the same reason: a reordered dict would silently
point bit 0 at the wrong family.
"""
from __future__ import annotations

import json
import os
from collections import OrderedDict
from typing import Any

import numpy as np

from . import tagfamily
from .config import CONFIG

_files: dict[tuple[str, int], np.ndarray] = {}      # (axis, bit) -> bitmap or id array
_union_cache: "OrderedDict[tuple[str, int], np.ndarray]" = OrderedDict()   # (axis, mask) -> array
_selectors: dict[tuple[str, int], Any] = {}         # (axis, mask) -> faiss.IDSelector
_CACHE_MAX = 8
_FORMAT = "bitmap"
ENABLED = False


def init() -> str:
    """Load the selectors iff present and their family order matches tagfamily. Returns a log line."""
    global ENABLED, _FORMAT
    ENABLED = False
    _files.clear(); _union_cache.clear(); _selectors.clear()
    if not CONFIG.tag_filter:
        return "tag bitmaps disabled (SONIC_TAG_FILTER=0) — unfiltered retrieval"
    path = os.path.join(CONFIG.bitmap_dir, "manifest.json")
    if not os.path.exists(path):
        return f"tag bitmaps absent ({CONFIG.bitmap_dir}) — unfiltered retrieval (run backend/build_tag_bitmaps.py)"
    with open(path) as fh:
        man = json.load(fh)
    live = ([("region", i, f) for i, f in enumerate(tagfamily.REGION_RULES)]
            + [("sonic", i, f) for i, f in enumerate(tagfamily.SONIC_RULES)])
    disk = [(r["axis"], r["bit"], r["name"]) for r in man["families"]]
    if disk != live:
        return "tag bitmaps DISAGREE with engine.tagfamily — unfiltered retrieval. Rebuild them."
    _FORMAT = man.get("format", "bitmap")
    suffix = ".ids.npy" if _FORMAT == "ids" else ".npy"
    total = 0
    for ax, i, f in live:
        arr = np.load(os.path.join(CONFIG.bitmap_dir, f"{ax}_{i}_{f}{suffix}"), mmap_mode="r")
        _files[(ax, i)] = arr
        total += arr.nbytes
    ENABLED = True
    if _FORMAT == "ids":
        return (f"tag id-sets ready ({len(_files)} families, {total / 1e6:.1f} MB total) "
                f"— filtered retrieval on")
    return (f"tag bitmaps ready ({len(_files)} families, {man['nbytes'] / 1e6:.0f} MB each, "
            f"mmap'd) — filtered retrieval on")


def _union(axis: str, mask: int) -> np.ndarray:
    """The union of the family arrays set in `mask`; single-family masks use the file directly."""
    bits = [i for i in range(mask.bit_length()) if mask >> i & 1 and (axis, i) in _files]
    if len(bits) == 1:
        return _files[(axis, bits[0])]
    key = (axis, mask)
    arr = _union_cache.get(key)
    if arr is None:
        if _FORMAT == "ids":
            arr = np.unique(np.concatenate([_files[(axis, i)] for i in bits]))
        else:
            arr = np.zeros_like(_files[(axis, bits[0])])
            for i in bits:
                np.bitwise_or(arr, _files[(axis, i)], out=arr)
        _union_cache[key] = arr
        if len(_union_cache) > _CACHE_MAX:
            old, _ = _union_cache.popitem(last=False)
            _selectors.pop(old, None)
    return arr


def selector(axis: str, mask: int) -> Any | None:
    """A faiss.IDSelector over every track carrying any family in `mask`, or None."""
    if not ENABLED or not mask:
        return None
    key = (axis, mask)
    sel = _selectors.get(key)
    if sel is None:
        import faiss
        arr = _union(axis, mask)
        if not arr.size:
            return None
        if _FORMAT == "ids":
            # IDSelectorBatch COPIES the ids into its own hash set, but keep the array
            # referenced anyway so the two formats have one lifetime rule between them.
            arr = np.ascontiguousarray(arr, dtype=np.int64)
            sel = faiss.IDSelectorBatch(arr.size, faiss.swig_ptr(arr))
            sel.referenced_arrays = arr          # type: ignore[attr-defined]
        else:
            # IDSelectorBitmap holds a RAW POINTER: the array must stay referenced for as long
            # as the selector does. _files / _union_cache own the arrays; _selectors mirrors
            # their lifetime.
            sel = faiss.IDSelectorBitmap(arr.size, faiss.swig_ptr(arr))
        _selectors[key] = sel
    return sel
