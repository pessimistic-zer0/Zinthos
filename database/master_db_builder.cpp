// ════════════════════════════════════════════════════════════════════════════
//  ZINTHOS — master.db ETL builder

//  Build:   see ./CMakeLists.txt   (or)
//    g++ -O3 -std=c++17 master_db_builder.cpp -o master_db_builder \
//        $(pkg-config --cflags --libs sqlite3)
//
//  Run:
//    ./master_db_builder
// ════════════════════════════════════════════════════════════════════════════
//  SCHEMA PARSING — how schema.sql becomes the DDL struct
//
//  schema.sql (text file)
//      │
//      ▼ readFile()
//  raw text string                        -- slurp entire file into memory as-is
//      │
//      ▼ stripLineComments()
//  clean text                             -- remove "-- …" lines so embedded ";" in comments don't break splitting
//      │
//      ▼ split on ";"                     -- each chunk is one complete SQL statement
//      │
//      ▼ classify each chunk (identAfter) -- does it say CREATE TABLE or CREATE INDEX?
//      │                                     extract the name that follows the keyword
//      ▼
//  DDL struct
//      ├── tableOrder: ["albums","tracks",…]  ← file order preserved; deps (albums) come before children (tracks)
//      ├── table{}:    name → CREATE TABLE …  ← full SQL, looked up by recreate()
//      └── index{}:    name → CREATE INDEX …  ← full SQL, looked up by buildIndex()
//                                │
//               ┌────────────────┴──────────────────┐
//               ▼                                   ▼
//       createSchema()                     buildIndex("name")
//    walks tableOrder in order             called explicitly at chosen moments —
//    DROP + CREATE each table              2 built early (phases depend on them)
//    → 10 empty tables exist               6 built late (after all data is loaded)
//
// ════════════════════════════════════════════════════════════════════════════
//  BUILD PHASES — pipeline that fills master.db
//
//  Phase 0 — createSchema()
//      │
//      ▼ walk tableOrder in file order
//      DROP TABLE IF EXISTS each table    -- so a retry always starts clean
//      CREATE TABLE each table            -- 10 empty tables now exist in master.db
//
//  ─────────────────────────────────────────────────────────────────────────
//  Phase 1 — phaseAlbums()
//      │
//      ▼ sequential scan of sp.album_images
//      CREATE _album_cover (temp table)   -- picks largest cover art per album (MAX width*height) in one pass
//      │
//      ▼ INSERT INTO albums
//      SELECT from sp.albums              -- LEFT JOIN _album_cover (on /home, not exfat)
//      LEFT JOIN _album_cover             -- to attach the cover url to each album row
//      │
//      ▼ DROP _album_cover                -- temp table no longer needed
//
//  ─────────────────────────────────────────────────────────────────────────
//  Phase 2 — phaseArtists()
//      │
//      ▼ INSERT INTO artists
//      SELECT rowid, id, name, …          -- pure sequential copy, no joins
//      from sp.artists
//      │
//      ▼ INSERT INTO artist_genres
//      SELECT artist_rowid, genre         -- pure sequential copy, no joins
//      from sp.artist_genres
//
//  ─────────────────────────────────────────────────────────────────────────
//  Phase 3 — phaseTracks()  [groups 3a, 3b, early index, 3c]
//
//  Phase 3a — tracks
//      │
//      ▼ INSERT INTO tracks
//      SELECT from sp.tracks              -- LEFT JOIN main.albums (on /home) to
//      LEFT JOIN main.albums              -- denormalize release_date onto each track
//
//  Phase 3b — track_mappings
//      │
//      ▼ INSERT INTO track_mappings
//      SELECT rowid, 'source_a', id       -- stores the original text id alongside
//      from sp.tracks                     -- the new integer track_id (the Rosetta Stone)
//
//  Phase 3b → 3c bridge
//      │
//      ▼ buildIndex("idx_track_mappings_platform_id")
//      built HERE not at the end          -- Phase 3c needs this index to do its remap lookup
//
//  Phase 3c — phaseAudioFeatures()
//      │
//      ▼ build in-RAM sorted array        -- 260M entries of {text_id, integer_track_id}
//      SELECT from main.track_mappings    -- sequential scan on /home (fast)
//      sort the array                     -- so binary search works
//      │
//      ▼ sequential scan of af.track_audio_features
//      for each row:
//          binary search array for text id  -- pure RAM lookup, zero disk I/O
//          compute camelot_code             -- from key + mode columns
//          INSERT INTO track_audio_features -- with remapped integer track_id
//      │
//      ▼ free the array                   -- releases ~7 GB of RAM
//
//  ─────────────────────────────────────────────────────────────────────────
//  Phase 4 — phaseTrackArtists()
//      │
//      ▼ allocate position vector         -- one uint16 slot per track_id (~512 MB)
//      │
//      ▼ SELECT from sp.track_artists
//      ORDER BY track_rowid, artist_rowid -- ordered so inserts append sequentially, no random writes
//      │
//      for each row:
//          position = vector[track_id]    -- how many artists already seen for this track
//          vector[track_id]++             -- increment for next artist on same track
//          INSERT INTO track_artists      -- with track_id, artist_id, artist_position
//
//  ─────────────────────────────────────────────────────────────────────────
//  Phase 5 bridge
//      │
//      ▼ buildIndex("idx_tracks_isrc")
//      built HERE not at the end          -- Phase 5 does WHERE isrc=? per row; needs this index
//
//  Phase 5 — phaseSourceB()
//      │
//      ▼ open source_b.csv (110 GB)
//      read header row                    -- find column positions for TrackISRC and AlbumGenreName
//      │
//      for each row:
//          look up isrc in main.tracks    -- uses idx_tracks_isrc (on /home, fast)
//          INSERT OR IGNORE into          -- first genre per track wins
//          source_b_genres               -- one ISRC can map to multiple track_ids (1:N)
//          │
//          every 2M rows:
//              save byte offset to db     -- so a crash can resume mid-CSV, not from byte 0
//              COMMIT
//
//  ─────────────────────────────────────────────────────────────────────────
//  Phase 7 — phaseFinalIndexes()
//      │
//      ▼ buildIndex × 6                   -- all remaining indexes built now that data is loaded
//      idx_taf_track_id
//      idx_track_artists_artist_id
//      idx_source_b_genres_genre
//      idx_ml_predictions_genre
//      idx_ml_predictions_confidence
//      idx_artists_name
//      │
//      ▼ ANALYZE                          -- gives SQLite query planner statistics for fast queries
//
// ════════════════════════════════════════════════════════════════════════════

#include <sqlite3.h>

#include <algorithm>
#include <cctype>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <chrono>
#include <string>
#include <vector>
#include <unordered_map>
#include <stdexcept>
#include <sys/stat.h>

// ─────────────────────────────── Configuration ─────────────────────────────
namespace cfg {
const std::string SRC_DIR      = "/run/media/zer0/7FE7-F0D8/Databases";
const std::string SOURCE_A_DB  = SRC_DIR + "/source_a.sqlite3";
const std::string AUDIO_DB     = SRC_DIR + "/source_a_audio_features.sqlite3";
const std::string SOURCE_B_CSV = SRC_DIR + "/source_b.csv";

const std::string OUT_DB     = "/home/zer0/Desktop/sonic_something/master.db";
const std::string SCHEMA_SQL = "/home/zer0/Desktop/sonic_something/database/schema.sql";
const std::string TMP_DIR    = "/home/zer0/Desktop/sonic_something/_build_tmp"; // SQLITE_TMPDIR

const int64_t COMMIT_EVERY   = 2'000'000; 
const int64_t LOG_EVERY      = 10'000'000; 
}

// ───────────────────────────────── Helpers ─────────────────────────────────
using Clock = std::chrono::steady_clock; // steady clock is monotonic and never jumps like unlike system_clock does in some circumstances

static double secsSince(Clock::time_point t0) {
    return std::chrono::duration<double>(Clock::now() - t0).count();
}

[[noreturn]] static void die(const std::string& msg) {
    std::fprintf(stderr, "\n[FATAL] %s\n", msg.c_str());
    std::exit(1);
}

static void execOrDie(sqlite3* db, const std::string& sql, const char* ctx) {
    char* err = nullptr;
    if (sqlite3_exec(db, sql.c_str(), nullptr, nullptr, &err) != SQLITE_OK) {
        std::string m = std::string("exec failed [") + ctx + "]: " +
                        (err ? err : "?") + "\n  SQL: " + sql.substr(0, 400);
        sqlite3_free(err);
        die(m);
    }
}

static int64_t scalarInt(sqlite3* db, const std::string& sql) {
    sqlite3_stmt* st = nullptr;
    if (sqlite3_prepare_v2(db, sql.c_str(), -1, &st, nullptr) != SQLITE_OK)
        die(std::string("prepare scalar: ") + sqlite3_errmsg(db) + " | " + sql);
    int64_t v = -1;
    if (sqlite3_step(st) == SQLITE_ROW) v = sqlite3_column_int64(st, 0);
    sqlite3_finalize(st);
    return v;
}

static void logStep(const std::string& s) {
    std::printf(">>> %s\n", s.c_str());
    std::fflush(stdout);
}

// ───────────────────────── Schema parsing (DDL) ────────────────────────────
struct DDL {
    std::unordered_map<std::string, std::string> table;
    std::vector<std::string> tableOrder;
    std::unordered_map<std::string, std::string> index;
};

static std::string readFile(const std::string& path) {
    FILE* f = std::fopen(path.c_str(), "rb");
    if (!f) die("cannot open " + path);
    std::string out;
    char buf[1 << 16];
    size_t n;
    while ((n = std::fread(buf, 1, sizeof(buf), f)) > 0) out.append(buf, n);
    std::fclose(f);
    return out;
}

static std::string identAfter(const std::string& chunk, const std::string& kw) {
    size_t p = chunk.find(kw);
    if (p == std::string::npos) return "";
    p += kw.size();
    while (p < chunk.size() && std::isspace((unsigned char)chunk[p])) p++;
    std::string name;
    while (p < chunk.size()) {
        char c = chunk[p++];
        if (c == '`' || c == '"' || c == '\'') continue;
        if (std::isspace((unsigned char)c) || c == '(') break;
        name.push_back(c);
    }
    return name;
}

// Strip `-- …` line comments (SQLite ignores them anyway)
static std::string stripLineComments(const std::string& in) {
    std::string out;
    out.reserve(in.size());
    size_t i = 0;
    while (i < in.size()) {
        if (in[i] == '-' && i + 1 < in.size() && in[i + 1] == '-') {
            while (i < in.size() && in[i] != '\n') i++; 
        } else {
            out.push_back(in[i++]);
        }
    }
    return out;
}

static DDL parseSchema(const std::string& path) {
    std::string sql = stripLineComments(readFile(path));
    DDL ddl;
    size_t start = 0;
    while (start < sql.size()) {
        size_t semi = sql.find(';', start);
        if (semi == std::string::npos) break;
        std::string chunk = sql.substr(start, semi - start + 1);
        start = semi + 1;

        bool isTable = chunk.find("CREATE TABLE") != std::string::npos;
        bool isIndex = chunk.find("CREATE INDEX") != std::string::npos ||
                       chunk.find("CREATE UNIQUE INDEX") != std::string::npos;
        if (isTable) {
            std::string name = identAfter(chunk, "TABLE");
            if (!name.empty()) { ddl.table[name] = chunk; ddl.tableOrder.push_back(name); }
        } else if (isIndex) {
            std::string name = identAfter(chunk, "INDEX");
            if (!name.empty()) ddl.index[name] = chunk;
        }
    }
    if (ddl.tableOrder.empty()) die("parsed no CREATE TABLE statements from schema.sql");
    return ddl;
}

// ──────────────────────── Build-progress harness ───────────────────────────
struct Progress {
    sqlite3* db;
    void init() {
        execOrDie(db,
            "CREATE TABLE IF NOT EXISTS _build_progress("
            "phase TEXT PRIMARY KEY, done_at INTEGER);", "progress.init");
    }
    bool done(const std::string& phase) {
        return scalarInt(db, "SELECT count(*) FROM _build_progress WHERE phase='" + phase + "';") > 0;
    }
    void mark(const std::string& phase) {
        execOrDie(db, "INSERT OR REPLACE INTO _build_progress(phase, done_at) VALUES('" +
                      phase + "', strftime('%s','now'));", "progress.mark");
    }
};

// ─────────────────────────── DDL recreate helper ───────────────────────────
struct Builder {
    sqlite3* db;
    const DDL& ddl;
    Progress prog;
    int64_t dryRun = 0;

    std::string filt(const std::string& col) const {
        return dryRun ? (" WHERE " + col + " <= " + std::to_string(dryRun)) : std::string();
    }
    std::string andFilt(const std::string& col) const {
        return dryRun ? (" AND " + col + " <= " + std::to_string(dryRun)) : std::string();
    }

    void recreate(const std::string& table) {
        auto it = ddl.table.find(table);
        if (it == ddl.table.end()) die("schema.sql has no table '" + table + "'");
        execOrDie(db, "DROP TABLE IF EXISTS main." + table + ";", "drop");
        execOrDie(db, it->second, ("create " + table).c_str());
    }

    void buildIndex(const std::string& name) {
        auto it = ddl.index.find(name);
        if (it == ddl.index.end()) die("schema.sql has no index '" + name + "'");
        Clock::time_point t0 = Clock::now();
        logStep("index " + name + " …");
        execOrDie(db, "DROP INDEX IF EXISTS " + name + ";", "dropidx");
        execOrDie(db, it->second, ("index " + name).c_str());
        std::printf("    built %s in %.1fs\n", name.c_str(), secsSince(t0));
    }

    void runSql(const std::string& phase, const std::string& sql) {
        Clock::time_point t0 = Clock::now();
        execOrDie(db, "BEGIN;", "begin");
        execOrDie(db, sql, phase.c_str());
        int64_t n = sqlite3_changes64(db);
        execOrDie(db, "COMMIT;", "commit");
        std::printf("    %s: %lld rows in %.1fs\n", phase.c_str(), (long long)n, secsSince(t0));
    }
};

// ───────────────────────────── Camelot (C++) ───────────────────────────────
static const char* camelot(int key, int mode) {
    static const char* MAJ[12] = {"8B","3B","10B","5B","12B","7B","2B","9B","4B","11B","6B","1B"};
    static const char* MIN[12] = {"5A","12A","7A","2A","9A","4A","11A","6A","1A","8A","3A","10A"};
    if (key < 0 || key > 11) return "";
    return (mode == 1) ? MAJ[key] : MIN[key];
}

static void bindCol(sqlite3_stmt* ins, int ii, sqlite3_stmt* sel, int si) {
    switch (sqlite3_column_type(sel, si)) {
        case SQLITE_INTEGER: sqlite3_bind_int64(ins, ii, sqlite3_column_int64(sel, si)); break;
        case SQLITE_FLOAT:   sqlite3_bind_double(ins, ii, sqlite3_column_double(sel, si)); break;
        default:             sqlite3_bind_null(ins, ii); break;
    }
}

// Phase 3c — audio features remapped via an in-RAM source_a_id->track_id table.
// WHY NOT SQL: any JOIN/ORDER BY plan random-probes an 8 GB structure that can't
// stay in page cache while the 41 GB af scan floods it → endless re-reads (we
// measured 157 GB read, 0 progress). A sorted in-RAM vector does the remap with
// ZERO random disk I/O: sequential af scan + binary-search lookup + append insert.
static void phaseAudioFeatures(Builder& b) {
    if (b.prog.done("audio_features")) { logStep("audio_features [skip]"); return; }
    logStep("Phase 3c: audio features (in-RAM source_a_id->track_id map)");
    b.recreate("track_audio_features");
    execOrDie(b.db, "PRAGMA cache_size=-1500000;", "taf cache down"); // ~1.5 GB; leave RAM for the map

    #pragma pack(push, 1)
    struct Entry { char id[22]; int32_t tid; };   // 26 bytes; Source A ids are 22 base62 chars
    #pragma pack(pop)
    auto cmp = [](const Entry& a, const Entry& c){ return std::memcmp(a.id, c.id, 22) < 0; };

    Clock::time_point t0 = Clock::now();
    std::vector<Entry> map;
    map.reserve(260'000'000);
    {
        sqlite3_stmt* s = nullptr;
        if (sqlite3_prepare_v2(b.db, "SELECT platform_id, track_id FROM main.track_mappings;",
                               -1, &s, nullptr) != SQLITE_OK)
            die(std::string("prepare map: ") + sqlite3_errmsg(b.db));
        while (sqlite3_step(s) == SQLITE_ROW) {
            Entry e{};                                   
            const char* pid = (const char*)sqlite3_column_text(s, 0);
            int len = sqlite3_column_bytes(s, 0);
            std::memcpy(e.id, pid, len > 22 ? 22 : len);
            e.tid = (int32_t)sqlite3_column_int64(s, 1);
            map.push_back(e);
        }
        sqlite3_finalize(s);
    }
    std::printf("    map: %zu entries (%.1f GB) in %.1fs; sorting...\n",
                map.size(), map.size() * sizeof(Entry) / 1e9, secsSince(t0));
    std::sort(map.begin(), map.end(), cmp);
    std::printf("    map sorted (%.1fs total)\n", secsSince(t0));

    std::string selSql =
        "SELECT track_id, danceability, energy, \"key\", loudness, mode, speechiness, "
        "acousticness, instrumentalness, liveness, valence, tempo, time_signature "
        "FROM af.track_audio_features WHERE null_response = 0" + b.andFilt("rowid") + ";";
    sqlite3_stmt* sel = nullptr;
    if (sqlite3_prepare_v2(b.db, selSql.c_str(), -1, &sel, nullptr) != SQLITE_OK)
        die(std::string("prepare af sel: ") + sqlite3_errmsg(b.db));
    sqlite3_stmt* ins = nullptr;
    if (sqlite3_prepare_v2(b.db,
        "INSERT INTO main.track_audio_features(track_id, danceability, energy, \"key\", loudness, "
        "mode, speechiness, acousticness, instrumentalness, liveness, valence, tempo, "
        "time_signature, camelot_code) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14);",
        -1, &ins, nullptr) != SQLITE_OK)
        die(std::string("prepare af ins: ") + sqlite3_errmsg(b.db));

    Clock::time_point t1 = Clock::now();
    int64_t n = 0, matched = 0;
    execOrDie(b.db, "BEGIN;", "taf begin");
    while (sqlite3_step(sel) == SQLITE_ROW) {
        ++n;
        Entry key{};
        const char* sid = (const char*)sqlite3_column_text(sel, 0);
        int len = sqlite3_column_bytes(sel, 0);
        std::memcpy(key.id, sid, len > 22 ? 22 : len);
        auto it = std::lower_bound(map.begin(), map.end(), key, cmp);
        if (it == map.end() || std::memcmp(it->id, key.id, 22) != 0) continue;
        ++matched;

        int km = (sqlite3_column_type(sel, 3) == SQLITE_NULL) ? -1 : sqlite3_column_int(sel, 3);
        int md = (sqlite3_column_type(sel, 5) == SQLITE_NULL) ? -1 : sqlite3_column_int(sel, 5);
        const char* cam = camelot(km, md);

        sqlite3_bind_int64(ins, 1, it->tid);
        bindCol(ins, 2, sel, 1);  bindCol(ins, 3, sel, 2);  bindCol(ins, 4, sel, 3);
        bindCol(ins, 5, sel, 4);  bindCol(ins, 6, sel, 5);  bindCol(ins, 7, sel, 6);
        bindCol(ins, 8, sel, 7);  bindCol(ins, 9, sel, 8);  bindCol(ins, 10, sel, 9);
        bindCol(ins, 11, sel, 10); bindCol(ins, 12, sel, 11); bindCol(ins, 13, sel, 12);
        if (cam[0]) sqlite3_bind_text(ins, 14, cam, -1, SQLITE_STATIC);
        else        sqlite3_bind_null(ins, 14);

        if (sqlite3_step(ins) != SQLITE_DONE) die(std::string("taf ins: ") + sqlite3_errmsg(b.db));
        sqlite3_reset(ins);

        if (n % cfg::COMMIT_EVERY == 0) { execOrDie(b.db, "COMMIT;", "c"); execOrDie(b.db, "BEGIN;", "b"); }
        if (n % cfg::LOG_EVERY == 0)
            std::printf("    audio: %lld scanned, %lld matched (%.0f k/s)\n",
                        (long long)n, (long long)matched, n / 1000.0 / secsSince(t1));
    }
    execOrDie(b.db, "COMMIT;", "taf final");
    sqlite3_finalize(sel);
    sqlite3_finalize(ins);
    std::printf("    audio_features: %lld matched / %lld scanned in %.1fs\n",
                (long long)matched, (long long)n, secsSince(t1));

    std::vector<Entry>().swap(map);
    execOrDie(b.db, "PRAGMA cache_size=-6000000;", "taf cache up");
    b.prog.mark("audio_features");
}

// ══════════════════════════════ Phases ═════════════════════════════════════

// Phase 1 — Albums (sequential album_images aggregate → /home temp → join albums)
static void phaseAlbums(Builder& b) {
    const std::string P = "albums";
    if (b.prog.done(P)) { logStep("albums [skip]"); return; }
    logStep("Phase 1: albums");
    b.recreate("albums");
    execOrDie(b.db, "DROP TABLE IF EXISTS main._album_cover;", "drop cover");

    Clock::time_point t0 = Clock::now();
    execOrDie(b.db,
        "CREATE TABLE main._album_cover AS "
        "SELECT album_rowid, url FROM ("
        "  SELECT album_rowid, url, MAX(width*height) AS mx "
        "  FROM sp.album_images" + b.filt("album_rowid") +
        "  GROUP BY album_rowid);", "album_cover");
    std::printf("    _album_cover built in %.1fs\n", secsSince(t0));

    b.runSql("albums",
        "INSERT INTO main.albums(album_id, source_a_id, title, album_type, release_date, cover_art_url) "
        "SELECT a.rowid, a.id, a.name, a.album_type, a.release_date, c.url "
        "FROM sp.albums a LEFT JOIN main._album_cover c ON c.album_rowid = a.rowid" +
        b.filt("a.rowid") + ";");

    execOrDie(b.db, "DROP TABLE IF EXISTS main._album_cover;", "drop cover2");
    b.prog.mark(P);
}

// Phase 2 — Artists + artist_genres (pure sequential copies, no joins)
static void phaseArtists(Builder& b) {
    const std::string P = "artists";
    if (b.prog.done(P)) { logStep("artists [skip]"); return; }
    logStep("Phase 2: artists + artist_genres");
    b.recreate("artists");
    b.recreate("artist_genres");

    b.runSql("artists",
        "INSERT INTO main.artists(artist_id, source_a_id, name, popularity, followers_total) "
        "SELECT rowid, id, name, popularity, followers_total FROM sp.artists" +
        b.filt("rowid") + ";");

    b.runSql("artist_genres",
        "INSERT OR IGNORE INTO main.artist_genres(artist_id, genre) "
        "SELECT artist_rowid, genre FROM sp.artist_genres" + b.filt("artist_rowid") + ";");
    b.prog.mark(P);
}

// Phase 3 — tracks (3a) → track_mappings (3b) → idx → audio features (3c)
static void phaseTracks(Builder& b) {
    // 3a + 3b + index + 3c are grouped, but each has its own progress marker
    // so a crash resumes at the right sub-step without redoing 256M-row inserts.

    // 3a tracks
    if (!b.prog.done("tracks")) {
        logStep("Phase 3a: tracks");
        b.recreate("tracks");
        b.runSql("tracks",
            "INSERT INTO main.tracks(track_id, album_id, isrc, title, popularity, release_date, "
            "is_explicit, duration_ms, preview_url) "
            "SELECT t.rowid, t.album_rowid, t.external_id_isrc, t.name, t.popularity, "
            "ma.release_date, t.explicit, t.duration_ms, t.preview_url "
            "FROM sp.tracks t LEFT JOIN main.albums ma ON ma.album_id = t.album_rowid" +
            b.filt("t.rowid") + ";");
        b.prog.mark("tracks");
    } else logStep("tracks [skip]");

    // 3b track_mappings  (also the /home probe target for 3c)
    if (!b.prog.done("track_mappings")) {
        logStep("Phase 3b: track_mappings");
        b.recreate("track_mappings");
        b.runSql("track_mappings",
            "INSERT INTO main.track_mappings(track_id, platform, platform_id) "
            "SELECT rowid, 'source_a', id FROM sp.tracks" + b.filt("rowid") + ";");
        b.prog.mark("track_mappings");
    } else logStep("track_mappings [skip]");

    if (!b.prog.done("idx_track_mappings_platform_id")) {
        b.buildIndex("idx_track_mappings_platform_id");
        b.prog.mark("idx_track_mappings_platform_id");
    } else logStep("idx_track_mappings_platform_id [skip]");

    // 3c audio features — in-RAM remap (no random disk probes); see phaseAudioFeatures
    phaseAudioFeatures(b);
}

// Phase 4 — track_artists (348M): direct rowid copy + artist_position counter (C++)
static void phaseTrackArtists(Builder& b) {
    const std::string P = "track_artists";
    if (b.prog.done(P)) { logStep("track_artists [skip]"); return; }
    logStep("Phase 4: track_artists (with artist_position)");
    b.recreate("track_artists");

    const int64_t maxTrack = scalarInt(b.db, "SELECT max(rowid) FROM sp.tracks;");
    if (maxTrack <= 0) die("could not read max track rowid");
    std::printf("    position vector: %lld slots (%.0f MB)\n",
                (long long)(maxTrack + 1), (maxTrack + 1) * 2.0 / 1e6);
    std::vector<uint16_t> pos(maxTrack + 1, 0);

    std::string sel = "SELECT track_rowid, artist_rowid FROM sp.track_artists" +
                      b.filt("track_rowid") + " ORDER BY track_rowid, artist_rowid;";
    sqlite3_stmt* rd = nullptr;
    if (sqlite3_prepare_v2(b.db, sel.c_str(), -1, &rd, nullptr) != SQLITE_OK)
        die(std::string("prepare ta select: ") + sqlite3_errmsg(b.db));

    sqlite3_stmt* ins = nullptr;
    if (sqlite3_prepare_v2(b.db,
            "INSERT OR IGNORE INTO main.track_artists(track_id, artist_id, artist_position) "
            "VALUES(?1, ?2, ?3);", -1, &ins, nullptr) != SQLITE_OK)
        die(std::string("prepare ta insert: ") + sqlite3_errmsg(b.db));

    Clock::time_point t0 = Clock::now();
    int64_t n = 0;
    execOrDie(b.db, "BEGIN;", "ta begin");
    while (sqlite3_step(rd) == SQLITE_ROW) {
        int64_t tr = sqlite3_column_int64(rd, 0);
        int64_t ar = sqlite3_column_int64(rd, 1);
        uint16_t p = 0;
        if (tr >= 0 && tr <= maxTrack) { p = pos[tr]; if (pos[tr] != 0xFFFF) pos[tr]++; }

        sqlite3_bind_int64(ins, 1, tr);
        sqlite3_bind_int64(ins, 2, ar);
        sqlite3_bind_int(ins, 3, p);
        if (sqlite3_step(ins) != SQLITE_DONE)
            die(std::string("ta insert step: ") + sqlite3_errmsg(b.db));
        sqlite3_reset(ins);

        if (++n % cfg::COMMIT_EVERY == 0) {
            execOrDie(b.db, "COMMIT;", "ta commit");
            execOrDie(b.db, "BEGIN;", "ta begin2");
        }
        if (n % cfg::LOG_EVERY == 0)
            std::printf("    track_artists: %lld rows (%.0f k/s)\n",
                        (long long)n, n / 1000.0 / secsSince(t0));
    }
    execOrDie(b.db, "COMMIT;", "ta final commit");
    sqlite3_finalize(rd);
    sqlite3_finalize(ins);
    std::printf("    track_artists: %lld rows in %.1fs\n", (long long)n, secsSince(t0));
    b.prog.mark(P);
}

// ─────────────────────── RFC-4180 CSV reader (Phase 5) ──────────────────────
struct CsvReader {
    FILE* f = nullptr;
    std::vector<char> buf;
    size_t pos = 0, len = 0;
    int64_t bytePos = 0;

    explicit CsvReader(const std::string& path) : buf(1 << 20) {
        f = std::fopen(path.c_str(), "rb");
        if (!f) die("cannot open CSV " + path);
    }
    ~CsvReader() { if (f) std::fclose(f); }

    inline int get() {
        if (pos >= len) {
            len = std::fread(buf.data(), 1, buf.size(), f);
            pos = 0;
            if (len == 0) return EOF;
        }
        bytePos++;
        return (unsigned char)buf[pos++];
    }
    void seekTo(int64_t off) {
        std::fseek(f, off, SEEK_SET);
        pos = len = 0;
        bytePos = off;
    }
    bool record(std::vector<std::string>& fields) {
        int c = get();
        if (c == EOF) return false;
        fields.clear();
        std::string cur;
        bool inQ = false;
        while (true) {
            if (c == EOF) { fields.push_back(cur); return true; }
            if (inQ) {
                if (c == '"') {
                    int n = get();
                    if (n == '"') { cur.push_back('"'); c = get(); }
                    else { inQ = false; c = n; }
                } else { cur.push_back((char)c); c = get(); }
            } else {
                if (c == '"') { inQ = true; c = get(); }
                else if (c == ',') { fields.push_back(cur); cur.clear(); c = get(); }
                else if (c == '\r') { c = get(); }
                else if (c == '\n') { fields.push_back(cur); return true; }
                else { cur.push_back((char)c); c = get(); }
            }
        }
    }
};

// Phase 5 — Source B CSV → source_b_genres (ISRC → /home tracks index, 1:N)
static void phaseSourceB(Builder& b) {
    const std::string P = "source_b";
    if (b.prog.done(P)) { logStep("source_b [skip]"); return; }
    logStep("Phase 5: source_b genres (raw AlbumGenreName by ISRC)");

    execOrDie(b.db, "CREATE TABLE IF NOT EXISTS _source_b_offset("
                    "id INTEGER PRIMARY KEY CHECK(id=0), byte_off INTEGER);", "dz off tbl");
    int64_t resume = scalarInt(b.db, "SELECT COALESCE((SELECT byte_off FROM _source_b_offset WHERE id=0),0);");
    if (resume == 0) b.recreate("source_b_genres");

    CsvReader csv(cfg::SOURCE_B_CSV);
    std::vector<std::string> fields;
    if (!csv.record(fields)) die("source_b CSV empty");

    int idxISRC = -1, idxGenre = -1;
    for (int i = 0; i < (int)fields.size(); ++i) {
        if (fields[i] == "TrackISRC")      idxISRC = i;
        if (fields[i] == "AlbumGenreName") idxGenre = i;
    }
    if (idxISRC < 0 || idxGenre < 0) die("source_b header missing TrackISRC/AlbumGenreName");
    int maxCol = std::max(idxISRC, idxGenre);
    std::printf("    columns: TrackISRC=%d AlbumGenreName=%d\n", idxISRC, idxGenre);

    if (resume > 0) { std::printf("    resuming at byte offset %lld\n", (long long)resume); csv.seekTo(resume); }

    sqlite3_stmt* findTrack = nullptr; 
    if (sqlite3_prepare_v2(b.db, "SELECT track_id FROM main.tracks WHERE isrc = ?1;",
                           -1, &findTrack, nullptr) != SQLITE_OK)
        die(std::string("prepare findTrack: ") + sqlite3_errmsg(b.db));
    sqlite3_stmt* insGenre = nullptr;
    if (sqlite3_prepare_v2(b.db,
            "INSERT OR IGNORE INTO main.source_b_genres(track_id, genre) VALUES(?1, ?2);",
            -1, &insGenre, nullptr) != SQLITE_OK)
        die(std::string("prepare insGenre: ") + sqlite3_errmsg(b.db));

    Clock::time_point t0 = Clock::now();
    int64_t rows = 0, matched = 0, inserted = 0;
    execOrDie(b.db, "BEGIN;", "dz begin");
    while (csv.record(fields)) {
        if (b.dryRun && rows >= b.dryRun) break;
        ++rows;
        if ((int)fields.size() <= maxCol) continue;
        const std::string& isrc  = fields[idxISRC];
        const std::string& genre = fields[idxGenre];
        if (isrc.empty() || genre.empty()) goto checkpoint;

        sqlite3_bind_text(findTrack, 1, isrc.c_str(), -1, SQLITE_TRANSIENT);
        while (sqlite3_step(findTrack) == SQLITE_ROW) {
            int64_t tid = sqlite3_column_int64(findTrack, 0);
            ++matched;
            sqlite3_bind_int64(insGenre, 1, tid);
            sqlite3_bind_text(insGenre, 2, genre.c_str(), -1, SQLITE_TRANSIENT);
            if (sqlite3_step(insGenre) != SQLITE_DONE)
                die(std::string("insGenre step: ") + sqlite3_errmsg(b.db));
            inserted += sqlite3_changes(b.db);
            sqlite3_reset(insGenre);
        }
        sqlite3_reset(findTrack);

    checkpoint:
        if (rows % cfg::COMMIT_EVERY == 0) {
            execOrDie(b.db, "INSERT OR REPLACE INTO _source_b_offset(id, byte_off) VALUES(0, " +
                            std::to_string(csv.bytePos) + ");", "dz off save");
            execOrDie(b.db, "COMMIT;", "dz commit");
            execOrDie(b.db, "BEGIN;", "dz begin2");
        }
        if (rows % cfg::LOG_EVERY == 0)
            std::printf("    source_b: %lld rows, %lld matched, %lld inserted (%.0f k rows/s)\n",
                        (long long)rows, (long long)matched, (long long)inserted,
                        rows / 1000.0 / secsSince(t0));
    }
    execOrDie(b.db, "COMMIT;", "dz final commit");
    sqlite3_finalize(findTrack);
    sqlite3_finalize(insGenre);
    std::printf("    source_b: %lld rows, %lld matched, %lld inserted in %.1fs\n",
                (long long)rows, (long long)matched, (long long)inserted, secsSince(t0));
    execOrDie(b.db, "DROP TABLE IF EXISTS _source_b_offset;", "dz off drop");
    b.prog.mark(P);
}

// Phase 6 — remaining indexes + finalize
static void phaseIndexes(Builder& b) {
    if (!b.prog.done("idx_tracks_isrc")) {
        b.buildIndex("idx_tracks_isrc");
        b.prog.mark("idx_tracks_isrc");
    } else logStep("idx_tracks_isrc [skip]");
}

static void phaseFinalIndexes(Builder& b) {
    const std::string P = "final_indexes";
    if (b.prog.done(P)) { logStep("final_indexes [skip]"); return; }
    logStep("Phase 7: remaining indexes + ANALYZE");
    for (const char* name : {"idx_taf_track_id", "idx_track_artists_artist_id",
                             "idx_source_b_genres_genre", "idx_ml_predictions_genre",
                             "idx_ml_predictions_confidence", "idx_artists_name"}) {
        b.buildIndex(name);
    }
    execOrDie(b.db, "PRAGMA analysis_limit=1000;", "analimit");
    logStep("ANALYZE …");
    Clock::time_point t0 = Clock::now();
    execOrDie(b.db, "ANALYZE;", "analyze");
    std::printf("    ANALYZE in %.1fs\n", secsSince(t0));
    b.prog.mark(P);
}

// ──────────────────────────── Validation ───────────────────────────────────
static void validate(sqlite3* db) {
    logStep("Validation — row counts");
    struct Exp { const char* tbl; long long expect; };
    const Exp tables[] = {
        {"albums",               58'591'047},
        {"artists",              15'430'442},
        {"artist_genres",         2'229'947},  
        {"tracks",              256'039'007},
        {"track_mappings",      256'039'007},
        {"track_audio_features",255'597'711}, 
        {"track_artists",       348'055'756}, 
        {"source_b_genres",141'670'000},  
    };
    for (const auto& e : tables) {
        int64_t got = scalarInt(db, std::string("SELECT count(*) FROM ") + e.tbl + ";");
        std::printf("    %-22s %14lld   (expect ~%lld)\n", e.tbl, (long long)got, e.expect);
    }
    int64_t cam = scalarInt(db, "SELECT count(*) FROM track_audio_features WHERE camelot_code IS NOT NULL;");
    int64_t camNull = scalarInt(db, "SELECT count(*) FROM track_audio_features WHERE camelot_code IS NULL;");
    std::printf("    camelot_code: %lld set / %lld null\n", (long long)cam, (long long)camNull);
    int64_t orphanAlb = scalarInt(db,
        "SELECT count(*) FROM tracks t LEFT JOIN albums a ON a.album_id=t.album_id "
        "WHERE t.album_id IS NOT NULL AND a.album_id IS NULL;");
    std::printf("    tracks with missing album: %lld\n", (long long)orphanAlb);
}

// ───────────────────────────── Connection ──────────────────────────────────
static sqlite3* openMaster() {
    ::mkdir(cfg::TMP_DIR.c_str(), 0755);
    setenv("SQLITE_TMPDIR", cfg::TMP_DIR.c_str(), 1); 

    sqlite3* db = nullptr;
    if (sqlite3_open(cfg::OUT_DB.c_str(), &db) != SQLITE_OK)
        die(std::string("open master: ") + sqlite3_errmsg(db));

    execOrDie(db, "PRAGMA page_size=32768;", "pragma page_size");
    execOrDie(db, "PRAGMA journal_mode=DELETE;", "pragma journal");
    execOrDie(db, "PRAGMA synchronous=FULL;", "pragma sync");
    execOrDie(db, "PRAGMA temp_store=FILE;", "pragma temp_store");
    execOrDie(db, "PRAGMA cache_size=-6000000;", "pragma cache");     
    execOrDie(db, "PRAGMA mmap_size=30000000000;", "pragma mmap");
    execOrDie(db, "PRAGMA foreign_keys=OFF;", "pragma fk");
    execOrDie(db, "PRAGMA busy_timeout=60000;", "pragma busy");

    execOrDie(db, "ATTACH DATABASE '" + cfg::SOURCE_A_DB + "' AS sp;", "attach sp");
    execOrDie(db, "ATTACH DATABASE '" + cfg::AUDIO_DB + "' AS af;", "attach af");
    return db;
}

static void createSchema(Builder& b) {
    if (b.prog.done("schema")) { logStep("schema [skip]"); return; }
    logStep("Phase 0: create tables (no indexes yet)");
    for (const std::string& t : b.ddl.tableOrder) b.recreate(t);
    b.prog.mark("schema");
}

static void resetAll(sqlite3* db, const DDL& ddl) {
    logStep("--reset: dropping all output tables + progress");
    for (auto it = ddl.tableOrder.rbegin(); it != ddl.tableOrder.rend(); ++it)
        execOrDie(db, "DROP TABLE IF EXISTS main." + *it + ";", "reset drop");
    execOrDie(db, "DROP TABLE IF EXISTS main._album_cover;", "reset cover");
    execOrDie(db, "DROP TABLE IF EXISTS _source_b_offset;", "reset dzoff");
    execOrDie(db, "DROP TABLE IF EXISTS _build_progress;", "reset prog");
}

// ─────────────────────────────────── main ──────────────────────────────────
int main(int argc, char** argv) {
    int64_t dryRun = 0;
    bool doReset = false, validateOnly = false;
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        if (a == "--dry-run" && i + 1 < argc) dryRun = std::strtoll(argv[++i], nullptr, 10);
        else if (a == "--reset") doReset = true;
        else if (a == "--validate") validateOnly = true;
        else die("unknown arg: " + a);
    }

    std::printf("Zinthos — master.db builder\n");
    std::printf("  source : %s\n  output : %s\n", cfg::SRC_DIR.c_str(), cfg::OUT_DB.c_str());
    if (dryRun) std::printf("  MODE   : DRY-RUN (source rows <= %lld)\n", (long long)dryRun);

    DDL ddl = parseSchema(cfg::SCHEMA_SQL);
    sqlite3* db = openMaster();

    Builder b{db, ddl, Progress{db}, dryRun};
    b.prog.init();

    if (validateOnly) { validate(db); sqlite3_close(db); return 0; }
    if (doReset) {
        resetAll(db, ddl);
        b.prog.init();
        std::printf("--reset complete: all output tables + progress cleared. "
                    "Run with no args to build.\n");
        sqlite3_close(db);
        return 0;
    }

    Clock::time_point all = Clock::now();
    createSchema(b);
    phaseAlbums(b);
    phaseArtists(b);
    phaseTracks(b); 
    phaseTrackArtists(b);
    phaseIndexes(b);        
    phaseSourceB(b);
    phaseFinalIndexes(b);    // remaining indexes + ANALYZE

    std::printf("\n=== BUILD COMPLETE in %.1f min ===\n", secsSince(all) / 60.0);
    validate(db);

    // Rollback journal is auto-deleted on the final commit
    sqlite3_close(db);
    std::printf("Run `sqlite3 %s 'PRAGMA integrity_check;'` for a final check.\n", cfg::OUT_DB.c_str());
    return 0;
}
