"""Manual M4 check — loads backend/.env, calls the configured LLM provider, runs F1 fallback.

Run from the repo root (with the devshell/.venv active):
    python backend/test_llm.py

Shows: provider config, the RAW model output (or the exact API error), and the full
/search fallback (abstract query → LLM filters → real tracks). No key/quota → it prints
the provider error and F1 cleanly degrades to the rules path.
"""
import os
import pathlib
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent

# Load backend/.env into the environment BEFORE importing the engine (config reads env at import).
env_file = HERE / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

sys.path.insert(0, str(HERE))
from engine import llm, search  # noqa: E402

_KEY_VARS = ("SONIC_LLM_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")
_found = [k for k in _KEY_VARS if os.environ.get(k)]
print(f"provider={os.environ.get('SONIC_LLM_PROVIDER', '<unset>')}  "
      f"model={os.environ.get('SONIC_LLM_MODEL', '<default>')}  "
      f"keys_found={','.join(_found) if _found else 'NONE'}")
print("llm.available():", llm.available())

# 1) Raw provider call — surfaces the exact API error, or the raw JSON the model returned.
fn = llm._provider_fn()
if fn is not None:
    try:
        raw = fn("songs that feel like a warm summer sunset")
        print("\nRAW MODEL OUTPUT:\n", raw)
        print("PARSED PREDICATES:", llm.parse_response(raw))
    except Exception as e:
        print("\nPROVIDER ERROR:\n", e)

# 2) Full F1 fallback path: abstract queries (rules can't map these) → LLM → tracks.
for q in ["rainy 3am drive through tokyo",
          "songs that feel like a warm summer sunset",
          "epic cinematic music for a boss battle"]:
    t = time.time()
    r = search.semantic_search(q, limit=5)
    ms = (time.time() - t) * 1000
    print(f"\nq={q!r}  [{ms:.0f} ms]  source={r['source']}  filters={r['filters']}")
    for x in r["results"][:3]:
        print(f"   {x['track_id']:>10} pop={x['popularity']:>3}  "
              f"{str(x['title'])[:36]:36} {str(x['artists'])[:24]}")
