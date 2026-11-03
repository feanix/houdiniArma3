//! Text-level rules that clippy has no lint for (#82, #88): file size, test hygiene, hostile-input hygiene,
//! non-atomic writes, and the opt-in `networked` / `realtime` groups.
//!
//! These are deliberately simple line heuristics: each reports the line it fired on, names the enclosing
//! function in the message (so identity survives moved lines), and skips test code unless it targets tests.

use crate::config::Config;
use crate::finding::Finding;
use crate::source::FileInfo;
use std::collections::{HashMap, HashSet};

pub(crate) fn finding(file: &str, line: usize, severity: &'static str, rule: &str, info: &FileInfo, msg: &str) -> Finding {
    let message = match info.enclosing(line) {
        Some(f) => format!("in `{}`: {msg}", f.name),
        None => msg.to_string(),
    };
    Finding { file: file.into(), line, severity, rule: rule.into(), message }
}

/// Code part of a line: text before a `//` comment.
pub(crate) fn code(line: &str) -> &str {
    line.split("//").next().unwrap_or("")
}

/// Is `arg` a literal, a constant (ALL_CAPS path) or clamped with `.min(`? Those cannot be sized by outside data.
fn bounded_size(arg: &str) -> bool {
    let a = arg.trim();
    let last = a.rsplit("::").next().unwrap_or(a);
    let literal = a.chars().next().is_some_and(|c| c.is_ascii_digit());
    let constant = !last.is_empty() && last.chars().all(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || c == '_');
    a.contains(".min(") || literal || constant
}

/// The argument of the first `needle(` on a line, balanced parentheses.
fn call_arg<'a>(line: &'a str, needle: &str) -> Option<&'a str> {
    let at = line.find(needle)? + needle.len();
    let mut depth = 1;
    for (i, c) in line[at..].char_indices() {
        match c {
            '(' => depth += 1,
            ')' => {
                depth -= 1;
                if depth == 0 {
                    return Some(&line[at..at + i]);
                }
            }
            _ => {}
        }
    }
    None
}

/// Size taken from `vec![x; N]`.
fn vec_repeat_len(line: &str) -> Option<&str> {
    let at = line.find("vec![")? + 5;
    let end = line[at..].find(']')? + at;
    line[at..end].split_once(';').map(|(_, n)| n)
}

/// Rules that apply to every file.
fn general(file: &str, info: &FileInfo, cfg: &Config, out: &mut Vec<Finding>) {
    let n = info.lines.len();
    if n > cfg.max_file_lines {
        let sev = if n > cfg.max_file_lines * 2 { "medium" } else { "low" };
        out.push(Finding {
            file: file.into(),
            line: 0,
            severity: sev,
            rule: "file_size".into(),
            message: format!("{n} lines (limit {}): split by responsibility", cfg.max_file_lines),
        });
    }
    let has_rename = info.lines.iter().any(|l| code(l).contains("rename("));
    for (i, l) in info.lines.iter().enumerate() {
        let ln = i + 1;
        let c = code(l);
        if info.in_test(ln) {
            continue;
        }
        if !has_rename && (c.contains("fs::write(") || c.contains("File::create(")) {
            out.push(finding(file, ln, "low", "io::non_atomic_write", info, "writes a file in place; write a temp file and rename it so a crash cannot leave a torn file"));
        }
    }
}

/// Does this code text check something on its own (assert family, panic, unwrap of a Result, `?`)?
fn asserts_inline(text: &str) -> bool {
    ["assert", "panic!", "unreachable!", ".unwrap(", ".expect(", ".unwrap_err(", ".expect_err("].iter().any(|p| text.contains(p))
}

/// Does this code text assert or panic on its own? A helper that merely unwraps a Result does not count: its
/// callers still check nothing about the outcome (VT: a `grow()` helper that unwraps hid a test with no assertion).
fn asserts_strict(text: &str) -> bool {
    ["assert", "panic!", "unreachable!"].iter().any(|p| text.contains(p))
}

/// Function and macro names called in `text` (an identifier directly followed by `(` or `!`).
fn called_names(text: &str) -> Vec<String> {
    let mut res = Vec::new();
    let mut cur = String::new();
    let chars: Vec<char> = text.chars().collect();
    for (i, &c) in chars.iter().enumerate() {
        if c.is_alphanumeric() || c == '_' {
            cur.push(c);
            continue;
        }
        if !cur.is_empty() && (c == '(' || (c == '!' && chars.get(i + 1) == Some(&'('))) {
            res.push(std::mem::take(&mut cur));
        }
        cur.clear();
    }
    res
}

/// Names of crate functions (and macros) that assert or panic, directly or through another such helper, so a test that
/// only calls one of them still counts as asserting. Name-based: a same-named function elsewhere also counts,
/// which errs toward silence.
fn asserting_helpers(infos: &HashMap<String, FileInfo>) -> HashSet<String> {
    let mut helpers: HashSet<String> = HashSet::new();
    for _ in 0..4 {
        let before = helpers.len();
        for info in infos.values() {
            for f in info.fns.iter().filter(|f| !f.test_attr) {
                let body = fn_body(info, f);
                if asserts_strict(&body) || called_names(&body).iter().any(|n| helpers.contains(n)) {
                    helpers.insert(f.name.clone());
                }
            }
            for l in &info.lines {
                if let Some(rest) = code(l).trim().strip_prefix("macro_rules!") {
                    helpers.insert(rest.trim().split(|c: char| !c.is_alphanumeric() && c != '_').next().unwrap_or("").to_string());
                }
            }
        }
        if helpers.len() == before {
            break;
        }
    }
    helpers
}

/// The body of `f` without comments and without its signature (a test named `asserts_nothing` is not asserting).
fn fn_body(info: &FileInfo, f: &crate::source::FnInfo) -> String {
    let text = no_comments(&info.text(f.start, f.end));
    match text.find('{') {
        Some(at) => text[at..].to_string(),
        None => text,
    }
}

/// `text` with `//` comments dropped line by line.
pub(crate) fn no_comments(text: &str) -> String {
    text.lines().map(code).collect::<Vec<_>>().join("
")
}

/// Names that read as an assertion even when their body is out of sight (dev-dependency helpers).
fn assertion_named(name: &str) -> bool {
    ["assert", "check", "verify", "expect", "ensure", "validate"].iter().any(|p| name.starts_with(p))
}

/// A `#[test]` that asserts nothing proves only that the code did not crash (VT: a NaN-light test computed a
/// result and asserted nothing). Counts as asserting: any assert-family macro, panic, `#[should_panic]`, unwrap or
/// expect (a failed Result fails the test), `?` in a test returning a Result, a call to a helper that asserts.
fn no_assertion(file: &str, info: &FileInfo, helpers: &HashSet<String>, out: &mut Vec<Finding>) {
    for f in info.fns.iter().filter(|f| f.test_attr && !f.should_panic) {
        let body = fn_body(info, f);
        let calls = called_names(&body);
        let checks = asserts_inline(&body)
            || (f.returns_result && body.contains('?'))
            || calls.iter().any(|n| helpers.contains(n) || assertion_named(n));
        if !checks {
            out.push(Finding {
                file: file.into(),
                line: f.start,
                severity: "low",
                rule: "test_hygiene::no_assertion".into(),
                message: format!("in `{}`: test has no assertion (no assert, panic, should_panic, unwrap or asserting helper); it passes whatever the code computes", f.name),
            });
        }
    }
}

/// Test-hygiene rules: wall-clock asserts and shared temp names.
fn test_hygiene(file: &str, info: &FileInfo, out: &mut Vec<Finding>) {
    for f in info.fns.iter().filter(|f| f.test) {
        let body = &info.lines[f.start.saturating_sub(1)..f.end.min(info.lines.len())];
        let clock = body.iter().position(|l| {
            let c = code(l);
            c.contains("Instant::now") || c.contains("SystemTime::now") || c.contains(".elapsed()")
        });
        if let Some(i) = clock {
            if body.iter().any(|l| code(l).contains("assert")) {
                out.push(Finding {
                    file: file.into(),
                    line: f.start + i,
                    severity: "low",
                    rule: "test_hygiene::wall_clock".into(),
                    message: format!("in `{}`: test asserts on wall-clock time; use a fake clock or a step count", f.name),
                });
            }
        }
    }
    for (i, l) in info.lines.iter().enumerate() {
        let c = code(l);
        if info.in_test(i + 1) && (c.contains("temp_dir().join(\"") || c.contains("\"/tmp/") || c.contains("\"C:\\\\Temp")) {
            out.push(finding(file, i + 1, "low", "test_hygiene::shared_temp_name", info, "fixed temp path shared between runs; use a unique directory (tempfile, pid + counter)"));
        }
    }
}

/// Rules for code that reads untrusted input (the project's hostile scope).
fn hostile(file: &str, info: &FileInfo, tests_corpus: &str, out: &mut Vec<Finding>) {
    for (i, l) in info.lines.iter().enumerate() {
        let ln = i + 1;
        if info.in_test(ln) {
            continue;
        }
        let c = code(l);
        let sized = ["with_capacity(", ".reserve(", ".resize(", ".reserve_exact("]
            .iter()
            .find_map(|p| call_arg(c, p).map(|a| a.split(',').next().unwrap_or(a)))
            .or_else(|| vec_repeat_len(c));
        if let Some(arg) = sized {
            if !bounded_size(arg) {
                out.push(finding(file, ln, "medium", "hostile::unbounded_alloc", info, &format!("size `{}` is not a constant or clamped; bound outside data before it sizes memory", arg.trim())));
            }
        }
        let stream_read = c.contains("read_to_end(") || (c.contains("read_to_string(") && !c.contains("fs::read_to_string"));
        if stream_read && !c.contains(".take(") {
            out.push(finding(file, ln, "medium", "hostile::unbounded_read", info, "reads a stream to the end with no limit; wrap it in `.take(MAX)`"));
        }
    }
    let stem = file.rsplit('/').next().unwrap_or(file).trim_end_matches(".rs");
    let has_tests = info.fns.iter().any(|f| f.test) || (!stem.is_empty() && tests_corpus.contains(stem));
    if !has_tests {
        out.push(Finding {
            file: file.into(),
            line: 0,
            severity: "low",
            rule: "hostile::no_hostile_test".into(),
            message: "hostile-input code with no test: add a table test of empty, truncated, max-value and nested input".into(),
        });
    }
}

/// G5, only for projects that adopted `networked`: blocking receives need a deadline.
fn networked(file: &str, info: &FileInfo, out: &mut Vec<Finding>) {
    let text = info.lines.join("\n");
    if text.contains("set_read_timeout") || text.contains("set_nonblocking") || text.contains("timeout(") {
        return;
    }
    for (i, l) in info.lines.iter().enumerate() {
        let c = code(l);
        if !info.in_test(i + 1) && (c.contains(".recv(") || c.contains(".recv_from(") || c.contains(".accept(")) {
            out.push(finding(file, i + 1, "medium", "networked::no_deadline", info, "blocking receive with no timeout in this file; every wait needs a deadline"));
        }
    }
}

/// G8, only for projects that adopted `realtime`: no file IO in per-frame / per-tick code.
fn realtime(file: &str, info: &FileInfo, out: &mut Vec<Finding>) {
    const HOT: &[&str] = &["update", "tick", "frame", "step", "render", "draw"];
    for f in info.fns.iter().filter(|f| !f.test && HOT.iter().any(|h| f.name.to_lowercase().contains(h))) {
        for ln in f.start..=f.end.min(info.lines.len()) {
            let c = code(&info.lines[ln - 1]);
            if ["fs::", "File::", "OpenOptions", "read_to_string(", "println!", "eprintln!"].iter().any(|p| c.contains(p)) {
                out.push(finding(file, ln, "medium", "realtime::io_in_hot_path", info, "file or console IO in per-frame code; move it off the hot path or cache it"));
            }
        }
    }
}

/// All text-level rules over the parsed files.
pub fn check(infos: &HashMap<String, FileInfo>, tests_corpus: &str, cfg: &Config) -> Vec<Finding> {
    let mut out = Vec::new();
    let mut files: Vec<&String> = infos.keys().collect();
    files.sort();
    let helpers = asserting_helpers(infos);
    for &file in &files {
        let info = &infos[file];
        general(file, info, cfg, &mut out);
        test_hygiene(file, info, &mut out);
        no_assertion(file, info, &helpers, &mut out);
        if cfg.is_hostile(file) && !crate::source::test_path(file) {
            hostile(file, info, tests_corpus, &mut out);
        }
        if cfg.networked && cfg.is_hostile(file) {
            networked(file, info, &mut out);
        }
        if cfg.realtime {
            realtime(file, info, &mut out);
        }
    }
    crate::state_rules::check(infos, &files, &mut out);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn run(file: &str, src: &str, cfg: &Config) -> Vec<(String, usize)> {
        let mut m = HashMap::new();
        m.insert(file.to_string(), FileInfo::new(file, src));
        check(&m, "", cfg).into_iter().map(|f| (f.rule, f.line)).collect()
    }

    #[test]
    fn sizes_from_outside_data_are_flagged() {
        let src = "fn p(n: usize) {\n    let a = Vec::with_capacity(n);\n    let b = Vec::with_capacity(16);\n    let c = Vec::with_capacity(n.min(MAX));\n    let d = vec![0u8; n];\n}\n#[test]\nfn t() {}\n";
        let r = run("src/wire.rs", src, &Config::default());
        let lines: Vec<usize> = r.iter().filter(|(k, _)| k == "hostile::unbounded_alloc").map(|x| x.1).collect();
        assert_eq!(lines, vec![2, 5]);
    }

    #[test]
    fn test_hygiene_rules() {
        let src = "#[test]\nfn slow() {\n    let t = std::time::Instant::now();\n    let p = std::env::temp_dir().join(\"fixed\");\n    assert!(t.elapsed().as_secs() < 1);\n}\n";
        let r = run("src/a.rs", src, &Config::default());
        assert!(r.contains(&("test_hygiene::wall_clock".to_string(), 3)));
        assert!(r.contains(&("test_hygiene::shared_temp_name".to_string(), 4)));
    }

    fn lines_of(r: &[(String, usize)], rule: &str) -> Vec<usize> {
        r.iter().filter(|(k, _)| k == rule).map(|x| x.1).collect()
    }

    // Planted: `nan_light_asserts_nothing` (line 11) computes and prints but checks nothing. The rest are near-misses.
    const NO_ASSERT_SRC: &str = "fn helper(x: u32) {\n    assert_eq!(x, 1);\n}\nfn outer(x: u32) {\n    helper(x);\n}\nfn plain(x: u32) -> u32 {\n    x + 1\n}\n\
#[test]\nfn nan_light_asserts_nothing() {\n    let r = plain(2);\n    println!(\"{r}\");\n}\n\
#[test]\nfn direct() {\n    assert!(plain(0) == 1);\n}\n\
#[test]\nfn via_helper() {\n    outer(1);\n}\n\
#[test]\n#[should_panic]\nfn panics() {\n    plain(0);\n}\n\
#[test]\nfn unwraps() {\n    \"1\".parse::<u32>().unwrap();\n}\n\
#[test]\nfn question() -> Result<(), String> {\n    Err(\"x\".to_string())?;\n    Ok(())\n}\n\
#[cfg(test)]\nmod t {\n    fn setup() {}\n}\n";

    #[test]
    fn no_assertion_planted_and_near_misses() {
        let r = run("src/a.rs", NO_ASSERT_SRC, &Config::default());
        assert_eq!(lines_of(&r, "test_hygiene::no_assertion"), vec![11]);
        // Moving the test keeps the message, so the finding keeps its identity.
        let mut m = HashMap::new();
        m.insert("src/a.rs".to_string(), FileInfo::new("src/a.rs", &format!("// pad\n{NO_ASSERT_SRC}")));
        let moved = check(&m, "", &Config::default());
        let msg = moved.iter().find(|f| f.rule == "test_hygiene::no_assertion").map(|f| f.message.clone());
        assert_eq!(msg, Some("in `nan_light_asserts_nothing`: test has no assertion (no assert, panic, should_panic, unwrap or asserting helper); it passes whatever the code computes".to_string()));
    }

    #[test]
    fn opt_in_groups_stay_off_by_default() {
        let src = "fn tick() {\n    let s = std::fs::read_to_string(\"x\");\n}\nfn r(s: &std::net::UdpSocket) {\n    s.recv_from(&mut []);\n}\n";
        assert!(run("src/a.rs", src, &Config::default()).iter().all(|(k, _)| !k.starts_with("realtime") && !k.starts_with("networked")));
        let cfg = Config { realtime: true, networked: true, ..Config::default() };
        let r = run("src/net.rs", src, &cfg);
        assert!(r.iter().any(|(k, _)| k == "realtime::io_in_hot_path"));
        assert!(r.iter().any(|(k, _)| k == "networked::no_deadline"));
    }

    #[test]
    fn file_size_limit() {
        let cfg = Config { max_file_lines: 3, ..Config::default() };
        assert!(run("src/a.rs", "fn a() {}\n\n\n\n", &cfg).iter().any(|(k, _)| k == "file_size"));
    }
}
