"""
push_space.py — assemble and push the Hugging Face Space
═══════════════════════════════════════════════════════════════════════════════
Builds the Space's working tree in a temp directory and uploads it. Nothing is hand-copied
and nothing is committed to this repo, so the Space can never drift from `backend/engine`:
it is a copy taken at push time.

    <space>/
      README.md          ← demo/space_README.md   (the Space card; YAML front-matter sets sdk)
      app.py             ← demo/app.py
      requirements.txt   ← demo/requirements.txt
      engine/            ← backend/engine/        (the engine, verbatim)
      static/            ← a fresh VITE_DEMO=1 build of frontend/

WHY THE FRONT END IS REBUILT HERE
  The local `frontend/dist` is built WITHOUT VITE_DEMO, so it claims the full 255M catalogue
  and would say so on a Space that serves a slice. This builds with the flag set, into its
  own output directory, and leaves frontend/dist alone.

WHAT IS NOT PUSHED
  The slice itself. It goes to a dataset repo (demo/upload_artifacts.py) and is pulled at
  boot — see that file for why.

Auth: `hf auth login`, or set HF_TOKEN.

SPLITTING THE PORTAL OUT
  --split pushes the built portal to a separate STATIC space and drops it from this one. A
  static space never sleeps, so a cold visitor sees the landing page immediately while the
  engine space is still waking; without it they get the platform's "Space is starting" screen
  and nothing to look at. See push_static below.

Usage:
  python demo/push_space.py --repo you/zinthos
  python demo/push_space.py --repo you/zinthos --dataset you/zinthos-demo-slice
  python demo/push_space.py --repo you/zinthos --dataset you/slice --mount   # no boot pull
  python demo/push_space.py --repo you/zinthos --no-frontend   # skip the npm build
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any

from huggingface_hub.errors import HfHubHTTPError

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
FRONTEND = os.path.join(REPO, "frontend")

# Engine files that must not travel: caches, and anything that could carry a key.
ENGINE_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".env", "*.db", "*.faiss")


def space_origin(api: Any, repo_id: str, override: str = "") -> str:
    """The URL a Space is actually served from, read from the Hub.

    DO NOT derive this. The obvious rule — owner/name → https://owner-name.hf.space — is right
    for a Gradio Space and WRONG for a static one, which gets an extra segment:
    https://owner-name.static.hf.space. Guessing produced a CORS allowlist entry for a
    hostname that does not exist, and a wrong allowlist fails only in the browser console,
    where nothing in a deploy script will ever see it.

    `space_info(...).host` is authoritative and populated as soon as the repo exists. The
    derived form survives only as a last-resort fallback, and says so.
    """
    if override:
        return override.rstrip("/")
    try:
        host = api.space_info(repo_id).host
        if host:
            return str(host).rstrip("/")
    except Exception as e:  # noqa: BLE001 — falling back is better than failing the push
        print(f"  ! could not read {repo_id}'s host from the Hub ({type(e).__name__})")
    slug = re.sub(r"[^a-z0-9]+", "-", repo_id.lower()).strip("-")
    guess = f"https://{slug}.hf.space"
    print(f"  ! falling back to a DERIVED url for {repo_id}: {guess}")
    print("    a static Space is served from <name>.static.hf.space — verify and use --origin")
    return guess


def build_frontend(dest: str, engine_base: str = "") -> None:
    """`vite build` with VITE_DEMO=1 into `dest`.

    `engine_base` is set only for a split deploy: api.ts falls back to the same-origin `/api`
    when it is empty, which is what the single-Space layout wants.
    """
    if not os.path.isdir(os.path.join(FRONTEND, "node_modules")):
        sys.exit(f"{FRONTEND}/node_modules missing — run `npm install` in frontend/ first")
    env = {**os.environ, "VITE_DEMO": "1"}
    if engine_base:
        env["VITE_ENGINE_BASE"] = engine_base
        print(f"building the portal (VITE_DEMO=1, engine at {engine_base}) …")
    else:
        print("building the portal (VITE_DEMO=1) …")
    subprocess.run(
        ["npx", "vite", "build", "--outDir", dest, "--emptyOutDir"],
        cwd=FRONTEND, env=env, check=True,
    )


STATIC_CARD = """---
title: {title}
emoji: 🎧
colorFrom: purple
colorTo: indigo
sdk: static
app_file: index.html
pinned: false
---

The Zinthos portal. The engine it talks to lives at {engine}.

Served from a static Space on purpose: a static Space never sleeps, so the landing page is
up instantly and the engine Space is woken by the portal's own first `/health` call while the
visitor is still looking at it.
"""


# The Hub validates a Space card's YAML server-side and rejects the whole upload on a
# mismatch — after the repo, its variables and its volumes have already been created. These
# two rules cost a round trip each to discover, so they are checked here first.
#
#   emoji  must match /\p{Extended_Pictographic}/u. U+1F702 ALCHEMICAL SYMBOL FOR FIRE looks
#          like an emoji and sits inside the 1F000-1FAFF range, but the Alchemical Symbols
#          block is NOT pictographic — hence the conservative allowlist rather than a range.
#   short_description  <= 60 characters.
_PICTOGRAPHIC = ((0x1F300, 0x1F5FF), (0x1F600, 0x1F64F), (0x1F680, 0x1F6FF),
                 (0x1F900, 0x1F9FF), (0x1FA70, 0x1FAFF), (0x2600, 0x26FF), (0x2700, 0x27BF))


def check_card(text: str, where: str) -> None:
    """Fail fast on the Space-card rules the Hub enforces, before anything is created."""
    for line in text.split("\n"):
        if line.startswith("emoji:"):
            ch = line.split(":", 1)[1].strip()
            cp = ord(ch[0]) if ch else 0
            if not any(lo <= cp <= hi for lo, hi in _PICTOGRAPHIC):
                sys.exit(f"{where}: emoji {ch!r} (U+{cp:04X}) is not Extended_Pictographic — "
                         f"the Hub will reject the upload. Use a plain emoji such as 🎧.")
        if line.startswith("short_description:"):
            desc = line.split(":", 1)[1].strip()
            if len(desc) > 60:
                sys.exit(f"{where}: short_description is {len(desc)} characters, "
                         f"the Hub allows 60.")


def push_static(api: Any, repo: str, engine_repo: str, origin_override: str,
                message: str) -> str:
    """Build the portal against the engine Space's URL and push it to a static Space.

    WHY SPLIT AT ALL
      Served from inside the engine Space, the portal is only reachable once that Space is
      awake — so a cold visitor stares at the platform's "Space is starting" screen with
      nothing to look at. A static Space is always on: the portal paints immediately, and
      frontend/src/lib/demo.ts calls /health on mount, which is what wakes the engine. The
      rift animation and the About page then cover the wait instead of a loading screen.
    """
    engine = space_origin(api, engine_repo, origin_override)
    # Created FIRST so its real host can be read back before anything depends on it.
    api.create_repo(repo, repo_type="space", space_sdk="static", exist_ok=True)
    work = tempfile.mkdtemp(prefix="zinthos-portal-")
    try:
        build_frontend(work, engine_base=f"{engine}/api")
        title = repo.split("/")[-1]
        card = STATIC_CARD.format(title=title, engine=engine)
        check_card(card, "the portal Space card (STATIC_CARD)")
        with open(os.path.join(work, "README.md"), "w") as fh:
            fh.write(card)
        api.upload_folder(folder_path=work, repo_id=repo, repo_type="space",
                          commit_message=message)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    portal = space_origin(api, repo)
    print(f"✓ portal → {portal}")
    return portal


def assemble(work: str, frontend: bool) -> None:
    check_card(open(os.path.join(HERE, "space_README.md")).read(), "demo/space_README.md")
    shutil.copy2(os.path.join(HERE, "space_README.md"), os.path.join(work, "README.md"))
    shutil.copy2(os.path.join(HERE, "app.py"), os.path.join(work, "app.py"))
    shutil.copy2(os.path.join(HERE, "requirements.txt"), os.path.join(work, "requirements.txt"))
    shutil.copytree(os.path.join(REPO, "backend", "engine"), os.path.join(work, "engine"),
                    ignore=ENGINE_IGNORE)
    if frontend:
        build_frontend(os.path.join(work, "static"))

    # A .env inside engine/ would be a key in a public repo. The ignore above already drops
    # it; this is the assertion that says so out loud rather than trusting the pattern.
    for root, _, files in os.walk(work):
        for f in files:
            if f == ".env" or f.endswith((".db", ".faiss")):
                sys.exit(f"refusing to push: {os.path.join(root, f)}")

    total = sum(os.path.getsize(os.path.join(r, f))
                for r, _, fs in os.walk(work) for f in fs)
    print(f"  assembled {total / 1e6:.1f} MB")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--repo", required=True, help="space repo id, e.g. you/zinthos")
    ap.add_argument("--dataset", default="", help="dataset repo holding the slice "
                                                  "(sets ZINTHOS_DATASET on the Space)")
    ap.add_argument("--no-frontend", action="store_true", help="skip the vite build")
    ap.add_argument("--mount", action="store_true",
                    help="mount the dataset as a read-only volume at /data instead of "
                         "downloading it at boot (needs --dataset)")
    ap.add_argument("--mount-path", default="/data", help="where to mount it (default /data)")
    ap.add_argument("--split", metavar="REPO", default="",
                    help="serve the portal from a separate STATIC space (e.g. you/zinthos-portal) "
                         "instead of from inside the engine space. A static space never sleeps, "
                         "so the landing page is up instantly and its own /health call wakes the "
                         "engine while the visitor is still looking at it")
    ap.add_argument("--hardware", default="zero-a10g",
                    help="Space hardware, set AT CREATION (default zero-a10g). A free account "
                         "cannot create a Gradio Space on cpu-basic — that returns 402 — so "
                         "ZeroGPU is the free path, not an upgrade applied afterwards")
    ap.add_argument("--portal-only", action="store_true",
                    help="push ONLY the static portal, leaving the engine Space untouched. "
                         "Any write to the engine Space restarts it, and a restart re-pulls "
                         "the whole slice — minutes of downtime for a CSS change")
    ap.add_argument("--origin", default="",
                    help="override the engine space's public URL (default https://<owner>-<name>.hf.space)")
    ap.add_argument("--message", default="deploy zinthos demo", help="commit message")
    ap.add_argument("--keep", action="store_true", help="leave the assembled tree on disk")
    args = ap.parse_args()

    from huggingface_hub import HfApi

    api = HfApi()

    if args.portal_only:
        if not args.split:
            sys.exit("--portal-only needs --split: that is the repo the portal lives in")
        push_static(api, args.split, args.repo, args.origin, args.message)
        print("  engine Space untouched (no restart, no re-download)")
        return

    work = tempfile.mkdtemp(prefix="zinthos-space-")
    try:
        # With --split the portal is pushed separately, so the engine space ships no static/.
        assemble(work, not args.no_frontend and not args.split)
        print(f"pushing → space {args.repo} ({args.hardware})")
        # HARDWARE IS SET AT CREATION, NOT AFTER.
        #   A Gradio Space on the default cpu-basic requires PRO — creating one on a free
        #   account returns 402 Payment Required, with "hosting Gradio and Docker Spaces on
        #   free cpu-basic requires a PRO subscription". The free allowance is specifically
        #   ZeroGPU (up to 2 per personal account in good standing), and it has to be asked
        #   for up front: there is no create-then-upgrade path on a free account, because the
        #   create itself is what gets refused.
        try:
            api.create_repo(args.repo, repo_type="space", space_sdk="gradio",
                            space_hardware=args.hardware, exist_ok=True)
        except HfHubHTTPError as e:
            if "402" not in str(e):
                raise
            sys.exit(
                f"\nthe Hub refused to create {args.repo} on {args.hardware}:\n  {e}\n\n"
                "On a free account the only Gradio hardware is ZeroGPU, limited to 2 Spaces.\n"
                "Check https://huggingface.co/spaces for existing ZeroGPU Spaces on this\n"
                "account and delete one, or pass --hardware with what your plan allows."
            )
        if args.dataset:
            api.add_space_variable(args.repo, "ZINTHOS_DATASET", args.dataset)
            print(f"  set ZINTHOS_DATASET={args.dataset}")
        if args.mount:
            if not args.dataset:
                sys.exit("--mount needs --dataset: there has to be something to mount")
            # A mounted repo is a directory in the container, so the slice never touches the
            # Space's ephemeral disk and nothing is re-pulled when it wakes from sleep. The
            # open question is READ LATENCY: this engine does random 4 KB SQLite reads and
            # mmaps the index, which is the worst case for network-backed storage. Both paths
            # ship so the live Space can settle it — see demo/README.md.
            from huggingface_hub import Volume

            api.set_space_volumes(args.repo, volumes=[
                Volume(type="dataset", source=args.dataset,
                       mount_path=args.mount_path, read_only=True),
            ])
            api.add_space_variable(args.repo, "ZINTHOS_ARTIFACTS", args.mount_path)
            # A mounted volume is read-only network storage. SQLite's own mmap and its POSIX
            # locking both assume a local filesystem; on the mount they produce
            # "disk I/O error" before a single query runs. Set as Space VARIABLES rather than
            # app.py defaults so the download path keeps its mmap, where mmap is free.
            api.add_space_variable(args.repo, "SONIC_SQLITE_IMMUTABLE", "1")
            api.add_space_variable(args.repo, "SONIC_SQLITE_MMAP", "0")
            print("  set SONIC_SQLITE_IMMUTABLE=1 SONIC_SQLITE_MMAP=0 (read-only mount)")
            print(f"  mounted dataset {args.dataset} read-only at {args.mount_path}")
            print(f"  set ZINTHOS_ARTIFACTS={args.mount_path} (no boot download)")
        if not args.mount and args.dataset:
            try:
                if api.get_space_runtime(args.repo).volumes:
                    api.delete_space_volumes(args.repo)
                # /tmp, not /data: with the volume gone /data is no longer a mount point and
                # the container may not be able to create it. /tmp is always writable and lives
                # on the same 50 GB ephemeral disk.
                api.add_space_variable(args.repo, "ZINTHOS_ARTIFACTS", "/tmp/zinthos-slice")
                # The download path DOES want SQLite's mmap — the file is local then — and has
                # no locking problem, so both mount workarounds come back off.
                api.add_space_variable(args.repo, "SONIC_SQLITE_MMAP", "34000000000")
                api.add_space_variable(args.repo, "SONIC_SQLITE_IMMUTABLE", "0")
                print("  cleared any mounted volume — using the boot download")
                print("  NOTE: a private dataset needs an HF_TOKEN read secret to download")
            except Exception as e:  # noqa: BLE001
                print(f"  ! could not clear volumes ({type(e).__name__}: {e})")
        api.upload_folder(folder_path=work, repo_id=args.repo, repo_type="space",
                          commit_message=args.message,
                          # A previous non-split push left static/ in the engine space; leaving
                          # it there would serve a stale portal at / alongside the new one.
                          delete_patterns=["static/*"] if args.split else None)
        print(f"✓ engine → https://huggingface.co/spaces/{args.repo}")

        if args.split:
            portal = push_static(api, args.split, args.repo, args.origin, args.message)
            # The browser now calls the engine cross-origin, so the allowlist stops being a
            # safety net and becomes load-bearing. config.py parses this comma-separated.
            api.add_space_variable(args.repo, "SONIC_CORS", portal)
            api.add_space_variable(args.repo, "ZINTHOS_PORTAL", portal)
            print(f"  set SONIC_CORS={portal} on the engine space")
        print("  remaining manual steps (Hub settings — see demo/README.md):")
        print("    • secrets  → HF_TOKEN (read; the dataset is private)")
        print("    • secrets  → SONIC_LLM_PROVIDER, GROQ_API_KEY, SONIC_LLM_MODEL "
              "(for the LLM fallback)")
    finally:
        if args.keep:
            print(f"  assembled tree left at {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
