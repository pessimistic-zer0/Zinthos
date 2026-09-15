"""Per-family FAISS ID bitmaps — filter retrieval to the seed's region/sonic family.

Built by backend/build_tag_bitmaps.py (one uint8 array per family, bit = track_id). Loaded
mmap'd, so only the families a query touches are paged in (32 MB each). A seed carrying
several families of one axis (south_asian+mena via a `sufi` tag) gets the OR of their bitmaps,
computed once per distinct mask and kept in a small cache.

Refuses the files when the manifest's family order disagrees with engine.tagfamily — the same
drift guard as track_tag_family, for the same reason: a reordered dict would silently point
bit 0 at the wrong family.
"""
from __future__ import annotations

import json
import os
from collections import OrderedDict
from typing import Any

import numpy as np

from . import tagfamily
from .config import CONFIG

_files: dict[tuple[str, int], np.ndarray] = {}      # (axis, bit) -> mmap'd uint8 array
_or_cache: "OrderedDict[tuple[str, int], np.ndarray]" = OrderedDict()   # (axis, mask) -> array
_selectors: dict[tuple[str, int], Any] = {}         # (axis, mask) -> faiss.IDSelectorBitmap
_CACHE_MAX = 8
ENABLED = False


def init() -> str:
    """Load the bitmaps iff present and their family order matches tagfamily. Returns a log line."""
    global ENABLED
    ENABLED = False
    _files.clear(); _or_cache.clear(); _selectors.clear()
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
    for ax, i, f in live:
        _files[(ax, i)] = np.load(os.path.join(CONFIG.bitmap_dir, f"{ax}_{i}_{f}.npy"), mmap_mode="r")
    ENABLED = True
    return f"tag bitmaps ready ({len(_files)} families, {man['nbytes']/1e6:.0f} MB each, mmap'd) — filtered retrieval on"


def _array(axis: str, mask: int) -> np.ndarray:
    """The OR of the family bitmaps set in `mask`; single-family masks use the mmap directly."""
    bits = [i for i in range(mask.bit_length()) if mask >> i & 1 and (axis, i) in _files]
    if len(bits) == 1:
        return _files[(axis, bits[0])]
    key = (axis, mask)
    arr = _or_cache.get(key)
    if arr is None:
        arr = np.zeros_like(_files[(axis, bits[0])])
        for i in bits:
            np.bitwise_or(arr, _files[(axis, i)], out=arr)
        _or_cache[key] = arr
        if len(_or_cache) > _CACHE_MAX:
            old, _ = _or_cache.popitem(last=False)
            _selectors.pop(old, None)
    return arr


def selector(axis: str, mask: int) -> Any | None:
    """A faiss.IDSelectorBitmap over every track carrying any family in `mask`, or None."""
    if not ENABLED or not mask:
        return None
    key = (axis, mask)
    sel = _selectors.get(key)
    if sel is None:
        import faiss
        arr = _array(axis, mask)
        # The selector holds a raw pointer: the array must stay referenced for as long as the
        # selector does. _files / _or_cache own the arrays; _selectors mirrors their lifetime.
        sel = faiss.IDSelectorBitmap(arr.size, faiss.swig_ptr(arr))
        _selectors[key] = sel
    return sel
