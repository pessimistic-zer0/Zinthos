"""Two-tier hydration — the expensive string/joins, done ONLY for the final result set.

Cheap numeric work (filter / re-rank) happens upstream on the compact track_search table;
this turns a small list of track_ids into display-ready records (titles, artists, art).
"""
from __future__ import annotations

from typing import Any

from . import db
from .textnorm import fold

# group_concat with an explicit ORDER BY needs SQLite ≥ 3.44; artist_position gives credited-ish order.
_HYDRATE_SQL = """
    SELECT t.track_id, t.title, t.popularity, t.release_date, t.preview_url,
           t.duration_ms, t.is_explicit, t.isrc,
           al.title AS album_title, al.cover_art_url,
           group_concat(ar.name, ', ') AS artists
    FROM tracks t
    LEFT JOIN albums al        ON al.album_id = t.album_id
    LEFT JOIN track_artists ta ON ta.track_id = t.track_id
    LEFT JOIN artists ar       ON ar.artist_id = ta.artist_id
    WHERE t.track_id IN ({ph})
    GROUP BY t.track_id
"""


# Set by init_preview(). A demo slice may store preview_url as the 20 varying bytes rather
# than the 107-char URL — see the COMPACTION note in demo/build_demo_slice.py.
_PREVIEW_TEMPLATE: str | None = None


def init_preview() -> str:
    """Load the preview-URL template, if this database packs them. Log line for startup.

    The template lives in the database (`demo_meta.preview_template`) rather than in code or
    an env var, so a bundle carries its own decoding rule and cannot be paired with the wrong
    one. master.db has no demo_meta and stores full URLs, so this is a no-op there.
    """
    global _PREVIEW_TEMPLATE
    _PREVIEW_TEMPLATE = None
    if not db.has_table("demo_meta"):
        return "preview_url stored in full"
    rows = db.query("SELECT value FROM demo_meta WHERE key = 'preview_template'")
    if not rows:
        return "preview_url stored in full"
    _PREVIEW_TEMPLATE = rows[0]["value"]
    return f"preview_url packed ({_PREVIEW_TEMPLATE[:40]}…) — rebuilding on hydrate"


def expand_preview(value: Any) -> str | None:
    """A packed preview_url back to a full URL; anything else through untouched.

    bytes → the template's {} filled with their hex. A str is already a URL (the builder
    leaves rows that did not match the template as they were), and None stays None. Being
    type-driven rather than flag-driven is what lets one table hold both.
    """
    if isinstance(value, (bytes, bytearray)) and _PREVIEW_TEMPLATE:
        return _PREVIEW_TEMPLATE.format(bytes(value).hex())
    return value


def hydrate(track_ids: list[int]) -> list[dict[str, Any]]:
    """Display records for the given ids, returned in the SAME order as requested."""
    if not track_ids:
        return []
    rows = db.query(_HYDRATE_SQL.format(ph=db.placeholders(len(track_ids))), track_ids)
    by_id = {r["track_id"]: dict(r) for r in rows}
    if _PREVIEW_TEMPLATE:
        for rec in by_id.values():
            rec["preview_url"] = expand_preview(rec["preview_url"])
    return [by_id[tid] for tid in track_ids if tid in by_id]


def hydrate_one(track_id: int) -> dict[str, Any] | None:
    out = hydrate([track_id])
    return out[0] if out else None


def dupe_key(r: dict[str, Any]) -> tuple[str, str]:
    """Collapse-key for catalog copies of one recording.

    Uses textnorm.fold — the SAME folding that builds track_match.norm_key, plus a dedupe-only
    strip of re-release tails — so the decorative variance the catalog is full of collapses
    instead of eating result slots: 'Mere Khayal Se Tum (From "Balmaa")' == 'Mere Khayal Se
    Tum - From "Balmaa"' == 'Mere Khayal Se Tum', "What's Done Is Done" == 'Whats Done Is Done'.

    Artists are folded but NOT reordered or subsetted, so copies credited to different SUBSETS
    of the same lineup ('Dilraj Kaur, Preeti Sagar' vs 'Dilraj Kaur, Om Prakash, Preeti Sagar')
    still read as distinct. Fixing that needs subset/overlap matching, which isn't an
    equivalence relation — a `seen` set can't express it. See the F6 notes.
    """
    return (fold(str(r.get("title", ""))), fold(str(r.get("artists", ""))))


DupeKey = tuple[str, str]


def dupe_keys(r: dict[str, Any]) -> tuple[DupeKey, ...]:
    """EVERY key under which this record counts as a copy: the folded (title, artists) AND its
    ISRC when it has one. Two records are copies if they share ANY key.

    Neither key alone is enough. The title key misses re-releases the fold can't see; the ISRC
    misses re-issues that were assigned a fresh code (Mere Khayal Se Tum sits on four ISRCs in
    this catalog) and the rows with no ISRC at all. The union catches what either one does.
    """
    keys: list[DupeKey] = [dupe_key(r)]
    isrc = r.get("isrc")
    if isrc:
        keys.append(("isrc", str(isrc).strip().upper()))
    return tuple(keys)


_VARIOUS = frozenset({"various artists", "various", "va"})


def _artist_set(r: dict[str, Any]) -> frozenset[str]:
    """Folded names of every credited artist (the hydrate join joins them with ', ').

    A leading 'the' is dropped so 'Dave Brubeck Quartet' meets 'The Dave Brubeck Quartet'. A
    'Various Artists' credit carries no artist information at all, so it is returned as the
    EMPTY set and matched by title alone (see _Seen.is_dupe) — a compilation copy of the seed
    is still a copy of the seed.
    """
    names = set()
    for a in str(r.get("artists") or "").split(", "):
        f = fold(a)
        if f.startswith("the "):
            f = f[4:]
        if f and f not in _VARIOUS:
            names.add(f)
    return frozenset(names)


def _credits_overlap(a: frozenset[str], b: frozenset[str]) -> bool:
    """Two artist credits name the same act: a shared name, or one name contained in another
    ('dave brubeck' in 'dave brubeck quartet'). Containment is checked on whole names, so
    'john' inside 'john denver' can't fire — only a full credited name can."""
    if a & b:
        return True
    return any(x in y or y in x for x in a for y in b)


class _Seen:
    """Dedupe state: exact keys (folded title+artists, ISRC) PLUS a per-title list of artist
    sets, so a copy credited to a SUBSET of the lineup ('Arijit Singh' vs 'Arijit Singh, Mithoon')
    is caught too. Subset overlap isn't an equivalence relation, so it can't live in a key set;
    it is a greedy scan against what has already been kept — bounded by k plus one chunk, so cheap.
    """

    def __init__(self) -> None:
        self.keys: set[DupeKey] = set()
        self.by_title: dict[str, list[frozenset[str]]] = {}

    def add(self, r: dict[str, Any]) -> None:
        self.keys.update(dupe_keys(r))
        title, artists = fold(str(r.get("title", ""))), _artist_set(r)
        self.by_title.setdefault(title, []).append(artists)

    def is_dupe(self, r: dict[str, Any]) -> bool:
        if any(k in self.keys for k in dupe_keys(r)):
            return True
        artists = _artist_set(r)
        kept_sets = self.by_title.get(fold(str(r.get("title", ""))), ())
        if not artists:                      # 'Various Artists': same title is enough
            return bool(kept_sets)
        return any(not kept or _credits_overlap(artists, kept) for kept in kept_sets)

    def check(self, r: dict[str, Any]) -> bool:
        """True if a copy of something kept; otherwise keeps it and returns False."""
        if self.is_dupe(r):
            return True
        self.add(r)
        return False


def dedupe(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop catalog duplicates (shared folded title/artists, shared ISRC, or same title with an
    overlapping artist credit), keeping the first (highest-ranked)."""
    seen = _Seen()
    return [r for r in records if not seen.check(r)]


def hydrate_top(track_ids: list[int], size: int, chunk: int = 150,
                exclude: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """First `size` deduped display records from a ranked candidate list, hydrating lazily.

    Dedupe needs the title/artist strings, so SOME over-hydration past `size` is inherent —
    but candidates must be hydrated in ranked chunks with an early stop, not all at once:
    the callers pass hundreds of FAISS hits (F7 recommend ≈ 800 for a real library) of
    which only `size` survive, and each hydrated row costs a 4-table join on the 162 GB DB.

    `exclude` pre-seeds the dedupe with RECORDS to suppress copies of — F6 passes the seed
    track's own record so no copy of the seed (same ISRC, same folded title+artists, or same
    title with a shared artist) comes back as "similar".
    """
    seen = _Seen()
    for r in exclude or ():
        seen.add(r)
    out: list[dict[str, Any]] = []
    for i in range(0, len(track_ids), chunk):
        for r in hydrate(track_ids[i:i + chunk]):
            if seen.check(r):
                continue
            out.append(r)
            if len(out) >= size:
                return out
    return out
