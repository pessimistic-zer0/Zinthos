//! Zinthos — terminal client (M6).
//!
//! "Something like that." — describe how music should sound or feel, and find it.
//!
//! A thin HTTP client over the engine's JSON API (default http://127.0.0.1:3000). It links
//! NO FAISS/SQLite — all heavy lifting stays in the engine. HTTP runs on a background thread
//! so the UI never freezes; a spinner animates while a request is in flight.
//!
//! Screens: Menu → one of { Search, Scan (local library), Playlist, ByName } → Results/Detail.
//! ByName (title+artist) → Candidates pick-list → `s`/Enter → Similar results — the by-name
//! front door onto F6. Esc walks back up; from the menu Esc/q quits, Ctrl-C quits from anywhere.

use std::collections::HashSet;
use std::process::Child;
use std::sync::mpsc::{channel, Receiver, TryRecvError};
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use ratatui::crossterm::event::{self, Event, KeyCode, KeyEventKind, KeyModifiers};
use ratatui::buffer::Buffer;
use ratatui::layout::{Alignment, Constraint, Layout, Margin, Position, Rect};
use ratatui::style::{Color, Modifier, Style, Stylize};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, BorderType, Borders, List, ListItem, ListState, Paragraph, Wrap};
use ratatui::Frame;
use serde_json::Value;
use unicode_width::UnicodeWidthChar;

const DEFAULT_URL: &str = "http://127.0.0.1:3000";
const PAGE: usize = 50;
const SPIN: [&str; 10] = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];

// Delta Corps Priest 1, "Zinthos". Regenerate with tui/assets/regen-banner.sh.
const BANNER: [&str; 8] = [
    " ▄███████▄   ▄█  ███▄▄▄▄       ███        ▄█    █▄     ▄██████▄     ▄████████ ",
    "██▀     ▄██ ███  ███▀▀▀██▄ ▀█████████▄   ███    ███   ███    ███   ███    ███ ",
    "      ▄███▀ ███▌ ███   ███    ▀███▀▀██   ███    ███   ███    ███   ███    █▀  ",
    " ▀█▀▄███▀▄▄ ███▌ ███   ███     ███   ▀  ▄███▄▄▄▄███▄▄ ███    ███   ███        ",
    "  ▄███▀   ▀ ███▌ ███   ███     ███     ▀▀███▀▀▀▀███▀  ███    ███ ▀███████████ ",
    "▄███▀       ███  ███   ███     ███       ███    ███   ███    ███          ███ ",
    "███▄     ▄█ ███  ███   ███     ███       ███    ███   ███    ███    ▄█    ███ ",
    " ▀████████▀ █▀    ▀█   █▀     ▄████▀     ███    █▀     ▀██████▀   ▄████████▀  ",
];
const CHANT: &str = "Aeolian · Melodion · Zinthos";
const TAGLINE: &str = "Something like that.";

// A small augury beneath the chant — what Zinthos does, in a darker register.
const DESC: [&str; 3] = [
    "Utter the mood, and let the augury begin.",
    "From a trove that knows no end it shall plumb thy liking,",
    "and weave thee song to song, each to each akin.",
];

// (glyph, name, subtitle) — rendered as tiles, or as a stacked list on narrow terminals.
/// Menu cards are laid out as a grid this many columns wide (2×2 for the four options).
const MENU_COLS: usize = 2;

const TILES: [(&str, &str, &str); 4] = [
    ("⌕", "Search by Description", "find songs by how they sound & feel"),
    ("≡", "Scan My Library", "map your taste & get recommendations"),
    ("♫", "Build a Mood Playlist", "a smoothly-sequenced set from a vibe"),
    ("≈", "Find Similar Songs", "name a track & find ones like it"),
];

#[derive(PartialEq, Clone, Copy)]
enum Mode {
    Menu,
    Search,
    Results,
    Detail,
    Scan,
    Library,
    Playlist,
    ByName,     // similar-by-name: a two-field (title, artist) input form
    Candidates, // the by-name pick-list — choose which track to find similars for
}

enum Job {
    Search { q: String, offset: usize, append: bool, seed: u32 },
    Similar(i64),
    Detail(i64),
    Scan { path: String },
    Playlist { q: String },
    ByName { title: String, artist: String },
}

enum Done {
    Results { items: Vec<Value>, source: String, append: bool, pageable: bool },
    Detail(Value),
    Library(Value),
    Candidates(Vec<Value>), // by-name matches → the pick-list
    Err(String),
}

// Audio file extensions lofty can read tags from.
const AUDIO_EXT: [&str; 11] =
    ["mp3", "flac", "m4a", "mp4", "aac", "ogg", "opus", "wav", "wv", "aiff", "ape"];
const SCAN_CAP: usize = 5000; // cap files per scan so the JSON payload stays sane

fn s(v: &Value, k: &str) -> String {
    match v.get(k) {
        Some(Value::String(s)) => s.clone(),
        Some(x) if !x.is_null() => x.to_string(),
        _ => String::new(),
    }
}

fn results_of(v: &Value) -> Vec<Value> {
    v.get("results").and_then(Value::as_array).cloned().unwrap_or_default()
}

fn tracks_of(v: &Value) -> Vec<Value> {
    v.get("tracks").and_then(Value::as_array).cloned().unwrap_or_default()
}

fn candidates_of(v: &Value) -> Vec<Value> {
    v.get("candidates").and_then(Value::as_array).cloned().unwrap_or_default()
}

fn u(v: &Value, k: &str) -> usize {
    v.get(k).and_then(Value::as_u64).unwrap_or(0) as usize
}

/// Parse a breakdown histogram array (`[{<key>,count}, …]`) into (label, count) pairs.
fn hist(v: &Value, arr_key: &str, label_key: &str) -> Vec<(String, u64)> {
    v.get("breakdown")
        .and_then(|b| b.get(arr_key))
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .map(|e| (s(e, label_key), e.get("count").and_then(Value::as_u64).unwrap_or(0)))
                .collect()
        })
        .unwrap_or_default()
}

fn dedupe_key(v: &Value) -> (String, String) {
    (s(v, "title").to_lowercase(), s(v, "artists").to_lowercase())
}

/// `Title — artists`: the one-line identity of a track, for the similar-results header.
fn label_of(v: &Value) -> String {
    let (t, a) = (s(v, "title"), s(v, "artists"));
    if a.is_empty() { t } else { format!("{t} — {a}") }
}

// ── local-library tag extraction (runs on the worker thread) ──────────────────────
// The client reads only light tags locally and ships JSON; the audio never leaves the box.
fn extract_one(path: &std::path::Path) -> Option<Value> {
    use lofty::prelude::*;
    let tagged = lofty::read_from_path(path).ok()?;
    let tag = tagged.primary_tag().or_else(|| tagged.first_tag())?;
    let title = tag.title().map(|c| c.to_string()).unwrap_or_default();
    let artist = tag.artist().map(|c| c.to_string()).unwrap_or_default();
    let album = tag.album().map(|c| c.to_string()).unwrap_or_default();
    let isrc = tag.get_string(ItemKey::Isrc).map(str::to_string);
    let duration_ms = tagged.properties().duration().as_millis() as u64;
    // Useless to send if we have neither an ISRC nor a title to match on.
    if isrc.is_none() && title.is_empty() {
        return None;
    }
    Some(serde_json::json!({
        "title": title, "artist": artist, "album": album,
        "duration_ms": duration_ms, "isrc": isrc,
    }))
}

fn extract_library(root: &str) -> Vec<Value> {
    use walkdir::WalkDir;
    // Pass 1: collect audio paths (cheap directory metadata only). SCAN_CAP is applied to
    // the path list here, BEFORE dispatch, so the cap stays exact under parallelism.
    let paths: Vec<std::path::PathBuf> = WalkDir::new(root)
        .into_iter()
        .filter_map(Result::ok)
        .filter(|e| e.file_type().is_file())
        .map(|e| e.into_path())
        .filter(|p| {
            p.extension()
                .and_then(|e| e.to_str())
                .map(|e| AUDIO_EXT.contains(&e.to_ascii_lowercase().as_str()))
                .unwrap_or(false)
        })
        .take(SCAN_CAP)
        .collect();
    if paths.is_empty() {
        return Vec::new();
    }
    // Pass 2: read tags in parallel. Each read is independent, ~18 ms, and mostly disk
    // wait — sequential reads took ~13 s for a 726-file library on a 12-core box. Contiguous
    // chunks joined in spawn order keep the output in walk order, same as the old loop.
    let workers = thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(4)
        .min(8)
        .min(paths.len());
    let chunk = (paths.len() + workers - 1) / workers;
    let mut out = Vec::with_capacity(paths.len());
    thread::scope(|s| {
        let handles: Vec<_> = paths
            .chunks(chunk)
            .map(|c| s.spawn(move || c.iter().filter_map(|p| extract_one(p)).collect::<Vec<_>>()))
            .collect();
        for h in handles {
            out.extend(h.join().unwrap_or_default());
        }
    });
    out
}

// ── Zinthos palette ───────────────────────────────────────────────────────────────
// Banner runs cool, top→base; gold frames it ("outline"); crimson is the hot accent.
const VIOLET: Color = Color::Rgb(0x7C, 0x6B, 0xB0); // frame / border (mid-gradient)
const CRIMSON: Color = Color::Rgb(0xB0, 0x1F, 0x32); // sparing hot accent
const LAVENDER: Color = Color::Rgb(0xB6, 0xA6, 0xDC); // top of the gradient, wordmark
const TITLE: Color = Color::Rgb(0xEC, 0xEC, 0xF4); // result-row titles: explicit near-white so
                                                   // bold text renders crisply (no terminal-default
                                                   // "bright bold" remap that dims under GIF capture)

// ── banner gradient: lavender (top) → indigo (base), 4 stops, per line ─────────────
fn grad(i: usize, n: usize) -> Color {
    // soft lavender → hair violet → raven blue → deep indigo
    const STOPS: [(u8, u8, u8); 4] =
        [(0xB6, 0xA6, 0xDC), (0x7C, 0x6B, 0xB0), (0x3A, 0x34, 0x88), (0x1C, 0x19, 0x47)];
    let t = if n <= 1 { 0.0 } else { i as f32 / (n - 1) as f32 };
    let seg = t * (STOPS.len() - 1) as f32;
    let k = (seg.floor() as usize).min(STOPS.len() - 2); // which stop pair we're between
    let f = seg - k as f32; // 0..1 within that pair
    let lerp = |a: u8, b: u8| (a as f32 + (b as f32 - a as f32) * f).round() as u8;
    let (a, b) = (STOPS[k], STOPS[k + 1]);
    Color::Rgb(lerp(a.0, b.0), lerp(a.1, b.1), lerp(a.2, b.2))
}

/// A slow "breathing" brightness in [0.78, 1.0] — multiplies the banner's RGB so the
/// whole logo gently pulses. ~6s period at the ~10fps idle tick.
fn glow(frame: u64) -> f32 {
    let t = (frame as f32) * 0.10; // radians/tick
    0.89 + 0.11 * t.sin()
}

fn dim_rgb(c: Color, k: f32) -> Color {
    match c {
        Color::Rgb(r, g, b) => Color::Rgb(
            (r as f32 * k).round() as u8,
            (g as f32 * k).round() as u8,
            (b as f32 * k).round() as u8,
        ),
        other => other,
    }
}

fn banner_lines(frame: u64) -> Vec<Line<'static>> {
    let k = glow(frame);
    BANNER
        .iter()
        .enumerate()
        .map(|(i, b)| {
            Line::from(Span::styled(*b, Style::default().fg(dim_rgb(grad(i, BANNER.len()), k)).bold()))
        })
        .collect()
}

// ── ambient sky: parallax starfield + a rare meteor + drifting notes ───────────────
// All drawn *before* the foreground, so banner/menu text overwrites what it covers and
// only the gaps (side margins, padding) keep the sky.
// dim lavender / near-white / violet — scaled down further per-mote as it twinkles
const STAR_HUES: [(u8, u8, u8); 3] = [(0xB6, 0xA6, 0xDC), (0xCF, 0xC9, 0xE8), (0x7C, 0x6B, 0xB0)];

fn hashi(a: i64, b: i64, seed: u32) -> u32 {
    let h = (a as u64)
        .wrapping_mul(73856093)
        ^ (b as u64).wrapping_mul(19349663)
        ^ (seed as u64).wrapping_mul(83492791);
    let mut h = (h as u32) ^ ((h >> 32) as u32);
    h ^= h >> 13;
    h = h.wrapping_mul(0x5bd1e995);
    h ^ (h >> 15)
}

/// One depth layer of stars, panning left at `speed` cells/frame. Nearer layers (passed
/// brighter/faster) read as in front; far ones drift slow and dim → parallax depth.
fn star_layer(
    buf: &mut Buffer,
    area: Rect,
    frame: u64,
    speed: f32,
    density: u32,
    bright: f32,
    glyphs: &[&str],
    seed: u32,
) {
    let offset = (frame as f32 * speed) as i64; // whole-cell pan
    for y in area.top()..area.bottom() {
        for x in area.left()..area.right() {
            let wx = x as i64 + offset; // world column (so stars drift as time passes)
            let h = hashi(wx, y as i64, seed);
            if h % density != 0 {
                continue;
            }
            let phase = (h % 628) as f32 / 100.0;
            let tw = (frame as f32 * 0.07 + phase).sin() * 0.5 + 0.5; // twinkle 0..1
            if tw < 0.18 {
                continue; // blinks out at the trough
            }
            let g = glyphs[(h >> 5) as usize % glyphs.len()];
            let (r, gr, b) = STAR_HUES[(h >> 3) as usize % STAR_HUES.len()];
            let k = (0.25 + 0.6 * tw) * bright;
            let col = Color::Rgb((r as f32 * k) as u8, (gr as f32 * k) as u8, (b as f32 * k) as u8);
            if let Some(cell) = buf.cell_mut(Position::new(x, y)) {
                cell.set_symbol(g).set_fg(col);
            }
        }
    }
}

fn starfield(f: &mut Frame, area: Rect, frame: u64) {
    let buf = f.buffer_mut();
    // static (speed 0) — fixed positions that only twinkle; depth tiers vary by brightness.
    star_layer(buf, area, frame, 0.0, 75, 0.40, &["·", "˙"], 1);
    star_layer(buf, area, frame, 0.0, 95, 0.70, &["·", "∙", "⋆"], 2);
    star_layer(buf, area, frame, 0.0, 120, 1.00, &["✦", "⋆", "∙"], 3);
}

/// A lone meteor every ~24s: streaks a short fading tail diagonally across, then gone.
fn shooting_star(f: &mut Frame, area: Rect, frame: u64) {
    const PERIOD: u64 = 240; // gap between meteors (~24s at the idle tick)
    const DUR: u64 = 16; // how long the streak is visible (~1.6s)
    let (w, ht) = (area.width as i32, area.height as i32);
    if w < 12 || ht < 6 {
        return;
    }
    let tt = frame % PERIOD;
    if tt >= DUR {
        return; // dormant most of the cycle
    }
    let h = hashi((frame / PERIOD) as i64, 777, 9); // one RNG draw per meteor
    let sx = area.left() as i32 + (h % w as u32) as i32;
    let sy = area.top() as i32 + ((h >> 8) % (ht / 2).max(1) as u32) as i32;
    let dir = if (h >> 16) & 1 == 0 { 1 } else { -1 }; // down-right or down-left
    let prog = tt as i32;
    let hx = sx + dir * prog * 2; // shallow diagonal: 2 cols per row
    let hy = sy + prog;
    let slope = if dir == 1 { "╲" } else { "╱" };
    let tail = [("✦", 1.0_f32), (slope, 0.6), (slope, 0.4), ("·", 0.2)];
    let buf = f.buffer_mut();
    for (i, (g, br)) in tail.iter().enumerate() {
        let x = hx - dir * i as i32 * 2;
        let y = hy - i as i32;
        if x < area.left() as i32 || x >= area.right() as i32 || y < area.top() as i32 || y >= area.bottom() as i32 {
            continue;
        }
        let col = Color::Rgb((0xCF as f32 * br) as u8, (0xC9 as f32 * br) as u8, (0xE8 as f32 * br) as u8);
        if let Some(cell) = buf.cell_mut(Position::new(x as u16, y as u16)) {
            cell.set_symbol(g).set_fg(col);
        }
    }
}

/// Faint music notes drifting up from the bottom and fading — sparse, staggered slots.
fn notes(f: &mut Frame, area: Rect, frame: u64) {
    const SLOTS: u64 = 6;
    const PERIOD: u64 = 150; // each slot emits a note ~every 15s
    const RISE: u64 = 100; // visible while rising (~10s); rest of the period is silent
    let (w, ht) = (area.width as i32, area.height as i32);
    if w < 6 || ht < 6 {
        return;
    }
    let glyphs = ["♪", "♫", "♩", "♬"];
    let buf = f.buffer_mut();
    for slot in 0..SLOTS {
        let stag = frame + slot * (PERIOD / SLOTS); // stagger slots across the period
        let local = stag % PERIOD;
        if local >= RISE {
            continue;
        }
        let h = hashi((stag / PERIOD) as i64, slot as i64, 5);
        let base_x = area.left() as i32 + (h % w as u32) as i32;
        let total = (ht - 2).max(1);
        let y = area.bottom() as i32 - 1 - (local as i32 * total / RISE as i32);
        if y < area.top() as i32 {
            continue;
        }
        let sway = ((local as f32 * 0.15 + (h % 100) as f32).sin() * 1.5) as i32;
        let x = base_x + sway;
        if x < area.left() as i32 || x >= area.right() as i32 {
            continue;
        }
        let life = 1.0 - local as f32 / RISE as f32; // 1 → 0 as it rises
        let k = life * 0.55; // keep faint
        let g = glyphs[(h >> 4) as usize % glyphs.len()];
        let col = Color::Rgb((0xB6 as f32 * k) as u8, (0xA6 as f32 * k) as u8, (0xDC as f32 * k) as u8);
        if let Some(cell) = buf.cell_mut(Position::new(x as u16, y as u16)) {
            cell.set_symbol(g).set_fg(col);
        }
    }
}

/// The full ambient sky — starfield, the occasional meteor, and drifting notes — drawn
/// into `area`. Call it right after the screen's frame/border but *before* any foreground
/// widget, so the content overwrites the cells it covers and only the gaps keep the sky.
fn ambient(f: &mut Frame, area: Rect, frame: u64) {
    starfield(f, area, frame);
    shooting_star(f, area, frame);
    notes(f, area, frame);
}

// ── a little astronaut, drifting in the background ─────────────────────────────────
// Drawn after the starfield, before the foreground — so it floats *behind* the text,
// getting gently occluded as it crosses the centred content.
const ASTRONAUT: [&str; 8] = [
    "    ____     ",
    "  .'    '.   ",
    " /  o  o  \\  ",
    " |   ||   |  ",
    "  \\  __  /   ",
    " __'.__.'__  ",
    "/__|    |__\\ ",
    "   |_|  |_|  ",
];

#[allow(dead_code)]
fn astronaut(f: &mut Frame, area: Rect, frame: u64) {
    let sw = ASTRONAUT.iter().map(|l| l.chars().count()).max().unwrap_or(0) as f32;
    let sh = ASTRONAUT.len() as f32;
    if (area.width as f32) < sw + 2.0 || (area.height as f32) < sh + 2.0 {
        return; // no room to float without clipping
    }
    // slow Lissajous drift across the whole pane (~45–60s per axis at the idle tick)
    let t = frame as f32;
    let mx = area.width as f32 - sw - 2.0;
    let my = area.height as f32 - sh - 2.0;
    let fx = (t * 0.013).sin() * 0.5 + 0.5; // 0..1
    let fy = (t * 0.019 + 1.3).sin() * 0.5 + 0.5; // 0..1, out of phase → roams
    let ox = area.left() as f32 + 1.0 + fx * mx;
    let oy = area.top() as f32 + 1.0 + fy * my;
    let col = Color::Rgb(0x6A, 0x63, 0x86); // dim violet-grey, sits behind the UI

    let buf = f.buffer_mut();
    for (r, line) in ASTRONAUT.iter().enumerate() {
        let y = oy as u16 + r as u16;
        if y >= area.bottom() {
            break;
        }
        for (c, ch) in line.chars().enumerate() {
            if ch == ' ' {
                continue; // transparent — don't blank the stars behind it
            }
            let x = ox as u16 + c as u16;
            if x >= area.right() {
                continue;
            }
            if let Some(cell) = buf.cell_mut(Position::new(x, y)) {
                let mut tmp = [0u8; 4];
                cell.set_symbol(ch.encode_utf8(&mut tmp)).set_fg(col);
            }
        }
    }
}

// ── ornamental section divider: a rule with a sigil centred in it ──────────────────
fn divider(width: u16, sigil: &str) -> Line<'static> {
    let w = width as usize;
    let pad = sigil.chars().count() + 2; // a space either side of the sigil
    if w <= pad {
        return Line::from("");
    }
    let side = (w - pad) / 2;
    Line::from(vec![
        Span::styled("─".repeat(side), Style::default().fg(VIOLET).dim()),
        Span::raw(" "),
        Span::styled(sigil.to_string(), Style::default().fg(LAVENDER)),
        Span::raw(" "),
        Span::styled("─".repeat(w - pad - side), Style::default().fg(VIOLET).dim()),
    ])
}

// ── a single editable text field (cursor is a char index) ─────────────────────────
#[derive(Default)]
struct TextField {
    value: String,
    cursor: usize,
}

impl TextField {
    fn char_len(&self) -> usize {
        self.value.chars().count()
    }
    fn byte_idx(&self, ci: usize) -> usize {
        self.value.char_indices().nth(ci).map(|(i, _)| i).unwrap_or(self.value.len())
    }
    fn insert(&mut self, c: char) {
        let b = self.byte_idx(self.cursor);
        self.value.insert(b, c);
        self.cursor += 1;
    }
    fn backspace(&mut self) {
        if self.cursor > 0 {
            let b = self.byte_idx(self.cursor - 1);
            self.value.remove(b);
            self.cursor -= 1;
        }
    }
    fn delete(&mut self) {
        if self.cursor < self.char_len() {
            let b = self.byte_idx(self.cursor);
            self.value.remove(b);
        }
    }
    fn delete_word(&mut self) {
        let chars: Vec<char> = self.value.chars().collect();
        let mut i = self.cursor;
        while i > 0 && chars[i - 1].is_whitespace() {
            i -= 1;
        }
        while i > 0 && !chars[i - 1].is_whitespace() {
            i -= 1;
        }
        self.value = chars[..i].iter().chain(chars[self.cursor..].iter()).collect();
        self.cursor = i;
    }
    fn clear(&mut self) {
        self.value.clear();
        self.cursor = 0;
    }

    /// Handle a key as a text edit / caret move. Returns true if it was consumed here.
    fn handle(&mut self, code: KeyCode, ctrl: bool) -> bool {
        match code {
            KeyCode::Left => self.cursor = self.cursor.saturating_sub(1),
            KeyCode::Right => self.cursor = (self.cursor + 1).min(self.char_len()),
            KeyCode::Home => self.cursor = 0,
            KeyCode::End => self.cursor = self.char_len(),
            KeyCode::Backspace if ctrl => self.delete_word(),
            KeyCode::Backspace => self.backspace(),
            KeyCode::Delete => self.delete(),
            // Ctrl+Backspace often arrives as Ctrl-H; Ctrl-W is the classic delete-word.
            KeyCode::Char('h') | KeyCode::Char('w') if ctrl => self.delete_word(),
            KeyCode::Char('u') if ctrl => self.clear(),
            KeyCode::Char('a') if ctrl => self.cursor = 0,
            KeyCode::Char('e') if ctrl => self.cursor = self.char_len(),
            KeyCode::Char(c) if !ctrl => self.insert(c),
            _ => return false,
        }
        true
    }
}

/// Render a bordered input box, scrolled so the caret stays visible, and place the (visible)
/// terminal cursor on it.
///
/// The scroll is the point: a Paragraph clips at the border and the caret used to pin to the
/// last column, so everything typed past the box width went in BLIND. That bites on a mood
/// description and hardest on the scan path, where an absolute path routinely outruns an
/// 80-column box. Columns, not chars — a CJK title is double-width, so counting chars would
/// drift the caret away from the glyph it is meant to sit on.
///
/// The window is derived from the caret each frame rather than stored: walking back from the
/// caret keeps it pinned to the right edge once the value overflows, and the text scrolls
/// under it when moving left. A remembered scroll offset would let the caret travel to the
/// left edge before scrolling, but it would have to live in TextField and be fed the box
/// width — state this screen does not otherwise need.
fn input_box(f: &mut Frame, area: Rect, title: &str, field: &TextField, focus: bool) {
    let border = if focus { Color::Cyan } else { Color::DarkGray };
    let inner = area.width.saturating_sub(2) as usize;
    let chars: Vec<char> = field.value.chars().collect();
    let cw = |c: char| UnicodeWidthChar::width(c).unwrap_or(0);

    // Widen the window leftwards from the caret while it fits, keeping one column spare so
    // the caret has somewhere to sit when it is at end-of-text.
    let mut start = field.cursor.min(chars.len());
    let mut before = 0usize; // columns between `start` and the caret
    while start > 0 && before + cw(chars[start - 1]) < inner {
        start -= 1;
        before += cw(chars[start]);
    }
    let mut text = String::new();
    let mut used = 0usize;
    for &c in &chars[start..] {
        if used + cw(c) > inner {
            break;
        }
        text.push(c);
        used += cw(c);
    }

    let p = Paragraph::new(text).block(
        Block::default()
            .borders(Borders::ALL)
            .border_type(BorderType::Rounded)
            .title(format!(" {title} "))
            .border_style(Style::default().fg(border)),
    );
    f.render_widget(p, area);
    if focus {
        let max_x = area.x + area.width.saturating_sub(2);
        let cx = (area.x + 1 + before as u16).min(max_x);
        f.set_cursor_position(Position::new(cx, area.y + 1));
    }
}

/// A read-only companion to `input_box`: same frame, no cursor, no focus colour. For context
/// the user is meant to READ, not type into — e.g. which track the similars belong to.
fn label_box(f: &mut Frame, area: Rect, title: &str, text: &str) {
    let p = Paragraph::new(Span::styled(text.to_string(), Style::default().fg(LAVENDER))).block(
        Block::default()
            .borders(Borders::ALL)
            .border_type(BorderType::Rounded)
            .title(format!(" {title} "))
            .border_style(Style::default().fg(Color::DarkGray)),
    );
    f.render_widget(p, area);
}

/// The shared-list state one screen owns. `App.results` is a SINGLE buffer that the search
/// page, the by-name pick-list, the library recommendations and the similars page all render
/// from, so launching a similars search destroys whatever list was under it. This is the
/// one-level undo that lets Esc put that list back exactly as it was.
struct Stash {
    results: Vec<Value>,
    seen: HashSet<(String, String)>,
    sel: Option<usize>,
    source: String,
    status: String,
    offset: usize,
    seed: u32,
    pageable: bool,
}

struct App {
    base_url: String,
    backend_dir: String,
    vim: bool,
    agent: ureq::Agent,
    mode: Mode,
    menu_sel: usize,
    query: TextField,
    scan: TextField,
    playlist: TextField,
    byname_title: TextField,
    byname_artist: TextField,
    byname_focus: usize, // 0 = title field, 1 = artist field
    last_query: String,
    offset: usize, // next raw SQL offset for paging
    seed: u32,     // 0 = strict popularity; >0 = a seeded reshuffle draw (no paging)
    pageable: bool,
    results: Vec<Value>,
    seen: HashSet<(String, String)>, // cross-page dedupe
    list: ListState,
    detail: Option<Value>,
    detail_from: Mode,
    status: String,
    source: String,
    // ── similar-results context ──────────────────────────────────────────────────
    // A similar list is NOT a query result: it has a seed track and an origin screen. The
    // Query box means nothing for it, and `r`/`/` (which re-run or edit last_query) are worse
    // than useless — `r` used to silently swap the similars for a reshuffle of a STALE search.
    similar_seed: String, // "Title — artists" of the seed, shown instead of the Query box
    similar_from: Mode,   // where Esc returns (Candidates for the by-name flow, Library, …)
    stash: Option<Stash>, // that screen's list, parked while the similars borrow the buffer
    spinner: usize,
    frame: u64, // monotonic tick for ambient animation (starfield, glow)
    rx: Option<Receiver<Done>>,
    child: Option<Child>,
    // library scan summary
    lib_total: usize,
    lib_matched: usize,
    lib_unmatched: usize,
    lib_genres: Vec<(String, u64)>,
    lib_eras: Vec<(String, u64)>,
}

impl App {
    fn new(base_url: String, backend_dir: String, vim: bool) -> Self {
        let agent = ureq::AgentBuilder::new().timeout(Duration::from_secs(30)).build();
        App {
            base_url,
            backend_dir,
            vim,
            agent,
            mode: Mode::Menu,
            menu_sel: 0,
            query: TextField::default(),
            scan: TextField::default(),
            playlist: TextField::default(),
            byname_title: TextField::default(),
            byname_artist: TextField::default(),
            byname_focus: 0,
            last_query: String::new(),
            offset: 0,
            seed: 0,
            pageable: false,
            results: Vec::new(),
            seen: HashSet::new(),
            list: ListState::default(),
            detail: None,
            detail_from: Mode::Results,
            status: String::new(),
            source: String::new(),
            similar_seed: String::new(),
            similar_from: Mode::Menu,
            stash: None,
            spinner: 0,
            frame: 0,
            rx: None,
            child: None,
            lib_total: 0,
            lib_matched: 0,
            lib_unmatched: 0,
            lib_genres: Vec::new(),
            lib_eras: Vec::new(),
        }
    }

    fn busy(&self) -> bool {
        self.rx.is_some()
    }

    fn selected_id(&self) -> Option<i64> {
        self.list
            .selected()
            .and_then(|i| self.results.get(i))
            .and_then(|r| r.get("track_id"))
            .and_then(Value::as_i64)
    }

    fn selected_label(&self) -> String {
        self.list
            .selected()
            .and_then(|i| self.results.get(i))
            .map(label_of)
            .unwrap_or_default()
    }

    /// Launch a similars search, recording WHAT it is for and WHERE Esc goes back to.
    /// Chaining `s` off an existing similar list keeps the ORIGINAL origin, so Esc still lands
    /// on the screen the chain started from instead of cycling inside Mode::Results.
    fn start_similar(&mut self, seed: String) {
        if self.busy() {
            return; // start() would drop the job — don't stash a screen we aren't leaving.
        }
        if let Some(id) = self.selected_id() {
            // Only the FIRST hop out of a real screen stashes. Chaining `s` down a similars
            // list keeps the original stash, so Esc lands where the chain began rather than
            // parking a similars list on top of itself.
            if self.source != "similar" {
                self.similar_from = self.mode;
                // Clone, don't take: the origin screen keeps rendering this list until the
                // job lands (and keeps it for good if the job errors out).
                self.stash = Some(Stash {
                    results: self.results.clone(),
                    seen: self.seen.clone(),
                    sel: self.list.selected(),
                    source: self.source.clone(),
                    status: self.status.clone(),
                    offset: self.offset,
                    seed: self.seed,
                    pageable: self.pageable,
                });
            }
            self.similar_seed = seed;
            self.start(Job::Similar(id));
        }
    }

    /// Esc out of a similars list: restore the borrowed buffer, then return to its screen.
    /// Restoring also clears `source`, which is what stops a second Esc looping back here.
    fn leave_similar(&mut self) {
        if let Some(st) = self.stash.take() {
            self.results = st.results;
            self.seen = st.seen;
            self.list.select(st.sel);
            self.source = st.source;
            self.status = st.status;
            self.offset = st.offset;
            self.seed = st.seed;
            self.pageable = st.pageable;
        }
        self.similar_seed.clear();
        self.mode = self.similar_from;
    }

    // ── async job plumbing ────────────────────────────────────────────────────────
    fn start(&mut self, job: Job) {
        if self.busy() {
            return;
        }
        let (tx, rx) = channel();
        let agent = self.agent.clone();
        let base = self.base_url.clone();
        self.rx = Some(rx);
        self.status = "searching".into();
        thread::spawn(move || {
            let _ = tx.send(run_job(&agent, &base, job));
        });
    }

    fn tick(&mut self) {
        self.frame = self.frame.wrapping_add(1);
        let msg = match &self.rx {
            Some(rx) => match rx.try_recv() {
                Ok(d) => Some(Some(d)),
                Err(TryRecvError::Empty) => None,
                Err(TryRecvError::Disconnected) => Some(None),
            },
            None => None,
        };
        match msg {
            Some(Some(done)) => {
                self.rx = None;
                self.apply(done);
            }
            Some(None) => {
                self.rx = None;
                self.status = "fetch failed".into();
            }
            None => {
                if self.busy() {
                    self.spinner = (self.spinner + 1) % SPIN.len();
                }
            }
        }
    }

    fn apply(&mut self, done: Done) {
        match done {
            Done::Results { items, source, append, pageable } => {
                self.source = source;
                self.pageable = pageable;
                if append {
                    let mut added = 0;
                    for it in items {
                        if self.seen.insert(dedupe_key(&it)) {
                            self.results.push(it);
                            added += 1;
                        }
                    }
                    self.offset += PAGE;
                    self.status = format!("{} results (+{}) · {}", self.results.len(), added, self.source);
                } else {
                    self.seen.clear();
                    self.results.clear();
                    for it in items {
                        if self.seen.insert(dedupe_key(&it)) {
                            self.results.push(it);
                        }
                    }
                    self.offset = PAGE;
                    self.list
                        .select(if self.results.is_empty() { None } else { Some(0) });
                    self.mode = Mode::Results;
                    self.status = format!("{} results · {}", self.results.len(), self.source);
                }
            }
            Done::Detail(v) => {
                self.detail = Some(v);
                self.mode = Mode::Detail;
                self.status.clear();
            }
            Done::Library(v) => {
                self.lib_total = u(&v, "total");
                self.lib_matched = u(&v, "matched");
                self.lib_unmatched = u(&v, "unmatched");
                self.lib_genres = hist(&v, "genres", "genre");
                self.lib_eras = hist(&v, "eras", "decade");
                self.source = "library".into();
                self.pageable = false;
                self.seen.clear();
                self.results.clear();
                let recs = v.get("recommendations").and_then(Value::as_array).cloned().unwrap_or_default();
                for it in recs {
                    if self.seen.insert(dedupe_key(&it)) {
                        self.results.push(it);
                    }
                }
                self.list
                    .select(if self.results.is_empty() { None } else { Some(0) });
                self.mode = Mode::Library;
                self.status = format!(
                    "matched {}/{} · {} recommendations",
                    self.lib_matched, self.lib_total, self.results.len()
                );
            }
            Done::Candidates(items) => {
                // by-name pick-list. Server already dedupes, but reuse the cross-item dedupe
                // for consistency, then land on the Candidates screen with row 0 selected.
                self.source = "by-name".into();
                self.pageable = false;
                self.seen.clear();
                self.results.clear();
                for it in items {
                    if self.seen.insert(dedupe_key(&it)) {
                        self.results.push(it);
                    }
                }
                self.list.select(if self.results.is_empty() { None } else { Some(0) });
                self.mode = Mode::Candidates;
                self.status = format!("{} match(es) · pick one, then s for similar", self.results.len());
            }
            Done::Err(e) => self.status = format!("error: {e}"),
        }
    }

    // ── navigation / actions ──────────────────────────────────────────────────────
    fn move_sel(&mut self, delta: isize) {
        if self.results.is_empty() {
            return;
        }
        let n = self.results.len() as isize;
        let cur = self.list.selected().unwrap_or(0) as isize;
        self.list.select(Some((cur + delta).clamp(0, n - 1) as usize));
    }
    fn down(&mut self) {
        let last = self.results.len().saturating_sub(1);
        if !self.results.is_empty() && self.list.selected() == Some(last) {
            self.load_more();
        } else {
            self.move_sel(1);
        }
    }
    fn load_more(&mut self) {
        // A seeded reshuffle is a one-shot sampled draw, not an offset-pageable list.
        if self.pageable && self.seed == 0 && !self.last_query.is_empty() && !self.busy() {
            self.start(Job::Search { q: self.last_query.clone(), offset: self.offset, append: true, seed: 0 });
        }
    }
    fn submit_search(&mut self) {
        let q = self.query.value.trim().to_string();
        if q.is_empty() {
            return;
        }
        self.last_query = q.clone();
        self.seed = 0; // a fresh query shows the best (most popular) matches first
        self.start(Job::Search { q, offset: 0, append: false, seed: 0 });
    }
    /// "Different songs": re-run the current query with a fresh random seed, surfacing a new
    /// slice of the matching pool (popularity-favoured but no longer popularity-locked).
    fn reshuffle(&mut self) {
        if self.last_query.is_empty() || self.busy() {
            return;
        }
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.subsec_nanos())
            .unwrap_or(0);
        self.seed = nanos.max(1); // never 0 — that's the deterministic path
        self.start(Job::Search { q: self.last_query.clone(), offset: 0, append: false, seed: self.seed });
    }

    /// Similar-by-name step 1: resolve a typed (title, artist) to catalog candidates. The
    /// engine requires the artist (Option-1); an empty title we reject locally.
    fn submit_byname(&mut self) {
        let title = self.byname_title.value.trim().to_string();
        let artist = self.byname_artist.value.trim().to_string();
        if title.is_empty() {
            self.status = "enter a song title".into();
            return;
        }
        self.start(Job::ByName { title, artist });
    }

    /// Step the menu cursor by `d` cards, clamped to the grid. A vertical move (`d` = ±cols)
    /// on the short last row would fall off the end, so it lands on the final card instead.
    fn menu_move(&mut self, d: isize) {
        let last = TILES.len() as isize - 1;
        self.menu_sel = (self.menu_sel as isize + d).clamp(0, last) as usize;
    }

    fn select_menu(&mut self) {
        match self.menu_sel {
            0 => self.mode = Mode::Search,
            1 => {
                self.mode = Mode::Scan;
                self.status = "point Zinthos at a folder of audio files".into();
            }
            2 => {
                self.mode = Mode::Playlist;
                self.status = "describe a mood — a smooth, transition-ordered playlist".into();
            }
            3 => {
                self.mode = Mode::ByName;
                self.byname_focus = 0;
                self.status = "name a song — get others like it".into();
            }
            _ => {}
        }
    }

    /// Returns true to quit.
    fn on_key(&mut self, code: KeyCode, mods: KeyModifiers) -> bool {
        let ctrl = mods.contains(KeyModifiers::CONTROL);
        if ctrl && code == KeyCode::Char('c') {
            return true;
        }
        match self.mode {
            Mode::Menu => match code {
                KeyCode::Esc | KeyCode::Char('q') => return true,
                // the tiles are a MENU_COLS-wide grid: ←/→ step one card, ↑/↓ jump a row.
                KeyCode::Left => self.menu_move(-1),
                KeyCode::Right => self.menu_move(1),
                KeyCode::Up => self.menu_move(-(MENU_COLS as isize)),
                KeyCode::Down => self.menu_move(MENU_COLS as isize),
                KeyCode::Char('h') if self.vim => self.menu_move(-1),
                KeyCode::Char('l') if self.vim => self.menu_move(1),
                KeyCode::Char('k') if self.vim => self.menu_move(-(MENU_COLS as isize)),
                KeyCode::Char('j') if self.vim => self.menu_move(MENU_COLS as isize),
                KeyCode::Char('1') => {
                    self.menu_sel = 0;
                    self.select_menu();
                }
                KeyCode::Char('2') => {
                    self.menu_sel = 1;
                    self.select_menu();
                }
                KeyCode::Char('3') => {
                    self.menu_sel = 2;
                    self.select_menu();
                }
                KeyCode::Char('4') => {
                    self.menu_sel = 3;
                    self.select_menu();
                }
                KeyCode::Enter => self.select_menu(),
                _ => {}
            },
            Mode::Search => match code {
                KeyCode::Enter => self.submit_search(),
                KeyCode::Esc => self.mode = Mode::Menu,
                KeyCode::Down => self.mode = Mode::Results,
                _ => {
                    self.query.handle(code, ctrl);
                }
            },
            Mode::Results => {
                // Mode::Results renders TWO different lists: a semantic-search page (backed by
                // last_query, pageable, reshufflable) and a similars page (backed by a seed
                // track). Only the first has a query, so gate the query-shaped keys on it.
                let sim = self.source == "similar";
                match code {
                    KeyCode::Esc => {
                        if sim {
                            self.leave_similar();
                        } else {
                            self.mode = Mode::Search;
                        }
                    }
                    KeyCode::Char('q') => {
                        // Bailing to the menu abandons the chain rather than suspending it:
                        // a stash left armed here would let a later Esc — reached from an
                        // unrelated screen — teleport into a pick-list the user has moved on
                        // from. `results` keeps the similars; only the way back is dropped.
                        self.stash = None;
                        self.similar_seed.clear();
                        self.source.clear();
                        self.mode = Mode::Menu;
                    }
                    KeyCode::Char('/') | KeyCode::Char('i') if !sim => self.mode = Mode::Search,
                    KeyCode::Up => self.move_sel(-1),
                    KeyCode::Down => self.down(),
                    KeyCode::Char('k') if self.vim => self.move_sel(-1),
                    KeyCode::Char('j') if self.vim => self.down(),
                    KeyCode::Char('s') => self.start_similar(self.selected_label()),
                    KeyCode::Char('r') => {
                        if sim {
                            self.status = "reshuffle applies to search results — Esc to go back".into();
                        } else {
                            self.reshuffle();
                        }
                    }
                    KeyCode::Enter => {
                        if let Some(id) = self.selected_id() {
                            self.detail_from = Mode::Results;
                            self.start(Job::Detail(id));
                        }
                    }
                    _ => {}
                }
            }
            Mode::Library => match code {
                KeyCode::Esc | KeyCode::Char('q') => self.mode = Mode::Menu,
                KeyCode::Up => self.move_sel(-1),
                KeyCode::Down => self.move_sel(1),
                KeyCode::Char('k') if self.vim => self.move_sel(-1),
                KeyCode::Char('j') if self.vim => self.move_sel(1),
                KeyCode::Char('s') => self.start_similar(self.selected_label()),
                KeyCode::Enter => {
                    if let Some(id) = self.selected_id() {
                        self.detail_from = Mode::Library;
                        self.start(Job::Detail(id));
                    }
                }
                _ => {}
            },
            Mode::Detail => match code {
                KeyCode::Esc | KeyCode::Enter | KeyCode::Char('q') => self.mode = self.detail_from,
                // The card, not the list row: they agree today, but the card is what's on screen.
                KeyCode::Char('s') => {
                    let seed = label_of(self.detail.as_ref().unwrap_or(&Value::Null));
                    self.start_similar(seed);
                }
                _ => {}
            },
            Mode::Scan => match code {
                KeyCode::Esc => self.mode = Mode::Menu,
                KeyCode::Enter => {
                    let path = self.scan.value.trim().to_string();
                    if path.is_empty() {
                        self.status = "enter a directory path to scan".into();
                    } else {
                        self.start(Job::Scan { path });
                        self.status = "scanning library…".into();
                    }
                }
                _ => {
                    self.scan.handle(code, ctrl);
                }
            },
            Mode::Playlist => match code {
                KeyCode::Esc => self.mode = Mode::Menu,
                KeyCode::Enter => {
                    let q = self.playlist.value.trim().to_string();
                    if q.is_empty() {
                        self.status = "describe a mood to build a playlist".into();
                    } else {
                        self.last_query = String::new(); // a playlist isn't pageable/refreshable
                        self.start(Job::Playlist { q });
                        self.status = "building playlist…".into();
                    }
                }
                _ => {
                    self.playlist.handle(code, ctrl);
                }
            },
            Mode::ByName => match code {
                KeyCode::Esc => self.mode = Mode::Menu,
                KeyCode::Tab | KeyCode::BackTab => self.byname_focus ^= 1,
                KeyCode::Up => self.byname_focus = 0,
                KeyCode::Down => self.byname_focus = 1,
                KeyCode::Enter => self.submit_byname(),
                _ => {
                    if self.byname_focus == 0 {
                        self.byname_title.handle(code, ctrl);
                    } else {
                        self.byname_artist.handle(code, ctrl);
                    }
                }
            },
            Mode::Candidates => match code {
                KeyCode::Esc => self.mode = Mode::ByName, // back to edit the name
                KeyCode::Char('q') => self.mode = Mode::Menu,
                KeyCode::Up => self.move_sel(-1),
                KeyCode::Down => self.move_sel(1),
                KeyCode::Char('k') if self.vim => self.move_sel(-1),
                KeyCode::Char('j') if self.vim => self.move_sel(1),
                // the whole point of this screen: pick a track → songs like it.
                KeyCode::Enter | KeyCode::Char('s') => self.start_similar(self.selected_label()),
                _ => {}
            },
        }
        false
    }

    fn ensure_engine(&mut self, spawn: bool) {
        if fetch(&self.agent, &self.base_url, "/health", &[]).is_ok() {
            self.status = format!("connected to {}", self.base_url);
            return;
        }
        if spawn {
            self.spawn_engine();
        } else {
            self.status = format!(
                "engine unreachable at {} — run: cd backend && python -m engine.main",
                self.base_url
            );
        }
    }

    fn spawn_engine(&mut self) {
        match std::process::Command::new("python")
            .args(["-m", "engine.main"])
            .current_dir(&self.backend_dir)
            .spawn()
        {
            Ok(c) => {
                self.child = Some(c);
                for _ in 0..40 {
                    thread::sleep(Duration::from_millis(700));
                    if fetch(&self.agent, &self.base_url, "/health", &[]).is_ok() {
                        self.status = "engine started & connected".into();
                        return;
                    }
                }
                self.status = "engine spawned but not responding yet".into();
            }
            Err(e) => self.status = format!("could not spawn engine ({e}) — start it manually"),
        }
    }
}

impl Drop for App {
    fn drop(&mut self) {
        if let Some(c) = self.child.as_mut() {
            let _ = c.kill();
        }
    }
}

// ── HTTP (runs on a worker thread) ───────────────────────────────────────────────
fn fetch(agent: &ureq::Agent, base: &str, path: &str, params: &[(&str, &str)]) -> Result<Value, String> {
    let mut req = agent.get(&format!("{base}{path}"));
    for (k, v) in params {
        req = req.query(k, v);
    }
    match req.call() {
        Ok(r) => r.into_json::<Value>().map_err(|e| e.to_string()),
        // ureq hands the RESPONSE back on 4xx/5xx, and the engine puts a human-readable reason
        // in `detail` ("no embedding or similars for track N" — a normal outcome, since not
        // every catalog track is embedded). Mapping straight to e.to_string() would show the
        // user "http status: 404" and nothing to act on.
        Err(ureq::Error::Status(code, resp)) => Err(resp
            .into_json::<Value>()
            .ok()
            .map(|v| s(&v, "detail"))
            .filter(|d| !d.is_empty())
            .unwrap_or_else(|| format!("engine returned HTTP {code}"))),
        Err(e) => Err(e.to_string()),
    }
}

fn run_job(agent: &ureq::Agent, base: &str, job: Job) -> Done {
    match job {
        Job::Search { q, offset, append, seed } => {
            let off = offset.to_string();
            let sd = seed.to_string();
            match fetch(agent, base, "/search", &[("q", &q), ("limit", "50"), ("offset", &off), ("seed", &sd)]) {
                Ok(v) => Done::Results { items: results_of(&v), source: s(&v, "source"), append, pageable: true },
                Err(e) => Done::Err(e),
            }
        }
        Job::Similar(id) => match fetch(agent, base, &format!("/search/similar/{id}"), &[("k", "40")]) {
            Ok(v) => Done::Results { items: results_of(&v), source: "similar".into(), append: false, pageable: false },
            Err(e) => Done::Err(e),
        },
        Job::Detail(id) => match fetch(agent, base, &format!("/track/{id}"), &[]) {
            Ok(v) => Done::Detail(v),
            Err(e) => Done::Err(e),
        },
        Job::Scan { path } => {
            let tracks = extract_library(&path);
            if tracks.is_empty() {
                return Done::Err("no readable audio tags found in that folder".into());
            }
            let body = serde_json::json!({ "tracks": tracks, "size": 50 });
            match agent.post(&format!("{base}/library/scan")).send_json(body) {
                Ok(resp) => match resp.into_json::<Value>() {
                    Ok(v) => Done::Library(v),
                    Err(e) => Done::Err(e.to_string()),
                },
                Err(e) => Done::Err(e.to_string()),
            }
        }
        Job::Playlist { q } => {
            let body = serde_json::json!({ "q": q, "size": 40 });
            match agent.post(&format!("{base}/playlist")).send_json(body) {
                // /playlist returns an ORDERED `tracks` array (smooth transitions) — keep order.
                Ok(resp) => match resp.into_json::<Value>() {
                    Ok(v) => Done::Results { items: tracks_of(&v), source: "playlist".into(), append: false, pageable: false },
                    Err(e) => Done::Err(e.to_string()),
                },
                Err(e) => Done::Err(e.to_string()),
            }
        }
        Job::ByName { title, artist } => {
            match fetch(agent, base, "/search/by-name",
                        &[("title", &title), ("artist", &artist), ("limit", "25")]) {
                Ok(v) => {
                    let items = candidates_of(&v);
                    if items.is_empty() {
                        // An empty list is a normal outcome, not a fetch error — turn it into a
                        // helpful hint and stay on the input screen (Done::Err doesn't change mode).
                        let msg = if v.get("need_artist").and_then(Value::as_bool).unwrap_or(false) {
                            "add the artist too, to pin the song".to_string()
                        } else {
                            format!("no catalog match for “{title}” — check the spelling")
                        };
                        Done::Err(msg)
                    } else {
                        Done::Candidates(items)
                    }
                }
                Err(e) => Done::Err(e),
            }
        }
    }
}

// ── rendering ──────────────────────────────────────────────────────────────────
fn ui(f: &mut Frame, app: &mut App) {
    match app.mode {
        Mode::Menu => menu_screen(f, app),
        Mode::Scan => scan_screen(f, app),
        Mode::Library => library_screen(f, app),
        Mode::Playlist => playlist_screen(f, app),
        Mode::ByName => byname_screen(f, app),
        Mode::Candidates => candidates_screen(f, app),
        _ => query_screen(f, app),
    }
}

/// One result/recommendation row: `score  Title  artists  (pop)`.
fn rec_item(r: &Value) -> ListItem<'static> {
    row_item(None, r)
}

/// A playlist row, numbered to convey the (transition-ordered) sequence.
fn pl_item((i, r): (usize, &Value)) -> ListItem<'static> {
    row_item(Some(i + 1), r)
}

fn row_item(pos: Option<usize>, r: &Value) -> ListItem<'static> {
    let mut spans = Vec::new();
    if let Some(n) = pos {
        spans.push(Span::styled(format!("{n:>2}. "), Style::default().fg(Color::DarkGray)));
    } else {
        let score = r.get("score").and_then(Value::as_f64).map(|x| format!("{x:.2} ")).unwrap_or_default();
        spans.push(Span::styled(score, Style::default().fg(Color::Yellow)));
    }
    let pop = s(r, "popularity");
    spans.push(Span::styled(s(r, "title"), Style::default().fg(TITLE).add_modifier(Modifier::BOLD)));
    spans.push(Span::raw("  "));
    spans.push(Span::styled(s(r, "artists"), Style::default().fg(Color::Gray)));
    spans.push(Span::styled(
        if pop.is_empty() { String::new() } else { format!("  ({pop})") },
        Style::default().fg(Color::DarkGray),
    ));
    ListItem::new(Line::from(spans))
}

fn outer(title: impl Into<String>) -> Block<'static> {
    Block::default()
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .border_style(Style::default().fg(VIOLET))
        .title(Span::styled(format!(" {} ", title.into()), Style::default().fg(LAVENDER).bold()))
}

fn footer(app: &App, help: &str) -> Paragraph<'static> {
    let busy = app.busy();
    let spin = if busy { format!("{} ", SPIN[app.spinner]) } else { String::new() };
    let line = Line::from(vec![
        Span::styled("Zinthos ", Style::default().fg(LAVENDER).bold()),
        Span::styled(format!("· {TAGLINE}  "), Style::default().fg(LAVENDER).dim().italic()),
        Span::styled(
            format!("{spin}{} ", app.status),
            Style::default().fg(if busy { Color::Yellow } else { Color::Green }),
        ),
        Span::raw("│ "),
        Span::styled(help.to_string(), Style::default().fg(Color::DarkGray)),
    ]);
    Paragraph::new(line)
}

fn menu_screen(f: &mut Frame, app: &App) {
    let block = outer("Zinthos 🐦‍⬛");
    let inner = block.inner(f.area());
    f.render_widget(block, f.area());

    // ambient motes first; every foreground widget below overwrites the cells it covers,
    // leaving stars only in the empty gaps.
    ambient(f, inner, app.frame);

    let rows = Layout::vertical([
        Constraint::Length(1),  // pad
        Constraint::Length(8),  // banner
        Constraint::Length(1),  // chant
        Constraint::Length(1),  // divider
        Constraint::Length(3),  // description
        Constraint::Length(1),  // divider
        Constraint::Min(0),     // menu
        Constraint::Length(1),  // footer
    ])
    .split(inner);

    f.render_widget(Paragraph::new(banner_lines(app.frame)).alignment(Alignment::Center), rows[1]);
    f.render_widget(
        Paragraph::new(Line::from(Span::styled(CHANT, Style::default().fg(LAVENDER).dim().italic())))
            .alignment(Alignment::Center),
        rows[2],
    );
    f.render_widget(Paragraph::new(divider(rows[3].width, "⟡")), rows[3]);

    let desc: Vec<Line> = DESC
        .iter()
        .map(|l| Line::from(Span::styled(*l, Style::default().fg(Color::Gray).italic())))
        .collect();
    f.render_widget(Paragraph::new(desc).alignment(Alignment::Center), rows[4]);
    f.render_widget(Paragraph::new(divider(rows[5].width, "⟡")), rows[5]);

    // tiles need room for the longest label across every column, plus height for even the
    // squat 3-tall cards; below either threshold, fall back to a stacked list.
    if rows[6].width >= menu_min_w() && rows[6].height >= menu_grid_h(*CARD_HEIGHTS.last().unwrap())
    {
        menu_tiles(f, rows[6], app);
    } else {
        menu_list(f, rows[6], app);
    }

    f.render_widget(
        footer(app, "←/→/↑/↓ move · 1-4 or Enter select · q quit"),
        rows[7],
    );
}

/// Option B — a vertical stack where only the active option is boxed; the rest are quiet
/// rows. The selected card grows to 3 lines, so the eye lands on exactly one thing.
#[allow(dead_code)]
fn menu_cards(f: &mut Frame, area: Rect, app: &App) {
    let sel = app.menu_sel;
    let h = |i: usize| if i == sel { 3 } else { 1 };
    let rows = Layout::vertical([
        Constraint::Length(h(0)),
        Constraint::Length(1), // gap
        Constraint::Length(h(1)),
        Constraint::Length(1), // gap
        Constraint::Length(h(2)),
        Constraint::Min(0),
    ])
    .split(area);
    let slots = [rows[0], rows[2], rows[4]];

    for (i, (glyph, name, sub)) in TILES.iter().enumerate() {
        let on = i == sel;
        let line = Line::from(vec![
            Span::styled(format!("{glyph}  "), Style::default().fg(if on { CRIMSON } else { VIOLET })),
            Span::styled(
                *name,
                if on { Style::default().fg(LAVENDER).bold() } else { Style::default().fg(Color::Gray) },
            ),
            Span::styled("  —  ", Style::default().fg(Color::DarkGray)),
            Span::styled(
                *sub,
                Style::default().fg(if on { Color::Gray } else { Color::DarkGray }).italic(),
            ),
            Span::styled(format!("   {}", i + 1), Style::default().fg(Color::DarkGray)),
        ]);
        if on {
            let block = Block::default()
                .borders(Borders::ALL)
                .border_type(BorderType::Rounded)
                .border_style(Style::default().fg(LAVENDER).bold());
            let inner = block.inner(slots[i]);
            f.render_widget(block, slots[i]);
            f.render_widget(Paragraph::new(line), inner);
        } else {
            // indent the quiet rows to sit under the boxed card's text
            f.render_widget(Paragraph::new(line), slots[i].inner(Margin { horizontal: 2, vertical: 0 }));
        }
    }
}

/// Card heights the menu grid will try, tallest first; the last is the floor.
const CARD_HEIGHTS: [u16; 3] = [7, 5, 3];

/// Narrowest menu area the tile grid can hold, derived from the longest label rather than a
/// fixed number so renaming an option can't silently start clipping it. Per column: the label,
/// the glyph a squat card prefixes onto the same row, the border ring, the inter-card gap and
/// a column of breathing room either side.
fn menu_min_w() -> u16 {
    let longest = TILES.iter().map(|(_, n, _)| n.chars().count()).max().unwrap_or(0) as u16;
    (longest + 9) * MENU_COLS as u16
}

/// Rows the tile grid needs for a given card height: the bands themselves, a blank row
/// between each pair, then the gap + subtitle line underneath.
fn menu_grid_h(card_h: u16) -> u16 {
    let bands = TILES.len().div_ceil(MENU_COLS) as u16;
    bands * card_h + (bands - 1) + 2
}

/// Option A — the options as a `MENU_COLS`-wide grid of cards, with the active one's
/// subtitle below. Four options read as a 2×2 block rather than a long row.
fn menu_tiles(f: &mut Frame, area: Rect, app: &App) {
    let bands = TILES.len().div_ceil(MENU_COLS) as u16;
    // take the tallest card the space allows: 7 rows is the roomy default (glyph and name
    // floating in air), 5 trims the breathing room, 3 is the squat one-line fallback. The
    // whole grid + blank gap + subtitle must fit, or the bands get squeezed and the cards
    // lose their interior rows — `menu_grid_h` is the arithmetic menu_screen sizes against.
    let card_h = *CARD_HEIGHTS
        .iter()
        .find(|h| area.height >= menu_grid_h(**h))
        .unwrap_or(CARD_HEIGHTS.last().unwrap());
    let grid_h = bands * card_h + (bands - 1); // one blank row between card bands

    let vt = Layout::vertical([
        Constraint::Length(grid_h),
        Constraint::Length(1), // gap
        Constraint::Length(1), // subtitle of the active tile
        Constraint::Min(0),
    ])
    .split(area);

    // the grid spans the full width: each card runs edge-to-midpoint, midpoint-to-edge.
    let grid = vt[0];
    let mut band_h = Vec::new();
    for b in 0..bands {
        if b > 0 {
            band_h.push(Constraint::Length(1)); // gap
        }
        band_h.push(Constraint::Length(card_h));
    }
    let rows = Layout::vertical(band_h).split(grid);
    let n = MENU_COLS as u32;
    let cells: Vec<Rect> = rows
        .iter()
        .step_by(2) // skip the gap rows
        .flat_map(|r| {
            Layout::horizontal(vec![Constraint::Ratio(1, n); MENU_COLS])
                .split(*r)
                .to_vec()
        })
        .collect();

    for (i, (glyph, name, _sub)) in TILES.iter().enumerate() {
        let on = i == app.menu_sel;
        let cell = cells[i].inner(Margin { horizontal: 1, vertical: 0 }); // gap between cards
        // all tiles are boxed with the same dark fill; only the *brightness* of the border,
        // glyph and name changes — the active one lights up, the others rest dim.
        let border_style = if on {
            Style::default().fg(LAVENDER).bold()
        } else {
            Style::default().fg(VIOLET).dim()
        };
        let glyph_style = if on {
            Style::default().fg(CRIMSON).bold()
        } else {
            Style::default().fg(VIOLET).dim()
        };
        let name_style = if on {
            Style::default().fg(LAVENDER).bold().underlined()
        } else {
            Style::default().fg(Color::Gray).dim()
        };
        let num_style = if on {
            Style::default().fg(CRIMSON).bold()
        } else {
            Style::default().fg(Color::DarkGray)
        };
        // no fill — only the border ring + text are drawn, so the starfield shows through.
        let block = Block::default()
            .borders(Borders::ALL)
            .border_type(BorderType::Rounded)
            .border_style(border_style)
            .title(Span::styled(format!(" {} ", i + 1), num_style));
        let inner = block.inner(cell);
        f.render_widget(block, cell);
        // a 3-tall card has one interior row, so glyph and name share it there.
        let mut lines = if card_h >= 4 {
            vec![
                Line::from(Span::styled(*glyph, glyph_style)),
                Line::from(Span::styled(*name, name_style)),
            ]
        } else {
            vec![Line::from(vec![
                Span::styled(format!("{glyph}  "), glyph_style),
                Span::styled(*name, name_style),
            ])]
        };
        // Paragraph draws from the top, so pad the leftover interior rows onto the front to
        // sit the text in the middle of a roomy card (bias upward on an odd remainder).
        for _ in 0..(inner.height.saturating_sub(lines.len() as u16) / 2) {
            lines.insert(0, Line::default());
        }
        f.render_widget(Paragraph::new(lines).alignment(Alignment::Center), inner);
    }

    let sub = TILES[app.menu_sel].2;
    f.render_widget(
        Paragraph::new(Line::from(Span::styled(
            format!("❝ {sub} ❞"),
            Style::default().fg(LAVENDER).dim().italic(),
        )))
        .alignment(Alignment::Center),
        vt[2],
    );
}

/// Narrow-terminal fallback — the options as a stacked list (glyph · name — subtitle).
fn menu_list(f: &mut Frame, area: Rect, app: &App) {
    let items: Vec<Line> = TILES
        .iter()
        .enumerate()
        .map(|(i, (glyph, name, sub))| {
            let on = i == app.menu_sel;
            let marker = if on { "  ▶ " } else { "    " };
            let style = if on {
                Style::default().fg(LAVENDER).bold()
            } else {
                Style::default().fg(Color::Gray)
            };
            Line::from(vec![
                Span::styled(marker, Style::default().fg(CRIMSON)),
                Span::styled(format!("{glyph}  "), Style::default().fg(if on { CRIMSON } else { VIOLET })),
                Span::styled(format!("{name} — {sub}"), style),
                Span::styled(format!("  {}", i + 1), Style::default().fg(Color::DarkGray)),
            ])
        })
        .collect();
    f.render_widget(Paragraph::new(items), area);
}

fn scan_screen(f: &mut Frame, app: &App) {
    let block = outer("Scan Local Library");
    let inner = block.inner(f.area());
    f.render_widget(block, f.area());
    ambient(f, inner, app.frame);

    let rows = Layout::vertical([
        Constraint::Length(1),  // pad
        Constraint::Length(8),  // banner
        Constraint::Length(2),  // pad
        Constraint::Length(3),  // input
        Constraint::Min(0),     // hint
        Constraint::Length(1),  // footer
    ])
    .split(inner);

    f.render_widget(Paragraph::new(banner_lines(app.frame)).alignment(Alignment::Center), rows[1]);
    input_box(f, rows[3], "Enter Directory Path", &app.scan, true);
    f.render_widget(
        Paragraph::new(Line::from(Span::styled(
            "Point Zinthos at a folder of audio files — it reads their tags, matches them to the catalog, and recommends more.",
            Style::default().fg(Color::DarkGray),
        )))
        .alignment(Alignment::Center)
        .wrap(Wrap { trim: true }),
        rows[4],
    );
    f.render_widget(footer(app, "Enter scan · Esc back"), rows[5]);
}

fn playlist_screen(f: &mut Frame, app: &App) {
    let block = outer("Transition Playlist");
    let inner = block.inner(f.area());
    f.render_widget(block, f.area());
    ambient(f, inner, app.frame);

    let rows = Layout::vertical([
        Constraint::Length(1),  // pad
        Constraint::Length(8),  // banner
        Constraint::Length(2),  // pad
        Constraint::Length(3),  // input
        Constraint::Min(0),     // hint
        Constraint::Length(1),  // footer
    ])
    .split(inner);

    f.render_widget(Paragraph::new(banner_lines(app.frame)).alignment(Alignment::Center), rows[1]);
    input_box(f, rows[3], "Describe a Mood", &app.playlist, true);
    f.render_widget(
        Paragraph::new(Line::from(Span::styled(
            "e.g. \"chill study\", \"late-night drive\", \"workout hype\" — Zinthos builds a playlist ordered for smooth tempo/key/energy transitions.",
            Style::default().fg(Color::DarkGray),
        )))
        .alignment(Alignment::Center)
        .wrap(Wrap { trim: true }),
        rows[4],
    );
    f.render_widget(footer(app, "Enter build · Esc back"), rows[5]);
}

/// Similar-by-name, step 1: a two-field form (title + artist). Tab/↑/↓ move focus; only the
/// focused box shows the caret. Enter resolves the name to catalog candidates.
fn byname_screen(f: &mut Frame, app: &App) {
    let block = outer("Similar to a Song");
    let inner = block.inner(f.area());
    f.render_widget(block, f.area());
    ambient(f, inner, app.frame);

    let rows = Layout::vertical([
        Constraint::Length(1), // pad
        Constraint::Length(8), // banner
        Constraint::Length(1), // pad
        Constraint::Length(3), // title input
        Constraint::Length(3), // artist input
        Constraint::Min(0),    // hint
        Constraint::Length(1), // footer
    ])
    .split(inner);

    f.render_widget(Paragraph::new(banner_lines(app.frame)).alignment(Alignment::Center), rows[1]);
    input_box(f, rows[3], "Song Title", &app.byname_title, app.byname_focus == 0);
    input_box(f, rows[4], "Artist", &app.byname_artist, app.byname_focus == 1);
    f.render_widget(
        Paragraph::new(Line::from(Span::styled(
            "Name a song you love (title + artist) — Zinthos finds that track, then recommends others that sound like it.",
            Style::default().fg(Color::DarkGray),
        )))
        .alignment(Alignment::Center)
        .wrap(Wrap { trim: true }),
        rows[5],
    );
    f.render_widget(footer(app, "Tab switch field · Enter find · Esc back"), rows[6]);
}

/// Similar-by-name, step 2: the pick-list. Choosing a row (Enter/s) fires Job::Similar — the
/// same path the results/library screens use — so the last hop is already-proven code.
fn candidates_screen(f: &mut Frame, app: &mut App) {
    let block = outer("Pick a Track");
    let inner = block.inner(f.area());
    f.render_widget(block, f.area());

    let rows = Layout::vertical([
        Constraint::Length(2), // header
        Constraint::Min(0),    // candidates
        Constraint::Length(1), // footer
    ])
    .split(inner);

    let header = Line::from(vec![
        Span::styled("Which one? ", Style::default().fg(LAVENDER).bold()),
        Span::styled(
            "pick the track you mean, then get songs like it",
            Style::default().fg(Color::DarkGray).italic(),
        ),
    ]);
    f.render_widget(Paragraph::new(header), rows[0]);

    let items: Vec<ListItem> = app.results.iter().map(rec_item).collect();
    let list = List::new(items)
        .block(outer(format!("Matches ({})", app.results.len())))
        .highlight_style(Style::default().bg(Color::Blue).add_modifier(Modifier::BOLD))
        .highlight_symbol("▶ ");
    f.render_stateful_widget(list, rows[1], &mut app.list);

    let help = if app.vim {
        "j/k move · Enter/s find similar · Esc edit name · q menu"
    } else {
        "↑/↓ move · Enter/s find similar · Esc edit name · q menu"
    };
    f.render_widget(footer(app, help), rows[2]);
}

fn library_screen(f: &mut Frame, app: &mut App) {
    let block = outer("Your Library");
    let inner = block.inner(f.area());
    f.render_widget(block, f.area());
    // No ambient sky on results-bearing screens: the animated starfield redraws every tick
    // (defeating mouse text-selection) and its motes overlap the rows, dimming glyphs. The
    // decorative landing screens (menu/scan/playlist) keep the sky; this one stays calm.

    let bd_height = (app.lib_genres.len().min(6) as u16 + 2).max(3);
    let rows = Layout::vertical([
        Constraint::Length(2),          // header
        Constraint::Length(bd_height),  // taste breakdown
        Constraint::Min(0),             // recommendations
        Constraint::Length(1),          // footer
    ])
    .split(inner);

    let header = Line::from(vec![
        Span::styled(
            format!("Matched {}/{}", app.lib_matched, app.lib_total),
            Style::default().fg(Color::Green).bold(),
        ),
        Span::styled(format!("   ·   {} unmatched", app.lib_unmatched), Style::default().fg(Color::DarkGray)),
        Span::styled(
            format!("   ·   {} recommendations", app.results.len()),
            Style::default().fg(Color::Cyan),
        ),
    ]);
    f.render_widget(Paragraph::new(header), rows[0]);

    // taste breakdown: top genres as bars, eras on one line
    let maxc = app.lib_genres.iter().map(|(_, c)| *c).max().unwrap_or(1).max(1);
    let mut bd = vec![Line::from(Span::styled(
        "taste breakdown",
        Style::default().fg(Color::Magenta).italic(),
    ))];
    for (g, c) in app.lib_genres.iter().take(6) {
        let w = ((*c as f64 / maxc as f64) * 20.0).round() as usize;
        bd.push(Line::from(vec![
            Span::styled(format!("{g:<12}"), Style::default().fg(Color::Gray)),
            Span::styled("█".repeat(w.max(1)), Style::default().fg(Color::Magenta)),
            Span::styled(format!(" {c}"), Style::default().fg(Color::DarkGray)),
        ]));
    }
    if !app.lib_eras.is_empty() {
        let eras = app
            .lib_eras
            .iter()
            .map(|(d, c)| format!("{d}:{c}"))
            .collect::<Vec<_>>()
            .join("  ");
        bd.push(Line::from(Span::styled(format!("eras  {eras}"), Style::default().fg(Color::DarkGray))));
    }
    f.render_widget(Paragraph::new(bd), rows[1]);

    let items: Vec<ListItem> = app.results.iter().map(rec_item).collect();
    let list = List::new(items)
        .block(outer(format!("Recommendations ({})", app.results.len())))
        .highlight_style(Style::default().bg(Color::Blue).add_modifier(Modifier::BOLD))
        .highlight_symbol("▶ ");
    f.render_stateful_widget(list, rows[2], &mut app.list);

    let help = if app.vim {
        "j/k move · Enter open · s similar · Esc menu"
    } else {
        "↑/↓ move · Enter open · s similar · Esc menu"
    };
    f.render_widget(footer(app, help), rows[3]);
}

fn query_screen(f: &mut Frame, app: &mut App) {
    // No ambient sky here — see library_screen: results & the detail card (incl. the preview
    // URL) must stay selectable and un-dimmed, so this screen doesn't animate a starfield
    // behind the text. The menu/scan/playlist landing screens keep the sky.

    let rows = Layout::vertical([Constraint::Length(3), Constraint::Min(0), Constraint::Length(1)])
        .split(f.area());

    // The similars list came from a SEED TRACK, not a typed query, so the editable Query box
    // is replaced by a read-only reminder of what's being matched — which the screen otherwise
    // never states. See the Mode::Results key handler for the matching key changes.
    let is_sim = app.source == "similar" && app.mode != Mode::Search;
    if is_sim {
        label_box(f, rows[0], "Similar to", &app.similar_seed);
    } else {
        input_box(f, rows[0], "Query", &app.query, app.mode == Mode::Search);
    }

    match app.mode {
        Mode::Detail => {
            let d = app.detail.clone().unwrap_or(Value::Null);
            let lines = vec![
                Line::from(s(&d, "title").bold().cyan()),
                Line::from(format!("artists : {}", s(&d, "artists"))),
                Line::from(format!("album   : {}", s(&d, "album_title"))),
                Line::from(format!("released: {}", s(&d, "release_date"))),
                Line::from(format!("pop     : {}", s(&d, "popularity"))),
                Line::from(format!("preview : {}", s(&d, "preview_url"))),
            ];
            f.render_widget(
                Paragraph::new(lines)
                    .wrap(Wrap { trim: true })
                    .block(outer("Track")),
                rows[1],
            );
        }
        _ => {
            let is_pl = app.source == "playlist";
            let items: Vec<ListItem> = if is_pl {
                app.results.iter().enumerate().map(pl_item).collect()
            } else {
                app.results.iter().map(rec_item).collect()
            };
            let label = if is_pl { "Playlist" } else if is_sim { "Similar" } else { "Results" };
            let list = List::new(items)
                .block(outer(format!("{label} ({})", app.results.len())))
                .highlight_style(Style::default().bg(Color::Blue).add_modifier(Modifier::BOLD))
                .highlight_symbol("▶ ");
            f.render_stateful_widget(list, rows[1], &mut app.list);
        }
    }

    let mv = if app.vim { "j/k move" } else { "↑/↓ move" };
    let help = match app.mode {
        Mode::Search => "←/→ edit · Ctrl-W del word · Enter search · ↓ results · Esc menu".into(),
        // No `r`/`/` for similars: there is no query behind them to reshuffle or edit.
        Mode::Results if is_sim => format!("{mv} · Enter open · s similar · Esc back · q menu"),
        Mode::Results => format!("{mv} · Enter open · s similar · r reshuffle · / edit · Esc query · q menu"),
        Mode::Detail => "s similar · Esc back".into(),
        _ => String::new(),
    };
    f.render_widget(footer(app, &help), rows[2]);
}

fn main() -> std::io::Result<()> {
    let mut url = DEFAULT_URL.to_string();
    let mut vim = false;
    let mut spawn = false;
    let mut backend = "../backend".to_string();
    let mut args = std::env::args().skip(1);
    while let Some(a) = args.next() {
        match a.as_str() {
            "--url" => url = args.next().unwrap_or(url),
            "--backend" => backend = args.next().unwrap_or(backend),
            "--vim" => vim = true,
            "--spawn" => spawn = true,
            "-h" | "--help" => {
                println!("sonic-tui [--url URL] [--vim] [--spawn] [--backend DIR]");
                return Ok(());
            }
            _ => {}
        }
    }

    let mut app = App::new(url, backend, vim);
    app.ensure_engine(spawn);

    let mut terminal = ratatui::init();
    let res = run(&mut terminal, &mut app);
    ratatui::restore();
    res
}

fn run(terminal: &mut ratatui::DefaultTerminal, app: &mut App) -> std::io::Result<()> {
    loop {
        terminal.draw(|f| ui(f, app))?;
        app.tick();
        if event::poll(Duration::from_millis(100))? {
            if let Event::Key(k) = event::read()? {
                if k.kind == KeyEventKind::Press && app.on_key(k.code, k.modifiers) {
                    return Ok(());
                }
            }
        }
    }
}






