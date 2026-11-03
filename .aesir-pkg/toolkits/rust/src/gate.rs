//! Gate feature coverage (G11): a Cargo feature that no gate command or CI file mentions is built and tested by nobody.
//!
//! "Gate text" is `aesir.toml` plus common CI/task files. `--all-features` anywhere covers every feature.

use crate::finding::Finding;
use std::path::Path;

/// Files whose text counts as "what the gate runs".
const GATE_FILES: &[&str] = &["aesir.toml", "Makefile", "makefile", "justfile", "Justfile", "Taskfile.yml", ".gitlab-ci.yml"];

fn gate_text(root: &Path) -> String {
    let mut text = String::new();
    for f in GATE_FILES {
        text.push_str(&std::fs::read_to_string(root.join(f)).unwrap_or_default());
    }
    if let Ok(rd) = std::fs::read_dir(root.join(".github").join("workflows")) {
        for e in rd.flatten() {
            text.push_str(&std::fs::read_to_string(e.path()).unwrap_or_default());
        }
    }
    text
}

/// Feature names declared in a manifest, with their 1-based lines (`default` excluded).
fn features(manifest: &str) -> Vec<(String, usize)> {
    let mut in_features = false;
    let mut res = Vec::new();
    for (i, l) in manifest.lines().enumerate() {
        let t = l.trim();
        if t.starts_with('[') {
            in_features = t == "[features]";
        } else if in_features && !t.starts_with('#') {
            if let Some((name, _)) = t.split_once('=') {
                let name = name.trim().trim_matches('"');
                if !name.is_empty() && name != "default" {
                    res.push((name.to_string(), i + 1));
                }
            }
        }
    }
    res
}

fn mentioned(text: &str, name: &str) -> bool {
    text.match_indices(name).any(|(i, _)| {
        let before = text[..i].chars().next_back();
        let after = text[i + name.len()..].chars().next();
        let word = |c: Option<char>| c.is_some_and(|c| c.is_alphanumeric() || c == '_' || c == '-');
        !word(before) && !word(after)
    })
}

/// One finding per feature the gate never names (and no `--all-features`).
pub fn uncovered_features(root: &Path, files: &[String]) -> Vec<Finding> {
    let text = gate_text(root);
    if text.contains("--all-features") {
        return vec![];
    }
    let mut res = Vec::new();
    for f in files.iter().filter(|f| f.ends_with("Cargo.toml") && !f.split('/').any(|c| c == "tests")) {
        let Ok(src) = std::fs::read_to_string(root.join(f)) else { continue };
        for (name, line) in features(&src) {
            if !mentioned(&text, &name) {
                res.push(Finding {
                    file: f.clone(),
                    line,
                    severity: "medium",
                    rule: "gate::feature_not_built".into(),
                    message: format!("feature `{name}` is never named by the gate: add --all-features or --features {name} to the gate commands"),
                });
            }
        }
    }
    res
}

/// Does the gate text run non-ignored tests with the release profile? `-- --ignored` alone does not count: the
/// everyday tests would still only ever run in debug. Commands are cut at quotes, commas, `&&`, `;` and newlines.
fn tests_release(text: &str) -> bool {
    text.split(['\n', '"', ',', '&', ';', '|']).any(|cmd| {
        let release = cmd.contains("--release") || cmd.contains("--profile release") || cmd.contains("--profile=release");
        let tests = cmd.contains("cargo test") || cmd.contains("cargo nextest") || cmd.contains("cargo t ");
        let only_ignored = cmd.contains("--ignored") && !cmd.contains("--include-ignored");
        release && tests && !only_ignored
    })
}

/// G11 companion: when release is what ships (a gate step or CI builds `--release`, a manifest tunes
/// `[profile.release]`, or the project has a binary) but no gate command tests with `--release`, code that
/// behaves differently there is never tested (VT: a `debug_assertions`-only conservation check, and a test that
/// failed under `--release`). Nothing is reported when the project has no gate text at all.
pub fn release_not_tested(root: &Path, manifests: &[String], rs_files: &[String]) -> Vec<Finding> {
    let text = gate_text(root);
    if text.trim().is_empty() || tests_release(&text) {
        return vec![];
    }
    let tuned = manifests.iter().any(|m| std::fs::read_to_string(root.join(m)).is_ok_and(|t| t.lines().any(|l| l.trim() == "[profile.release]")));
    let binary = rs_files.iter().any(|f| f == "src/main.rs" || f.ends_with("/src/main.rs") || f.contains("/src/bin/") || f.starts_with("src/bin/"));
    if !(text.contains("--release") || tuned || binary) {
        return vec![];
    }
    let file = GATE_FILES.iter().find(|f| root.join(f).is_file()).map_or("Cargo.toml", |f| f);
    vec![Finding {
        file: file.into(),
        line: 0,
        severity: "low",
        rule: "gate::release_not_tested".into(),
        message: "release is what ships but no gate command runs non-ignored tests with --release: add e.g. `cargo test --release` to a gate profile, since debug-only checks and overflow behaviour differ there".into(),
    }]
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn release_test_commands() {
        assert!(tests_release("quick = [\"cargo test --workspace\", \"cargo test --release -q\"]"));
        assert!(tests_release("run: cargo build && cargo nextest run --release"));
        assert!(tests_release("cargo test --release -- --include-ignored"));
        // Near-misses: ignored-only, build-only, debug-only.
        assert!(!tests_release("full = [\"cargo test --workspace --release -q -- --ignored full_\"]"));
        assert!(!tests_release("cargo build --release\ncargo test --workspace"));
    }

    #[test]
    fn release_gap_is_reported_only_when_release_ships() {
        let dir = std::env::temp_dir().join(format!("aesir_gate_rel_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let toml = dir.join("aesir.toml");
        std::fs::write(&toml, "[gates]\nquick = [\"cargo test -q\"]\nsoak = [\"cargo test --release -- --ignored soak_\"]\n").unwrap();
        // Planted: the soak lane builds release, the tests never run there.
        assert_eq!(release_not_tested(&dir, &[], &[]).len(), 1);
        // Near-miss: a library gate that never mentions release, no binary, no tuned profile.
        std::fs::write(&toml, "[gates]\nquick = [\"cargo test -q\"]\n").unwrap();
        assert!(release_not_tested(&dir, &[], &["src/lib.rs".to_string()]).is_empty());
        // A binary crate ships release.
        assert_eq!(release_not_tested(&dir, &[], &["src/main.rs".to_string()]).len(), 1);
        // Covered: a non-ignored release test step.
        std::fs::write(&toml, "[gates]\nfull = [\"cargo test --release -q\"]\n").unwrap();
        assert!(release_not_tested(&dir, &[], &["src/main.rs".to_string()]).is_empty());
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn reads_features_and_mentions() {
        let m = "[package]\nname = \"x\"\n[features]\ndefault = []\nnet = []\n# c = []\nfancy-ui = [\"net\"]\n[dependencies]\nz = \"1\"\n";
        assert_eq!(features(m), vec![("net".to_string(), 5), ("fancy-ui".to_string(), 7)]);
        assert!(mentioned("cargo test --features net", "net"));
        assert!(!mentioned("cargo test --features network", "net"));
    }
}
