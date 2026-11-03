//! Run settings: which files are hostile-input code (declared by the project), which opt-in rule groups are on.
//!
//! The project declares the hostile-input scope as globs in `[audit.rust] hostile` (the core passes them as
//! `--hostile`). Only when it declares none do path name tokens apply, as a rough fallback default.

/// Fallback name tokens for hostile-input code when the project declares no globs.
pub const DEFAULT_TOKENS: &[&str] = &[
    "pars", "lex", "net", "proto", "codec", "decod", "deserial", "wire", "socket", "packet", "input", "replay", "json",
    "share", "log", "session",
];

/// Default limit for the `file_size` rule, in lines.
pub const DEFAULT_MAX_FILE_LINES: usize = 1000;

/// Everything the checks need to know about the project.
pub struct Config {
    /// Path/module globs declared by the project (`**` crosses folders, `*` and `?` stay inside one name).
    pub globs: Vec<String>,
    /// Lower-case path fragments, used only when `globs` is empty.
    pub tokens: Vec<String>,
    /// Project adopted the `networked` practice (G5 checks run).
    pub networked: bool,
    /// Project adopted the `realtime` practice (G8 checks run).
    pub realtime: bool,
    /// Files longer than this many lines get a `file_size` finding.
    pub max_file_lines: usize,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            globs: vec![],
            tokens: DEFAULT_TOKENS.iter().map(|s| s.to_string()).collect(),
            networked: false,
            realtime: false,
            max_file_lines: DEFAULT_MAX_FILE_LINES,
        }
    }
}

impl Config {
    /// Does `file` (project-relative, `/` separators) hold code that reads untrusted input?
    pub fn is_hostile(&self, file: &str) -> bool {
        if !self.globs.is_empty() {
            return self.globs.iter().any(|g| glob_matches(g, file));
        }
        let f = file.to_lowercase();
        self.tokens.iter().any(|t| f.contains(t.as_str()))
    }
}

/// Module-style `a::b` is read as the path `a/b`; a pattern with no wildcard also matches a folder prefix and
/// the same path with `.rs` or `/mod.rs` appended (a module name).
pub fn glob_matches(pattern: &str, path: &str) -> bool {
    let pat = pattern.replace("::", "/").replace('\\', "/");
    let pat = pat.trim_start_matches("./");
    if match_segments(&pat.split('/').collect::<Vec<_>>(), &path.split('/').collect::<Vec<_>>()) {
        return true;
    }
    if pat.contains(['*', '?']) {
        return false;
    }
    path.starts_with(&format!("{pat}/"))
        || path == format!("{pat}.rs")
        || path.ends_with(&format!("/{pat}.rs"))
        || path.ends_with(&format!("/{pat}/mod.rs"))
}

fn match_segments(pat: &[&str], path: &[&str]) -> bool {
    match pat.split_first() {
        None => path.is_empty(),
        Some((&"**", rest)) => (0..=path.len()).any(|i| match_segments(rest, &path[i..])),
        Some((p, rest)) => match path.split_first() {
            Some((s, tail)) => match_name(p.as_bytes(), s.as_bytes()) && match_segments(rest, tail),
            None => false,
        },
    }
}

/// `*` any run of characters, `?` one character, within a single path segment.
fn match_name(p: &[u8], s: &[u8]) -> bool {
    match p.split_first() {
        None => s.is_empty(),
        Some((b'*', rest)) => (0..=s.len()).any(|i| match_name(rest, &s[i..])),
        Some((b'?', rest)) => !s.is_empty() && match_name(rest, &s[1..]),
        Some((c, rest)) => s.first() == Some(c) && match_name(rest, &s[1..]),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn globs_and_modules() {
        assert!(glob_matches("crates/core/src/replay.rs", "crates/core/src/replay.rs"));
        assert!(glob_matches("**/replay.rs", "crates/core/src/replay.rs"));
        assert!(glob_matches("crates/net/**", "crates/net/src/wire.rs"));
        assert!(glob_matches("crates/net", "crates/net/src/wire.rs"));
        assert!(glob_matches("src::replay", "crates/core/src/replay.rs"));
        assert!(glob_matches("src/*.rs", "src/a.rs"));
        assert!(!glob_matches("src/*.rs", "src/a/b.rs"));
        assert!(glob_matches("replay", "crates/core/src/replay.rs"));
        assert!(!glob_matches("crates/ne", "crates/net/src/wire.rs"));
    }

    #[test]
    fn declared_globs_replace_tokens() {
        let mut c = Config::default();
        assert!(c.is_hostile("core/src/replay.rs"));
        c.globs = vec!["only/this.rs".into()];
        assert!(!c.is_hostile("core/src/replay.rs"));
        assert!(c.is_hostile("only/this.rs"));
    }
}
