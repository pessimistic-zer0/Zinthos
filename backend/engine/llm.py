"""F1 LLM fallback — provider-agnostic abstract-query → feature-filter extraction.

Plug-and-play: the provider (Groq / Claude / Gemini / any OpenAI-compatible endpoint) is
chosen by SONIC_LLM_PROVIDER and lazily imported, so the engine has NO hard dependency on
any SDK. The active provider is Groq (llama-3.3-70b-versatile via .env). If nothing is
configured (or the key/SDK is missing), the fallback is simply disabled and F1 returns its
rules result — never a crash.

Security: the model only ever proposes (col, op, value) triples as JSON. They are parsed,
whitelisted, and bound as parameters by filters.build_where — the model never emits SQL.
"""
from __future__ import annotations

import json
import os
import re
import threading
from collections import OrderedDict
from collections.abc import Callable

from .config import CONFIG
from .filters import ALLOWED_COLS
from .rules import Predicate

# features stored 0–1000; tempo=BPM, loudness=dB, release_year=YYYY (not 0–1000 scaled)
_FEATURE_COLS = frozenset({
    "energy", "valence", "danceability", "acousticness",
    "instrumentalness", "speechiness", "liveness",
})

# Some API edges (Groq is behind Cloudflare) block the default "Python-urllib" UA → 403/1010.
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

SYSTEM_PROMPT = (
    "You translate a music-discovery query into audio-feature filters for a track database.\n"
    "Respond with ONLY a JSON object, no prose:\n"
    '  {"filters": [{"col": <name>, "op": <op>, "value": <number or [lo,hi]>}, ...]}\n'
    "Allowed col and scale:\n"
    "  energy, valence, danceability, acousticness, instrumentalness, speechiness, liveness\n"
    "      → integer 0-1000 (higher = more of that quality; valence = musical positivity)\n"
    "  tempo → BPM (e.g. 120)   loudness → dB, negative (e.g. -8)   release_year → e.g. 1995\n"
    'Allowed op: "<", ">", or "between" (value = [lo, hi]).\n'
    "Give 2-4 filters that capture the mood/vibe. Prefer \"between\" ranges over hard\n"
    "cutoffs so results aren't empty, and only include a feature you're confident about.\n"
    'Example — query "melancholy late-night rainy day":\n'
    '  {"filters": [{"col": "valence", "op": "between", "value": [100, 400]}, '
    '{"col": "energy", "op": "between", "value": [100, 450]}, '
    '{"col": "acousticness", "op": ">", "value": 500}]}\n'
    "Output JSON only."
)

DEFAULT_MODELS = {
    "anthropic": "claude-haiku-4-5-20251001",
    "gemini": "gemini-2.0-flash",
    "openai": "gpt-4o-mini",
    "groq": "llama-3.3-70b-versatile",
}

# ── provider plumbing (lazy, memoized) ───────────────────────────────────────────
_provider_lock = threading.Lock()
_provider: list[Callable[[str], str] | None] = []   # [callable] once resolved; [] until then


def _model() -> str:
    return CONFIG.llm_model or DEFAULT_MODELS.get(CONFIG.llm_provider.lower(), "")


def _make_provider() -> Callable[[str], str] | None:
    """Build a `complete(user_query) -> raw_text` callable with SYSTEM_PROMPT baked in."""
    name = CONFIG.llm_provider.lower()
    try:
        if name in ("anthropic", "claude"):
            import anthropic
            client = anthropic.Anthropic()                      # reads ANTHROPIC_API_KEY
            model, mx = _model(), CONFIG.llm_max_tokens

            def complete(user: str) -> str:
                msg = client.messages.create(
                    model=model, max_tokens=mx,
                    system=[{"type": "text", "text": SYSTEM_PROMPT,
                             "cache_control": {"type": "ephemeral"}}],   # cache the static instruction
                    messages=[{"role": "user", "content": user}],
                )
                return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
            return complete

        if name in ("gemini", "google"):
            # Direct REST over stdlib — NO SDK to install. Just set GEMINI_API_KEY.
            import urllib.error
            import urllib.request
            key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
            if not key:
                raise RuntimeError("GEMINI_API_KEY (or GOOGLE_API_KEY) not set")
            url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
                   f"{_model()}:generateContent")
            mx = CONFIG.llm_max_tokens

            def complete(user: str) -> str:
                body = json.dumps({
                    "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
                    "contents": [{"parts": [{"text": user}]}],
                    "generationConfig": {"response_mime_type": "application/json",
                                         "maxOutputTokens": mx},
                }).encode()
                req = urllib.request.Request(
                    url, data=body,
                    headers={"Content-Type": "application/json", "x-goog-api-key": key,
                             "User-Agent": _UA},
                )
                try:
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        data = json.loads(resp.read())
                except urllib.error.HTTPError as e:
                    raise RuntimeError(f"Gemini {e.code}: {e.read().decode('utf-8', 'replace')[:400]}")
                return data["candidates"][0]["content"]["parts"][0]["text"]
            return complete

        if name in ("openai", "openai-compatible", "groq"):
            # OpenAI-compatible chat/completions over stdlib REST — covers Groq, OpenAI,
            # OpenRouter, etc. NO SDK. Set SONIC_LLM_API_KEY (or GROQ_API_KEY/OPENAI_API_KEY).
            import urllib.error
            import urllib.request
            key = (os.environ.get("SONIC_LLM_API_KEY") or os.environ.get("GROQ_API_KEY")
                   or os.environ.get("OPENAI_API_KEY"))
            if not key:
                raise RuntimeError("set SONIC_LLM_API_KEY (or GROQ_API_KEY / OPENAI_API_KEY)")
            default_base = ("https://api.groq.com/openai/v1" if name == "groq"
                            else "https://api.openai.com/v1")
            base = (os.environ.get("SONIC_LLM_BASE_URL") or default_base).rstrip("/")
            url = f"{base}/chat/completions"
            model, mx = _model(), CONFIG.llm_max_tokens

            def complete(user: str) -> str:
                body = json.dumps({
                    "model": model, "max_tokens": mx, "temperature": 0,
                    "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                                 {"role": "user", "content": user}],
                }).encode()
                req = urllib.request.Request(url, data=body, headers={
                    "Content-Type": "application/json", "Authorization": f"Bearer {key}",
                    "User-Agent": _UA})
                try:
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        data = json.loads(resp.read())
                except urllib.error.HTTPError as e:
                    raise RuntimeError(f"LLM {e.code}: {e.read().decode('utf-8', 'replace')[:400]}")
                return data["choices"][0]["message"]["content"] or ""
            return complete
    except Exception as e:                                       # missing SDK/key/etc.
        print(f"[llm] provider '{name}' unavailable: {e}")
        return None

    if name:
        print(f"[llm] unknown SONIC_LLM_PROVIDER={name!r}; fallback disabled")
    return None


def _provider_fn() -> Callable[[str], str] | None:
    if not _provider:
        with _provider_lock:
            if not _provider:
                _provider.append(_make_provider() if CONFIG.llm_provider else None)
    return _provider[0]


# ── parsing / validation (provider-independent, unit-testable) ───────────────────
def _coerce_value(col: str, v: float) -> int:
    # tolerate a model that emits 0–1 for features instead of 0–1000
    if col in _FEATURE_COLS and 0 < abs(v) <= 1:
        v *= 1000
    return int(round(v))


def parse_response(raw: str) -> list[Predicate]:
    """Extract a validated, whitelisted predicate list from a model's raw text."""
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return []
    try:
        spec = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    out: list[Predicate] = []
    for f in spec.get("filters", []) if isinstance(spec, dict) else []:
        col, op, val = f.get("col"), f.get("op"), f.get("value")
        if col not in ALLOWED_COLS:
            continue
        if op == "between" and isinstance(val, (list, tuple)) and len(val) == 2 \
                and all(isinstance(x, (int, float)) for x in val):
            out.append((col, ">", _coerce_value(col, val[0])))
            out.append((col, "<", _coerce_value(col, val[1])))
        elif op in ("<", ">") and isinstance(val, (int, float)):
            out.append((col, op, _coerce_value(col, val)))
    return out


# ── public API with a small success-only LRU cache ───────────────────────────────
_cache: OrderedDict[str, list[Predicate]] = OrderedDict()
_cache_lock = threading.Lock()
_CACHE_MAX = 1024


def available() -> bool:
    return _provider_fn() is not None


def extract_filters(query: str) -> list[Predicate] | None:
    """Predicates for an abstract query, or None if the fallback is unavailable/unhelpful."""
    key = " ".join(query.lower().split())
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]

    fn = _provider_fn()
    if fn is None:
        return None
    try:
        preds = parse_response(fn(query))
    except Exception as e:
        print(f"[llm] call failed: {e}")
        return None
    if not preds:
        return None

    with _cache_lock:
        _cache[key] = preds
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)
    return preds
