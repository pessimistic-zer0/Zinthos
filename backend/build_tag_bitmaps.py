"""
build_tag_bitmaps.py — one FAISS ID-bitmap per genre family, from `track_tags`
═══════════════════════════════════════════════════════════════════════════════
Writes <out>/<axis>_<bit>_<family>.npy (uint8, bit i of byte j set ⇔ track_id 8j+i carries
the family) plus manifest.json recording the family order they were built from.

WHY
  The F6 tag GATE post-filters the FAISS pool, so it can only work when the pool already
  holds enough of the seed's family. For many Bollywood seeds it does not: Tu Jo Mila's
  1,195-candidate pool held 7 South Asian tracks. The 13 audio features put that song among
  Thai pop and lo-fi, and no post-filter can recover what retrieval never returned.
  A FAISS IDSelectorBitmap filters INSIDE the IVF scan instead: the 1,500 neighbours are
  drawn from the ~3.7M South Asian tracks only (measured 37–74 ms at nprobe=64, all hits in
  family, Tu Jo Mila → A.R. Rahman / Vishal-Shekhar neighbours).

SIZE
  max(track_id) ≈ 256M → 32 MB per family, 22 families ≈ 700 MB on disk. The engine mmaps
  them (np.load mmap_mode='r'), so only the families actually queried are paged in.

BIT STABILITY
  Same rule as build_track_tags.py: bit N is position N in engine.tagfamily's dicts. The
  manifest carries the assignment and engine.bitmaps refuses the files on any mismatch.

Run:  python backend/build_tag_bitmaps.py            (~1 min: one pass over track_tags)
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engine import tagfamily  # noqa: E402
from engine.config import CONFIG  # noqa: E402

BATCH = 2_000_000


def main() -> None:
    out = CONFIG.bitmap_dir
    os.makedirs(out, exist_ok=True)
    con = sqlite3.connect(f"file:{CONFIG.db_path}?mode=ro", uri=True)
    max_id = con.execute("SELECT max(track_id) FROM tracks").fetchone()[0]
    nbytes = max_id // 8 + 1
    fams = ([("region", i, f) for i, f in enumerate(tagfamily.REGION_RULES)]
            + [("sonic", i, f) for i, f in enumerate(tagfamily.SONIC_RULES)])
    bits = {(ax, i): np.zeros(nbytes, dtype=np.uint8) for ax, i, _ in fams}
    print(f"max track_id={max_id:,} → {nbytes/1e6:.0f} MB per family × {len(fams)} families")

    t0 = time.time()
    cur = con.execute("SELECT track_id, region_mask, sonic_mask FROM track_tags")
    n = 0
    while True:
        rows = cur.fetchmany(BATCH)
        if not rows:
            break
        a = np.array(rows, dtype=np.int64)
        ids, rm, sm = a[:, 0], a[:, 1], a[:, 2]
        byte, bit = ids >> 3, (1 << (ids & 7)).astype(np.uint8)
        for ax, i, _ in fams:
            m = (rm if ax == "region" else sm) >> i & 1
            sel = m.astype(bool)
            if sel.any():
                np.bitwise_or.at(bits[(ax, i)], byte[sel], bit[sel])
        n += len(rows)
        print(f"  {n:,} rows  [{time.time()-t0:.0f}s]", end="\r", flush=True)
    print(f"\n  scanned {n:,} track_tags rows in {time.time()-t0:.0f}s")

    counts = {}
    for ax, i, f in fams:
        arr = bits[(ax, i)]
        np.save(os.path.join(out, f"{ax}_{i}_{f}.npy"), arr)
        counts[f"{ax}/{f}"] = int(np.unpackbits(arr).sum())
    manifest = {"max_track_id": int(max_id), "nbytes": int(nbytes),
                "families": [{"axis": ax, "bit": i, "name": f} for ax, i, f in fams],
                "counts": counts, "built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    with open(os.path.join(out, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=1)
    for k, v in counts.items():
        print(f"  {k:28s} {v:>12,}")
    print(f"✓ wrote {len(fams)} bitmaps + manifest.json to {out}")


if __name__ == "__main__":
    main()
