"""Fold the 768 raw `artist_genres` tags into coarse FAMILIES for the F6 re-rank.

WHY THIS EXISTS
    The 20-class taxonomy the SAE was supervised on (`genres` table) has a `metal` class but no
    Indian one — all of Asia is the single label `asian`. So the 10-D space was never trained to
    separate Hindi from J-pop, and a Bollywood seed returns Punjabi, Bhojpuri, Tamil, devotional
    and jazz neighbours. `artist_genres` DOES carry that distinction (bollywood, ghazal, qawwali,
    bhajan, punjabi pop, …) — 768 tags over 2.23M artist rows — but the pipeline throws it away.

WHY FAMILIES AND NOT RAW TAGS
    Exact tag-string equality is far too strict. Measured on the live index, a `Chura Ke Dil Mera`
    pool of 1499 candidates shares an exact tag with the seed only 84 times (6%) — Kumar Sanu is
    `bollywood`, his neighbours are `bhajan`/`ghazal`/`punjabi pop`, all correctly South Asian and
    none literally `bollywood`. Folded to families that becomes 467 (31%), a 5.6x improvement.

TWO AXES, NOT ONE
    The two failure modes are different in kind and want different answers:
      REGION  fixes the Hindi problem — `bollywood` and `bhajan` are interchangeable in a
              similar-list even though they sound quite different.
      SONIC   fixes the Raining Blood problem — its predicted genre_id is `rock`, but 1027 of
              its 1499 neighbours are metal, so the existing W_GENRE term rewards the wrong 294.
    A tag can carry both (`punjabi hip hop` -> south_asian + hip_hop) and that is the point: two
    Punjabi rap tracks agree on both axes, a Punjabi rap track and a Punjabi devotional track
    agree on one. That graded agreement is the signal.

CALIBRATION WARNING (learned the hard way — see similar._normalize_sim)
    A weight is not an influence: influence is weight x SPREAD across the candidate pool. Exact-tag
    Jaccard has a pool mean of 0.014-0.106, so a 0.20-weighted Jaccard term would move a typical
    candidate by ~0.006 and be decorative. Family agreement is binary per axis, uses the full 0-1
    range, and so actually earns its weight. Weight the axes; keep Jaccard as a tiebreaker if at all.

REVIEW NOTES
    Judgement calls are marked `# CALL:` below — they are the rows most worth arguing with.
    Rules are substring matches over the lowercased tag; OVERRIDES win outright and exist for the
    traps ('rocksteady' is not rock, 'deathstep' is not death metal).
"""
from __future__ import annotations

# ── REGION axis ───────────────────────────────────────────────────────────────────────────────
# Cultural/linguistic provenance. This is the axis that fixes the Hindi problem.
REGION_RULES: dict[str, tuple[str, ...]] = {
    "south_asian": (
        "bollywood", "hindi", "desi", "bhojpuri", "bhojiwood", "punjabi", "bhangra", "tamil",
        "telugu", "kannada", "malayalam", "marathi", "gujarati", "haryanvi", "bangla", "bengali",
        "indian", "carnatic", "hindustani", "tollywood", "kollywood", "mollywood", "sandalwood",
        "bhajan", "ghazal", "qawwal", "garba",
    ),
    "east_asian": (
        "j-pop", "j-rock", "j-rap", "j-r&b", "j-dance", "japanese", "anime", "vocaloid",
        "kayokyoku", "shibuya-kei", "visual kei", "city pop", "enka", "k-pop", "k-rap", "k-rock",
        "k-ballad", "korean", "c-pop", "cantopop", "mandopop", "taiwanese", "chinese", "gufeng",
    ),
    "southeast_asian": (
        "thai", "luk thung", "phleng phuea chiwit", "morlam", "malay", "indonesian", "lagu ",
        "koplo", "dangdut", "funkot", "budots", "batak", "maluku", "hipdut", "pinoy", "opm",
        "kundiman", "harana", "bisrock", "v-pop", "vinahouse", "vietnam", "t-pop", "p-pop",
        "pop kreatif", "singeli",
    ),
    "mena": (  # Middle East + North Africa
        "arabesk", "arabic", "khaleeji", "mizrahi", "raï", "gnawa", "moroccan", "egyptian",
        "mahraganat", "turkish", "oyun havasi", "karadeniz", "anatolian", "algerian", "sufi",
    ),
    "african": (
        "afrobeat", "afropop", "afro house", "afro soul", "afropiano", "afroswing", "afro tech",
        "afro r&b", "afro adura", "amapiano", "gqom", "kuduro", "highlife", "hiplife", "azonto",
        "bongo flava", "gengetone", "asakaa", "ndombolo", "rumba congolaise", "coupé décalé",
        "bikutsi", "maskandi", "african gospel", "fújì", "ghanaian", "nigerian", "rap ivoire",
        "bacardi", "latin afrobeat",
    ),
    "brazilian": (
        "brazilian", "forró", "forro ", "sertanejo", "pagode", "axé", "brega", "funk carioca",
        "funk de bh", "funk melody", "funk consciente", "funk bruxaria", "mpb", "bossa nova",
        "samba", "piseiro", "arrocha", "seresta", "agronejo", "tecnobrega", "trap funk",
    ),
    "latin": (
        "latin", "reggaeton", "cumbia", "salsa", "bachata", "merengue", "mambo", "timba",
        "son cubano", "son jarocho", "bolero", "vallenato", "tejano", "norteño", "banda",
        "mariachi", "ranchera", "corrido", "grupera", "duranguense", "sierreño", "cuarteto",
        "chamamé", "huayno", "chicha", "trova", "guaracha", "punto guajiro", "candombe", "murga",
        "folklore argentino", "folclor", "argentine", "chilean", "mexican", "colombian",
        "rock en español", "rock urbano", "urbano latino", "turreo", "rkt", "neoperreo", "dembow",
        "techengue", "electrocumbia", "música mexicana", "spanish-language reggae", "cha cha cha",
        "tropical music", "pop urbano", "pop urbaine", "champeta", "punta", "flamenco",
    ),
    "caribbean": (
        "reggae", "rocksteady", "ska", "dancehall", "ragga", "riddim", "soca", "calypso", "zouk",
        "kompa", "lovers rock", "shatta", "dub",
    ),
    "european": (
        "schlager", "dansband", "dansktop", "epadunk", "russelåter", "iskelmä", "finnish",
        "swedish", "norwegian", "norsk", "dansk", "neue deutsche welle", "german", "nederpop",
        "hollands", "disco polo", "manele", "neomelodico", "canzone napoletana", "italian",
        "french", "variété", "chanson", "europop", "britpop", "madchester", "laïko", "entehno",
        "fado", "indorock", "québéc", "quebecois", "celtic", "polka", "sea shanties",
    ),
}

# ── SONIC axis ────────────────────────────────────────────────────────────────────────────────
# How it sounds. This is the axis that fixes the mislabelled-seed problem (Raining Blood).
SONIC_RULES: dict[str, tuple[str, ...]] = {
    "metal": ("metal", "metalcore", "deathcore", "djent", "grindcore", "mathcore"),
    "punk": (
        "punk", "screamo", "post-hardcore", "melodic hardcore", "emocore", "emo", "hardcore punk",
        "psychobilly", "deathrock", "riot grrrl", "queercore", "anti-folk",
    ),
    "rock": (
        "rock", "grunge", "post-grunge", "shoegaze", "krautrock", "britpop", "madchester",
        "rockabilly", "new wave", "no wave", "jangle pop", "power pop", "indorock",
    ),
    "hip_hop": (
        "hip hop", "rap", "trap", "drill", "grime", "phonk", "boom bap", "g-funk", "crunk",
        "hyphy", "bounce", "horrorcore", "nerdcore", "hiplife", "asakaa", "mahraganat",
        # Urbano is rap-adjacent by measurement, not by taxonomy: 19% of `reggaeton` artists also
        # carry trap latino / latin hip hop. Without this the whole reggaeton family — 3 tags —
        # would carry a region and NO sonic family at all.
        "reggaeton", "perreo",
    ),
    "electronic": (
        "house", "techno", "trance", "dubstep", "drum and bass", "liquid funk", "jungle",
        "garage", "hardstyle", "gabber", "speedcore", "frenchcore", "breakcore", "happy hardcore",
        "hardcore techno", "idm", "breakbeat", "big beat", "edm", "electro", "synthwave",
        "vaporwave", "chillwave", "witch house", "glitch", "downtempo", "future bass",
        "melodic bass", "bass music", "bassline", "moombahton", "eurodance", "italo dance",
        "nightcore", "3 step", "drumstep", "chillstep", "deathstep", "footwork", "jersey club",
        "baltimore club", "philly club", "hypertechno", "melbourne bounce", "cedm", "big room",
        "tekno", "dub techno", "ebm", "darkwave", "cold wave", "electroclash", "hi-nrg",
        "psytrance", "trip hop", "lo-fi beats", "lo-fi hip hop", "vinahouse", "funkot", "budots",
    ),
    "ambient_experimental": (
        "ambient", "drone", "experimental", "avant-garde", "noise", "minimalism", "new age",
        "space music", "slowcore", "post-rock", "math rock",
    ),
    "jazz": (
        "jazz", "bebop", "hard bop", "cool jazz", "swing music", "big band", "ragtime",
        "boogie-woogie", "lounge", "exotica", "adult standards",
    ),
    "classical": (
        "classical", "opera", "choral", "chamber music", "orchestra", "orchestral", "neoclassical",
        "medieval", "gregorian chant", "requiem", "villancicos",
    ),
    "country_folk": (
        "country", "bluegrass", "newgrass", "honky tonk", "americana", "red dirt", "folk",
        "singer-songwriter", "cajun", "zydeco", "sea shanties", "traditional music", "celtic",
        "polka", "schlager", "dansband", "iskelmä", "trova", "chanson", "variété",
    ),
    "blues_soul_rnb": (
        "blues", "soul", "r&b", "motown", "funk", "go-go", "boogie", "disco", "quiet storm",
        "new jack swing", "freestyle",
    ),
    "religious": (
        "gospel", "christian", "ccm", "worship", "pentecostal", "bhajan", "devotional", "qawwal",
        "sholawat", "gregorian chant", "requiem", "villancicos", "rap chrétien", "sufi",
        "evangelical",
    ),
    "pop": (
        "pop", "idol", "bubblegum", "dance pop", "electropop", "synthpop", "art pop",
        "chamber pop", "baroque pop", "dream pop", "bedroom pop", "hyperpop",
    ),
    "functional": (  # not a style so much as a use — kids, comedy, screen, holiday
        "children's music", "kids", "lullaby", "comedy", "spoken word", "musicals", "soundtrack",
        "christmas", "vgm", "easy listening", "yacht rock", "aor", "meme rap",
    ),
}

# ── OVERRIDES ─────────────────────────────────────────────────────────────────────────────────
# Exact tag -> its complete family set. These WIN over the rules and exist for the traps: tags
# whose spelling points at the wrong family. Every one of these is a bug the rules would create.
OVERRIDES: dict[str, frozenset[str]] = {
    # "rock" in the name, not rock music.
    "rocksteady":       frozenset({"caribbean"}),
    "rock and roll":    frozenset({"rock"}),
    # "death"/"core" traps.
    "deathstep":        frozenset({"electronic"}),                    # dubstep, not death metal
    "deathrock":        frozenset({"punk", "rock"}),                  # goth-punk, not metal
    # SETTLED: bare `hardcore` is 76% electronic here (gabber 19%, frenchcore 17%, speedcore 14%)
    # and 42% punk (hardcore punk 33%). It is honestly both, so it carries both.
    "hardcore":         frozenset({"electronic", "punk"}),
    "hardcore techno":  frozenset({"electronic"}),
    "hardcore hip hop": frozenset({"hip_hop"}),
    # "house"/"club" that aren't dance music, and dance music that hides it.
    "rally house":      frozenset({"electronic"}),
    "stutter house":    frozenset({"electronic"}),
    # SETTLED BY CO-OCCURRENCE: of the 11,525 `devotional` artists, 37% also carry `bhajan` and
    # only ~1% carry gospel/evangelical — 54% land in south_asian vs 42% religious. The ambiguity
    # I worried about isn't there in THIS catalog.
    "devotional":       frozenset({"religious", "south_asian"}),
    "sufi":             frozenset({"religious", "mena", "south_asian"}),
    "bhajan":           frozenset({"religious", "south_asian"}),
    "qawwali":          frozenset({"religious", "south_asian", "mena"}),
    "sholawat":         frozenset({"religious", "southeast_asian"}),
    # Iberian, not Latin American — but groups with Spanish-language music for retrieval.
    "flamenco":         frozenset({"latin", "country_folk"}),         # CALL: see docstring
    "flamenco pop":     frozenset({"latin", "pop"}),
    "flamenco urbano":  frozenset({"latin", "hip_hop"}),
    "fado":             frozenset({"european", "country_folk"}),
    # Bare umbrella tags: too generic to imply an axis they don't state.
    "traditional music": frozenset({"country_folk"}),
    "traditional folk":  frozenset({"country_folk"}),
    # DELIBERATELY UNMAPPED. Bare `indie` (154 artists) splits southeast_asian 31% / rock 24% /
    # pop 21% — its top co-tag is `indonesian indie` (14%). Asserting either axis would be wrong
    # for most of it, and at 154 artists the cost of mapping nothing is nil. Empty != forgotten.
    "indie":             frozenset(),
    "alternative dance": frozenset({"electronic", "rock"}),
    # Regional hip-hop/rock that the region rules catch but the sonic rules would miss or vice versa.
    "anime rap":        frozenset({"east_asian", "hip_hop", "functional"}),
    "japanese vgm":     frozenset({"east_asian", "functional"}),
    "kagok":            frozenset({"east_asian", "classical"}),       # CALL: 11 artists, too thin
    # NOT classical piano. 82% of these artists also carry `amapiano` and 45% `gqom` — this is
    # South African house, and the tag name is a trap. Caught only by measuring co-occurrence.
    "private school piano": frozenset({"african", "electronic"}),
    # `rockabilly` also sits on r&b: doo-wop 4%, boogie-woogie 3%, boogie 1% (psychobilly 25% is
    # the punk DERIVATIVE, so punk stays off the parent tag).
    "rockabilly":       frozenset({"rock", "country_folk", "blues_soul_rnb"}),
    "native american music": frozenset({"country_folk"}),
    "gothic country":   frozenset({"country_folk", "rock"}),
    "southern gothic":  frozenset({"country_folk", "rock"}),
    "dub":              frozenset({"caribbean", "electronic"}),
    "dub techno":       frozenset({"electronic"}),
    "ska punk":         frozenset({"caribbean", "punk"}),
    "reggae rock":      frozenset({"caribbean", "rock"}),
    "nz reggae":        frozenset({"caribbean"}),
    "punta":            frozenset({"latin", "caribbean"}),
    "champeta":         frozenset({"latin", "african"}),
    # Substring collisions the rules get wrong.
    "medieval metal":   frozenset({"metal"}),                         # not classical
    "electro swing":    frozenset({"electronic", "jazz"}),            # 'swing music' misses it
    # Tags no rule reaches. Each is a judgement call; these are the rows to argue with first.
    "tango":            frozenset({"latin", "country_folk"}),
    "industrial":       frozenset({"electronic", "rock"}),            # 99% electronic, 19% rock
    # SETTLED: `lo-fi` is 40% electronic (lo-fi beats 39%) and 15% jazz (jazz beats 12%) —
    # instrumental beat music, not lo-fi guitars. hip_hop is only 3%, so it does NOT get that.
    "lo-fi":            frozenset({"electronic", "jazz"}),
    "kizomba":          frozenset({"african"}),                       # Angolan, zouk-derived
    "indie dance":      frozenset({"electronic", "rock"}),
    "doo-wop":          frozenset({"blues_soul_rnb"}),
    "miami bass":       frozenset({"hip_hop", "electronic"}),
    "alté":             frozenset({"african"}),                       # Nigerian alternative
    # SETTLED: no family dominates (country_folk 17 / rock 15 / electronic 13 / jazz 9), but the
    # co-tags are gothic country, folk punk, neofolk, swing music, ragtime, electro swing —
    # theatrical folk-jazz, not rock. My original `rock` guess had nothing behind it.
    "dark cabaret":     frozenset({"country_folk", "jazz"}),
    "new rave":         frozenset({"electronic", "rock"}),
    "ballroom vogue":   frozenset({"electronic"}),
    "jam band":         frozenset({"rock"}),
    "neo-psychedelic":  frozenset({"rock"}),
    "lo-fi indie":      frozenset({"rock"}),
    "dance":            frozenset({"electronic"}),
}

# Patterns that must NOT fire a family even though a rule substring matches. Narrower than an
# OVERRIDE: it suppresses one family while leaving every other rule for that tag intact.
BLOCK: dict[str, tuple[str, ...]] = {
    # "pop" is inside dozens of tags where it is the noun, not the style ("pop punk" is punk-pop,
    # fine; "psychedelic pop" fine) — but these read as another style entirely.
    "pop": ("pop punk", "power pop", "pop rock", "pop rap", "pop worship"),
    # "folk" inside "folk metal"/"folk punk" is a modifier on the sonic family, not the family.
    "country_folk": ("folk metal", "folk punk"),
    # "rock" appears in these but they are not rock records.
    "rock": ("rocksteady",),
    # 'trap' is hip-hop, not a dance genre; 'lo-fi indie' is guitars.
    "electronic": ("trap", "lo-fi indie"),
    # Substring collisions: 'ska' inside skate punk / maskandi, 'dub' inside dubstep.
    # 'ska' inside skate punk / maskandi, 'dub' inside dubstep, and 'reggae' inside
    # reggaeton* — the last one measured 52% latin / 19% hip_hop / only 6% caribbean.
    "caribbean": ("skate punk", "maskandi", "dubstep", "reggaeton"),
}


def _matches(tag: str, patterns: tuple[str, ...]) -> bool:
    return any(p in tag for p in patterns)


def families(tag: str) -> frozenset[str]:
    """Every family one raw tag belongs to, across BOTH axes. Empty set = unmapped."""
    t = tag.strip().lower()
    if t in OVERRIDES:
        return OVERRIDES[t]
    out: set[str] = set()
    for axis in (REGION_RULES, SONIC_RULES):
        for fam, pats in axis.items():
            if _matches(t, pats) and not _matches(t, BLOCK.get(fam, ())):
                out.add(fam)
    return frozenset(out)


def region_families(tag: str) -> frozenset[str]:
    return families(tag) & REGION_RULES.keys()


def sonic_families(tag: str) -> frozenset[str]:
    return families(tag) & SONIC_RULES.keys()


ALL_FAMILIES: tuple[str, ...] = (*REGION_RULES, *SONIC_RULES)
