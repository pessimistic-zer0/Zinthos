//! Library match diagnostic — walks a folder, asks the engine how each file resolves
//! (ISRC / fuzzy / unmatched), and writes a JSON report you can grep/jq for triage.
//!
//!   cargo run --bin diagnose -- [--url URL] <dir> [out.json]
//!
//! Default URL http://127.0.0.1:3000, default out ./library_report.json. The engine must be
//! running (it owns the catalog; this tool only reads tags, like the TUI client).

use lofty::prelude::*;
use serde_json::{json, Map, Value};
use std::collections::HashMap;
use walkdir::WalkDir;

const AUDIO_EXT: [&str; 11] =
    ["mp3", "flac", "m4a", "mp4", "aac", "ogg", "opus", "wav", "wv", "aiff", "ape"];

struct Local {
    path: String,
    title: String,
    artist: String,
    isrc: Option<String>,
}

fn extract(root: &str) -> Vec<Local> {
    let mut out = Vec::new();
    for entry in WalkDir::new(root).into_iter().filter_map(Result::ok) {
        let p = entry.path();
        if !entry.file_type().is_file() {
            continue;
        }
        let ok = p
            .extension()
            .and_then(|e| e.to_str())
            .map(|e| AUDIO_EXT.contains(&e.to_ascii_lowercase().as_str()))
            .unwrap_or(false);
        if !ok {
            continue;
        }
        let Ok(tagged) = lofty::read_from_path(p) else { continue };
        let Some(tag) = tagged.primary_tag().or_else(|| tagged.first_tag()) else { continue };
        let title = tag.title().map(|c| c.to_string()).unwrap_or_default();
        let artist = tag.artist().map(|c| c.to_string()).unwrap_or_default();
        let isrc = tag.get_string(ItemKey::Isrc).map(str::to_string);
        if isrc.is_none() && title.is_empty() {
            continue;
        }
        out.push(Local { path: p.display().to_string(), title, artist, isrc });
    }
    out
}

fn main() {
    let mut url = "http://127.0.0.1:3000".to_string();
    let mut positional: Vec<String> = Vec::new();
    let mut args = std::env::args().skip(1);
    while let Some(a) = args.next() {
        match a.as_str() {
            "--url" => url = args.next().unwrap_or(url),
            "-h" | "--help" => {
                println!("diagnose [--url URL] <dir> [out.json]");
                return;
            }
            _ => positional.push(a),
        }
    }
    let Some(root) = positional.first().cloned() else {
        eprintln!("usage: diagnose [--url URL] <dir> [out.json]");
        std::process::exit(2);
    };
    let out_path = positional.get(1).cloned().unwrap_or_else(|| "library_report.json".into());

    eprintln!("scanning {root} …");
    let locals = extract(&root);
    eprintln!("read tags from {} files; querying engine …", locals.len());

    let tracks: Vec<Value> = locals
        .iter()
        .map(|l| json!({"title": l.title, "artist": l.artist, "isrc": l.isrc}))
        .collect();

    let agent = ureq::AgentBuilder::new().build();
    let resp: Value = match agent
        .post(&format!("{url}/library/diagnose"))
        .send_json(json!({"tracks": tracks, "size": 1}))
    {
        Ok(r) => r.into_json().expect("engine returned non-JSON"),
        Err(e) => {
            eprintln!("engine request failed (is it running at {url}?): {e}");
            std::process::exit(1);
        }
    };

    // Engine results are keyed by input index; zip the local path/tags back in.
    let mut by_input: HashMap<u64, &Value> = HashMap::new();
    if let Some(arr) = resp.get("results").and_then(Value::as_array) {
        for r in arr {
            if let Some(i) = r.get("input").and_then(Value::as_u64) {
                by_input.insert(i, r);
            }
        }
    }

    let mut rows: Vec<Value> = Vec::with_capacity(locals.len());
    for (i, l) in locals.iter().enumerate() {
        let r = by_input.get(&(i as u64));
        let method = r.and_then(|r| r.get("method")).cloned().unwrap_or(Value::Null);
        let track_id = r.and_then(|r| r.get("track_id")).cloned().unwrap_or(Value::Null);
        let cat_title = r.and_then(|r| r.get("catalog_title")).cloned().unwrap_or(Value::Null);
        let cat_artists = r.and_then(|r| r.get("catalog_artists")).cloned().unwrap_or(Value::Null);
        let mut m = Map::new();
        m.insert("path".into(), json!(l.path));
        m.insert("title".into(), json!(l.title));
        m.insert("artist".into(), json!(l.artist));
        m.insert("isrc".into(), json!(l.isrc));
        m.insert("method".into(), method);
        m.insert("track_id".into(), track_id);
        m.insert("catalog_title".into(), cat_title);
        m.insert("catalog_artists".into(), cat_artists);
        rows.push(Value::Object(m));
    }

    // Sort so the things worth eyeballing float to the top: misses, then fuzzy, then isrc.
    let rank = |v: &Value| match v.get("method").and_then(Value::as_str) {
        Some("none") | None => 0,
        Some("fuzzy") => 1,
        _ => 2,
    };
    rows.sort_by(|a, b| rank(a).cmp(&rank(b)).then_with(|| {
        a.get("title").and_then(Value::as_str).unwrap_or("")
            .cmp(b.get("title").and_then(Value::as_str).unwrap_or(""))
    }));

    let methods = resp.get("methods").cloned().unwrap_or(json!({}));
    let report = json!({
        "root": root,
        "engine": url,
        "summary": {
            "total": locals.len(),
            "isrc": methods.get("isrc").cloned().unwrap_or(json!(0)),
            "fuzzy": methods.get("fuzzy").cloned().unwrap_or(json!(0)),
            "unmatched": methods.get("none").cloned().unwrap_or(json!(0)),
        },
        "tracks": rows,
    });

    let f = std::fs::File::create(&out_path).expect("could not create report file");
    serde_json::to_writer_pretty(f, &report).expect("could not write report");
    eprintln!(
        "wrote {out_path}  ·  isrc {} · fuzzy {} · unmatched {}",
        methods.get("isrc").cloned().unwrap_or(json!(0)),
        methods.get("fuzzy").cloned().unwrap_or(json!(0)),
        methods.get("none").cloned().unwrap_or(json!(0)),
    );
}
