//! Aesir audit toolkit for Rust. Prints findings (format v1, see docs/audit-findings.md) as JSON lines.
//!
//! Run in the project root: `aesir-audit-rust [--base REF] [--scope a,b,c] [--hostile GLOB]... [--max-file-lines N]
//! [--networked] [--realtime]`, started by `aesir audit run`, which resolves aesir.toml and adds `--via-audit-run`.
//! Started bare it refuses (it would skip the project's settings) unless given `--no-config`.
//! Checks: clippy with the lint preset (one finding per occurrence), text rules (file size, test hygiene, hostile
//! input, atomic writes, assertion-free tests, debug-only invariants, unbounded growth, capacity nesting),
//! gate feature and release coverage, doc coverage of pub items, comment-only diff vs a base branch.
//! `--hostile` globs are the project-declared hostile-input scope; `--scope` name tokens apply only without them.
//! Any failure prints a message on stderr and exits 1, so the core imports nothing from a half-run.

mod clippy;
mod config;
mod diff;
mod docs;
mod finding;
mod gate;
mod rules;
mod source;
mod state_rules;

use config::Config;
use finding::Finding;
use source::FileInfo;
use std::collections::HashMap;
use std::path::Path;
use std::process::ExitCode;

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("aesir-audit-rust: {e}");
            ExitCode::FAILURE
        }
    }
}

fn run() -> Result<(), String> {
    let mut base = "main".to_string();
    let mut cfg = Config::default();
    let (mut via_run, mut no_config) = (false, false);
    let mut args = std::env::args().skip(1);
    while let Some(a) = args.next() {
        let mut val = |name: &str| args.next().ok_or_else(|| format!("{name} needs a value"));
        match a.as_str() {
            "--base" => base = val("--base")?,
            "--scope" => cfg.tokens = val("--scope")?.split(',').filter(|s| !s.is_empty()).map(str::to_lowercase).collect(),
            "--hostile" => cfg.globs.push(val("--hostile")?),
            "--max-file-lines" => cfg.max_file_lines = val("--max-file-lines")?.parse().map_err(|_| "--max-file-lines needs a number".to_string())?,
            "--via-audit-run" => via_run = true, // keep in step with audit.RUN_FLAG
            "--no-config" => no_config = true,   // keep in step with audit.NO_CONFIG_FLAG
            "--networked" => cfg.networked = true,
            "--realtime" => cfg.realtime = true,
            other => {
                return Err(format!(
                    "unknown argument {other} (usage: [--base REF] [--scope a,b,c] [--hostile GLOB]... [--max-file-lines N] [--networked] [--realtime] [--no-config])"
                ))
            }
        }
    }
    let root = std::env::current_dir().map_err(|e| e.to_string())?;
    if !(via_run || no_config) {
        return Err(bare_refusal(&root));
    }
    let files = rust_files(&root);
    let mut infos: HashMap<String, FileInfo> = HashMap::new();
    let mut tests_corpus = String::new();
    for f in &files {
        let Ok(src) = std::fs::read_to_string(root.join(f)) else { continue };
        if source::test_path(f) {
            tests_corpus.push_str(&src);
        }
        infos.insert(f.clone(), FileInfo::new(f, &src));
    }
    let mut all: Vec<Finding> = Vec::new();
    all.extend(clippy::run(&root, &cfg, &infos)?);
    all.extend(rules::check(&infos, &tests_corpus, &cfg));
    let manifests = manifests(&root);
    all.extend(gate::uncovered_features(&root, &manifests));
    all.extend(gate::release_not_tested(&root, &manifests, &files));
    all.extend(docs::coverage(&root, &files));
    all.extend(diff::comment_only(&root, &base));
    all.sort_by_key(|f| std::cmp::Reverse(f.rank())); // stable: each check's own order survives within a severity
    for f in &all {
        println!("{}", f.to_json());
    }
    Ok(())
}

/// Why a bare run is refused: it would skip the settings in aesir.toml (hostile globs, size limit, practice flags).
fn bare_refusal(root: &Path) -> String {
    let found = root.ancestors().map(|d| d.join("aesir.toml")).find(|p| p.is_file());
    let where_ = match found {
        Some(p) => format!(" in {}", p.display()),
        None => " (no aesir.toml found above here)".to_string(),
    };
    format!("run bare, this toolkit would skip the project's settings{where_}: use `aesir audit run`, or pass --no-config for an explicit run without them")
}

/// Tracked `.rs` files (git), or a plain walk outside a repository; `target/` and dot folders are skipped.
fn rust_files(root: &Path) -> Vec<String> {
    tracked(root, "*.rs", ".rs")
}

/// Tracked `Cargo.toml` manifests, same rules as `rust_files`.
fn manifests(root: &Path) -> Vec<String> {
    tracked(root, "Cargo.toml", "Cargo.toml")
}

fn tracked(root: &Path, pattern: &str, suffix: &str) -> Vec<String> {
    let pathspec = if pattern.starts_with('*') { pattern.to_string() } else { format!("**/{pattern}") };
    let out = std::process::Command::new("git").args(["ls-files", "-z", "--", &pathspec, pattern]).current_dir(root).output();
    if let Ok(o) = out {
        if o.status.success() {
            // Vendored Aesir copy and state are never the project's own code (same rule as the core's ALWAYS_EXCLUDED).
            return String::from_utf8_lossy(&o.stdout).split('\0').filter(|s| !s.is_empty() && !is_aesir_path(s)).map(String::from).collect();
        }
    }
    let mut found = Vec::new();
    walk(root, root, suffix, &mut found);
    found
}

fn is_aesir_path(rel: &str) -> bool {
    [".aesir-pkg", ".aesir"].iter().any(|d| rel == *d || rel.starts_with(&format!("{d}/")))
}

fn walk(root: &Path, dir: &Path, suffix: &str, found: &mut Vec<String>) {
    let Ok(rd) = std::fs::read_dir(dir) else { return };
    for e in rd.flatten() {
        let p = e.path();
        let name = e.file_name().to_string_lossy().to_string();
        if p.is_dir() {
            if name != "target" && !name.starts_with('.') {
                walk(root, &p, suffix, found);
            }
        } else if name.ends_with(suffix) {
            if let Ok(rel) = p.strip_prefix(root) {
                found.push(rel.to_string_lossy().replace('\\', "/"));
            }
        }
    }
}
