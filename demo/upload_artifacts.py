"""
upload_artifacts.py — push a built demo slice to a Hugging Face dataset repo
═══════════════════════════════════════════════════════════════════════════════
The Space's 50 GB disk is NOT persistent: it is wiped on every rebuild and every wake from
sleep (a free Space sleeps after 48 hours without visitors). So the slice cannot live in the
Space — it has to be pulled at boot from somewhere durable, and a dataset repo is the free,
resumable, versioned place to put it.

WHY A DATASET REPO AND NOT THE SPACE REPO
  Files committed to the Space repo are pulled on every build too, so the cold-start cost is
  the same either way. The difference is what a change costs: rebuilding a slice would mean
  a new commit to the Space (and a full rebuild) instead of a new commit to the data. Keeping
  them apart means `python demo/upload_artifacts.py` and a Space restart, with no rebuild.

  It also keeps the Space repo small enough to read. The code is ~30 files; the data is a
  gigabyte.

PRIVACY
  --private makes the dataset repo private; the Space then needs an HF_TOKEN secret with read
  access to pull it. Public is simpler and is fine for derived features and ids, but the
  choice is yours — see the data note in demo/space_README.md.

Auth: `hf auth login`, or set HF_TOKEN.

Usage:
  python demo/upload_artifacts.py --repo you/zinthos-demo-slice
  python demo/upload_artifacts.py --repo you/zinthos-demo-slice --private
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DIST = os.environ.get("ZINTHOS_DEMO_DIR", os.path.join(HERE, "dist"))

# What the engine needs at runtime, and nothing else. Anything not matched here stays local —
# in particular the build's scratch files, so a stray 100 GB temp never gets pushed by accident.
ALLOW = ["demo.db", "demo.faiss", "demo_match.db", "manifest.json", "tag_bitmaps/*"]

REPO_CARD = """---
license: other
tags:
  - music
  - audio-features
  - zinthos
---

# Zinthos demo slice

Derived artifacts for the Zinthos demo Space: a subset of a ~255M-track catalogue with
audio-feature columns, 10-D learned embeddings, a FAISS index over them, and the lookup
sidecars the engine needs.

Built by `demo/build_demo_slice.py`. Not source data — features, ids and metadata only.
See `manifest.json` for the exact slice parameters and row counts.
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--repo", required=True, help="dataset repo id, e.g. you/zinthos-demo-slice")
    ap.add_argument("--dir", default=DIST, help=f"slice directory (default {DIST})")
    ap.add_argument("--private", action="store_true", help="create the repo private")
    ap.add_argument("--message", default="update demo slice", help="commit message")
    args = ap.parse_args()

    from huggingface_hub import HfApi

    db = os.path.join(args.dir, "demo.db")
    if not os.path.exists(db):
        sys.exit(f"{db} not found — run demo/build_demo_slice.py first")

    # Size only what ALLOW will actually send. Walking the whole directory counts the build
    # sidecars too (demo_vectors.f32 is 2.1 GB), which made the printed figure ~2.5 GB larger
    # than the upload — alarming when you are watching a 27 GB push.
    import fnmatch

    total, sending = 0, 0
    for root, _, files in os.walk(args.dir):
        for f in files:
            size = os.path.getsize(os.path.join(root, f))
            total += size
            rel = os.path.relpath(os.path.join(root, f), args.dir)
            if any(fnmatch.fnmatch(rel, pat) for pat in (*ALLOW, "README.md")):
                sending += size
    print(f"uploading {sending / 1e9:.2f} GB of {total / 1e9:.2f} GB in {args.dir} "
          f"→ dataset {args.repo}{' [private]' if args.private else ''}")
    print(f"  (the {(total - sending) / 1e9:.2f} GB left behind are build-only sidecars)")

    api = HfApi()
    api.create_repo(args.repo, repo_type="dataset", private=args.private, exist_ok=True)

    card = os.path.join(args.dir, "README.md")
    if not os.path.exists(card):
        with open(card, "w") as fh:
            fh.write(REPO_CARD)

    api.upload_folder(
        folder_path=args.dir,
        repo_id=args.repo,
        repo_type="dataset",
        commit_message=args.message,
        allow_patterns=[*ALLOW, "README.md"],
    )
    print(f"✓ https://huggingface.co/datasets/{args.repo}")
    print(f"  set ZINTHOS_DATASET={args.repo} in the Space's variables")


if __name__ == "__main__":
    main()
