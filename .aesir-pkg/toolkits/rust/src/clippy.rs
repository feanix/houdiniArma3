//! `cargo clippy` with the Aesir lint preset (#82, #88). One finding per occurrence.
//!
//! The message names the enclosing function and the identity ignores digits, so a finding keeps its identity when
//! lines move. Function-level lints carry the function's line range. Noisy per-file lints (`missing_docs`) stay
//! one finding per file, since `doc_coverage` already lists the items.

use crate::config::Config;
use crate::finding::Finding;
use crate::source::FileInfo;
use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::path::Path;
use std::process::Command;

/// Lints switched on for the audit (missing_docs is rustc's; clippy passes it through).
const PRESET: &[&str] = &[
    "clippy::unwrap_used",
    "clippy::expect_used",
    "clippy::indexing_slicing",
    "clippy::arithmetic_side_effects",
    "clippy::let_underscore_must_use",
    "clippy::too_many_lines",
    "clippy::cognitive_complexity",
    "clippy::string_slice",
    "clippy::map_err_ignore",
    "clippy::unused_result_ok",
    "clippy::cast_possible_truncation",
    "clippy::undocumented_unsafe_blocks", // 'unsafe needs a SAFETY comment'
    "missing_docs",
];

/// Lints that only count inside the project's hostile-input scope (code that reads untrusted data).
const SCOPED: &[&str] = &[
    "clippy::indexing_slicing",
    "clippy::arithmetic_side_effects",
    "clippy::string_slice",
    "clippy::cast_possible_truncation",
];
/// Lints that do not count in test code.
const NOT_IN_TESTS: &[&str] = &[
    "clippy::unwrap_used",
    "clippy::expect_used",
    "clippy::indexing_slicing",
    "clippy::arithmetic_side_effects",
    "clippy::string_slice",
    "clippy::cast_possible_truncation",
];
/// Lints about a whole function: the message carries its line range.
const FN_LEVEL: &[&str] = &["clippy::too_many_lines", "clippy::cognitive_complexity"];
/// Lints reported once per file.
const PER_FILE: &[&str] = &["missing_docs"];

/// A crash on outside data is the worst thing these lints find, so inside the hostile scope they rank high.
fn severity(rule: &str, level: &str, hostile: bool) -> &'static str {
    if level == "error" {
        return "high";
    }
    match rule {
        "clippy::unwrap_used" | "clippy::expect_used" | "clippy::indexing_slicing" | "clippy::string_slice" if hostile => "high",
        "clippy::unwrap_used"
        | "clippy::expect_used"
        | "clippy::indexing_slicing"
        | "clippy::string_slice"
        | "clippy::arithmetic_side_effects"
        | "clippy::cast_possible_truncation"
        | "clippy::let_underscore_must_use"
        | "clippy::map_err_ignore"
        | "clippy::unused_result_ok"
        | "clippy::undocumented_unsafe_blocks" => "medium",
        _ => "low",
    }
}

/// `let _ = write!(buf, ..)` where `buf` is a `String`: fmt::Write on a String cannot fail, so ignoring the result is fine.
fn infallible_string_write(info: &FileInfo, first: usize, last: usize) -> bool {
    let stmt = info.text(first, last);
    // clippy's span may start at the `let` or at the expression after it
    let rest = stmt.trim_start();
    let rest = rest.strip_prefix("let _").map_or(rest, |r| r.trim_start().trim_start_matches('=').trim_start());
    let Some(args) = rest.strip_prefix("write!(").or_else(|| rest.strip_prefix("writeln!(")) else { return false };
    let target: String = args.split(',').next().unwrap_or("").trim().trim_start_matches("&mut ").trim_start_matches('*').trim().to_string();
    if target.is_empty() || !target.chars().all(|c| c.is_alphanumeric() || c == '_' || c == '.') {
        return false; // an expression: cannot tell what it is
    }
    let name = target.rsplit('.').next().unwrap_or("");
    let (a, b) = if target.contains('.') {
        (1, info.lines.len()) // a field: look through the whole file for its declaration
    } else {
        info.enclosing(first).map_or((1, info.lines.len()), |f| (f.start, f.end))
    };
    info.text(a, b).lines().any(|l| declares_string(l, name))
}

/// Does `line` declare `name` as a String (annotation, `&mut String` parameter, or a String-producing initialiser)?
fn declares_string(line: &str, name: &str) -> bool {
    let mut from = 0;
    while let Some(i) = line[from..].find(name) {
        let at = from + i;
        from = at + name.len();
        let before = line[..at].chars().next_back();
        let rest = &line[from..];
        if before.is_some_and(|c| c.is_alphanumeric() || c == '_') || rest.chars().next().is_some_and(|c| c.is_alphanumeric() || c == '_') {
            continue;
        }
        let rest = rest.trim_start();
        if let Some(ty) = rest.strip_prefix(':') {
            let ty = ty.trim_start().trim_start_matches('&');
            let ty = ty.trim_start().strip_prefix('\'').map_or(ty, |t| t.trim_start_matches(|c: char| c.is_alphanumeric() || c == '_'));
            let ty = ty.trim_start().trim_start_matches("mut ").trim_start();
            if ty.starts_with("String") || ty.starts_with("std::string::String") {
                return true;
            }
        } else if let Some(init) = rest.strip_prefix('=') {
            if !init.starts_with('=') && ["String::", "format!(", ".to_string()", ".to_owned()", ".into_string()"].iter().any(|p| init.contains(p)) {
                return true;
            }
        }
    }
    false
}

/// Run clippy in `root` and turn each warning into a finding. Errors when cargo/clippy produced nothing usable.
pub fn run(root: &Path, cfg: &Config, infos: &HashMap<String, FileInfo>) -> Result<Vec<Finding>, String> {
    // Fixed path: cargo tracks CLIPPY_CONF_DIR, a changing one would defeat its cache.
    let conf = std::env::temp_dir().join("aesir-audit-rust-clippy-conf");
    std::fs::create_dir_all(&conf).map_err(|e| e.to_string())?;
    let want = "allow-unwrap-in-tests = true\nallow-expect-in-tests = true\ntoo-many-lines-threshold = 100\ncognitive-complexity-threshold = 25\n";
    let file = conf.join("clippy.toml");
    // Rewrite only on change (keeps cargo's cache) and via rename, so a parallel audit never reads a half-written file.
    if std::fs::read_to_string(&file).ok().as_deref() != Some(want) {
        let tmp = conf.join(format!("clippy.toml.{}.tmp", std::process::id()));
        std::fs::write(&tmp, want).map_err(|e| e.to_string())?;
        std::fs::rename(&tmp, &file).map_err(|e| e.to_string())?;
    }
    let mut cmd = Command::new("cargo");
    cmd.args(["clippy", "--message-format=json", "--all-targets", "--quiet", "--"]);
    for l in PRESET {
        cmd.args(["-W", l]);
    }
    let out = cmd.current_dir(root).env("CLIPPY_CONF_DIR", &conf).output().map_err(|e| format!("cannot run cargo: {e}"))?;
    let mut res: Vec<Finding> = Vec::new();
    // per-file rules: (rule, file) -> (count, first line, first message, severity)
    let mut grouped: BTreeMap<(String, String), (usize, usize, String, &'static str)> = BTreeMap::new();
    let mut seen = BTreeSet::new();
    let mut parsed = 0;
    for l in String::from_utf8_lossy(&out.stdout).lines() {
        let Ok(v) = serde_json::from_str::<Value>(l) else { continue };
        if v["reason"] != "compiler-message" {
            continue;
        }
        parsed += 1;
        let m = &v["message"];
        let level = m["level"].as_str().unwrap_or("");
        if level != "warning" && level != "error" {
            continue;
        }
        let Some(span) = m["spans"].as_array().and_then(|s| s.iter().find(|x| x["is_primary"] == true)) else { continue };
        let file = span["file_name"].as_str().unwrap_or("").replace('\\', "/");
        if file.is_empty() || file.starts_with('/') || file.starts_with("..") || file.contains(':') {
            continue; // outside the project (registry, rustc sources)
        }
        let rule = match m["code"]["code"].as_str() {
            Some(c) if c.starts_with('E') && c[1..].chars().all(|d| d.is_ascii_digit()) => format!("rustc::{c}"),
            Some(c) => c.to_string(),
            None => format!("rustc::{level}"),
        };
        let line = span["line_start"].as_u64().unwrap_or(0) as usize;
        let line_end = span["line_end"].as_u64().unwrap_or(line as u64) as usize;
        let text = m["message"].as_str().unwrap_or("").lines().next().unwrap_or("").to_string();
        let info = infos.get(&file);
        let hostile = cfg.is_hostile(&file);
        if SCOPED.contains(&rule.as_str()) && !hostile {
            continue;
        }
        if NOT_IN_TESTS.contains(&rule.as_str()) && info.map_or(crate::source::test_path(&file), |i| i.in_test(line)) {
            continue;
        }
        if rule == "clippy::let_underscore_must_use" && info.is_some_and(|i| infallible_string_write(i, line, line_end)) {
            continue;
        }
        if !seen.insert((rule.clone(), file.clone(), line, text.clone())) {
            continue; // same warning from the lib and its test target
        }
        let sev = severity(&rule, level, hostile);
        if PER_FILE.contains(&rule.as_str()) {
            let e = grouped.entry((rule, file)).or_insert((0, line, text, sev));
            e.0 += 1;
            e.1 = e.1.min(line);
            continue;
        }
        let func = info.and_then(|i| i.enclosing(line));
        let mut message = match func {
            Some(f) => format!("in `{}`: {text}", f.name),
            None => text,
        };
        if FN_LEVEL.contains(&rule.as_str()) {
            let (a, b) = func.map_or((line, line_end), |f| (f.start, f.end));
            message.push_str(&format!(" (lines {a}-{b})"));
        }
        res.push(Finding { message, file, line, severity: sev, rule });
    }
    if !out.status.success() && parsed == 0 {
        let err = String::from_utf8_lossy(&out.stderr);
        let tail: Vec<&str> = err.lines().rev().take(3).collect();
        return Err(format!("cargo clippy failed (is clippy installed? `rustup component add clippy`): {}", tail.join(" | ")));
    }
    res.extend(grouped.into_iter().map(|((rule, file), (n, line, text, sev))| Finding {
        message: if n > 1 { format!("{n} occurrences, first: {text}") } else { text },
        file,
        line,
        severity: sev,
        rule,
    }));
    res.sort_by(|a, b| b.rank().cmp(&a.rank()).then(a.file.cmp(&b.file)).then(a.line.cmp(&b.line)));
    Ok(res)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn exempt(src: &str, line: usize) -> bool {
        infallible_string_write(&FileInfo::new("src/x.rs", src), line, line)
    }

    #[test]
    fn string_writes_are_exempt_io_writes_are_not() {
        let s = "use std::fmt::Write;\nfn a() {\n    let mut out = String::new();\n    let _ = write!(out, \"{}\", 1);\n}\n\
                 fn b(buf: &mut String) {\n    let _ = writeln!(buf, \"x\");\n}\n\
                 fn c(w: &mut Vec<u8>, names: Vec<String>) {\n    let _ = write!(w, \"x\");\n}\n\
                 fn d(names: Vec<String>) {\n    let _ = write!(names, \"x\");\n}\n";
        assert!(exempt(s, 4));
        assert!(exempt(s, 7));
        assert!(!exempt(s, 10));
        assert!(!exempt(s, 13)); // Vec<String> is not String
    }
}
