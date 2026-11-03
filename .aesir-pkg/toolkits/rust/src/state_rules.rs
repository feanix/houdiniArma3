//! Rules about state that outlives a step or a build profile (#124, from aesir-reports#52):
//! `debug_only_invariant`, `unbounded_growth`, `capacity_nesting`.
//!
//! Line heuristics over the parsed files, like `rules.rs`. Each message names the enclosing function or the
//! type and field, never a line number, so a finding keeps its identity when lines move.

use crate::finding::Finding;
use crate::rules::{code, finding, no_comments};
use crate::source::FileInfo;
use std::collections::{BTreeMap, HashMap};

/// Does this name read as an invariant check (`check`, `verify_totals`, `validate`, `world_invariants`)?
/// `checked_add` and friends are arithmetic, not checks.
fn check_like(name: &str) -> bool {
    ["check", "verify", "validate"].contains(&name)
        || ["check_", "verify_", "validate_"].iter().any(|p| name.starts_with(p))
        || ["_check", "_verify", "_validate", "_invariants"].iter().any(|s| name.ends_with(s))
}

/// First check-like call in `text`; a `fn check_x(` declaration is not a call.
fn check_call(text: &str) -> Option<String> {
    let chars: Vec<char> = text.chars().collect();
    let (mut cur, mut prev) = (String::new(), String::new());
    for (i, &c) in chars.iter().enumerate() {
        if c.is_alphanumeric() || c == '_' {
            cur.push(c);
            continue;
        }
        if !cur.is_empty() {
            let next = chars[i..].iter().find(|c| !c.is_whitespace());
            if next == Some(&'(') && prev != "fn" && check_like(&cur) {
                return Some(cur);
            }
            prev = std::mem::take(&mut cur);
        }
    }
    None
}

/// Text from `(line, col)` to the end of the first balanced `{..}` block, or to `;` if no block opens first,
/// or (with `parens`) to the closing `)` of the first parenthesis. Capped at 80 lines.
fn extent(info: &FileInfo, line: usize, col: usize, parens: bool) -> String {
    let (open, close) = if parens { ('(', ')') } else { ('{', '}') };
    let (mut depth, mut seen, mut out) = (0i32, false, String::new());
    for (k, l) in info.lines.iter().enumerate().skip(line - 1).take(80) {
        let c = code(l);
        let from = if k == line - 1 { col.min(c.len()) } else { 0 };
        for ch in c[from..].chars() {
            out.push(ch);
            if ch == open {
                depth += 1;
                seen = true;
            } else if ch == close {
                depth -= 1;
            }
            if (seen && depth <= 0) || (!parens && !seen && ch == ';') {
                return out;
            }
        }
        out.push('\n');
    }
    out
}

/// A check that only runs when `debug_assertions` is on guards nothing in a release build, and release is what
/// soak runs and users get (VT: the ledger's end_step conservation check ran only in debug).
/// Flags `if cfg!(debug_assertions) { ..check.. }`, `#[cfg(debug_assertions)]` on a statement or item that calls a
/// check, and `debug_assert!(..check..)`. Test code and `cfg(not(debug_assertions))` are skipped.
fn debug_only_invariant(file: &str, info: &FileInfo, out: &mut Vec<Finding>) {
    for (i, l) in info.lines.iter().enumerate() {
        let ln = i + 1;
        let c = code(l);
        if info.in_test(ln) {
            continue;
        }
        let mut text = None;
        if let Some(at) = c.find("cfg!(debug_assertions)") {
            if !c[..at].trim_end().ends_with('!') {
                text = Some(extent(info, ln, at, false));
            }
        } else if c.trim() == "#[cfg(debug_assertions)]" {
            // The attribute applies to the next non-attribute line's statement or item.
            if let Some(n) = (ln + 1..=info.lines.len()).find(|&n| !code(&info.lines[n - 1]).trim().starts_with("#[")) {
                text = Some(extent(info, n, 0, false));
            }
        } else if let Some(at) = c.find("debug_assert").filter(|_| c.contains('!')) {
            if let Some(p) = c[at..].find('(') {
                text = Some(extent(info, ln, at + p, true));
            }
        }
        if let Some(call) = text.as_deref().and_then(check_call) {
            out.push(finding(
                file,
                ln,
                "medium",
                "debug_only_invariant",
                info,
                &format!("`{call}` runs only when debug_assertions is on, so release and soak runs have no such check; run it in every build (or behind a runtime flag) and run the tests with --release"),
            ));
        }
    }
}

/// Crate a file belongs to: the path before `/src/`, or `""` for a root crate.
fn crate_key(file: &str) -> &str {
    file.find("/src/").map_or("", |p| &file[..p])
}

/// What one line does to the field called `name`, if it mentions `.name`.
#[derive(PartialEq)]
enum Use {
    Grow,
    Shrink,
}

/// Collection methods that add entries / take entries away or reset the whole collection.
const GROWS: &[&str] = &["insert", "push", "push_back", "push_front", "entry", "extend", "extend_from_slice", "append"];
const SHRINKS: &[&str] = &[
    "remove", "remove_entry", "retain", "retain_mut", "clear", "drain", "pop", "pop_front", "pop_back", "pop_first", "pop_last",
    "truncate", "swap_remove", "split_off", "take", "resize", "dedup", "dedup_by_key",
];

fn ident_at(s: &str) -> &str {
    let end = s.find(|c: char| !c.is_alphanumeric() && c != '_').unwrap_or(s.len());
    &s[..end]
}

/// Every use of `.name` on one code line.
fn field_uses(c: &str, name: &str) -> Vec<Use> {
    let pat = format!(".{name}");
    let mut res = Vec::new();
    for (at, _) in c.match_indices(&pat) {
        let rest = &c[at + pat.len()..];
        if rest.starts_with(|ch: char| ch.is_alphanumeric() || ch == '_') {
            continue;
        }
        if let Some(call) = rest.strip_prefix('.') {
            let m = ident_at(call);
            let after = &call[m.len()..];
            let bounded_by_len = m == "len" && after.starts_with("()") && c.contains(['<', '>']) && c.split(|ch: char| !ch.is_alphanumeric() && ch != '_').any(|w| w.len() > 2 && w.chars().all(|ch| ch.is_ascii_uppercase() || ch == '_'));
            if SHRINKS.contains(&m) || bounded_by_len {
                res.push(Use::Shrink);
            } else if GROWS.contains(&m) {
                res.push(Use::Grow);
            }
        } else {
            // `x.name = ..` resets it; `&mut x.name` handed to a function may empty it.
            let reassigned = rest.trim_start().starts_with('=') && !rest.trim_start().starts_with("==");
            let before = c[..at].trim_end_matches(|ch: char| ch.is_alphanumeric() || ch == '_' || ch == '.');
            if reassigned || before.ends_with("&mut ") || before.ends_with("&mut") {
                res.push(Use::Shrink);
            }
        }
    }
    res
}

/// Constructors and loaders build the collection once; growth there does not persist across steps.
fn constructor_like(fn_name: &str) -> bool {
    ["new", "default", "from", "with", "build", "load", "parse", "read", "init", "create", "make", "clone", "to_", "into", "decode", "deserialize"]
        .iter()
        .any(|p| fn_name == *p || fn_name.starts_with(&format!("{p}_")) || (*p == "to_" && fn_name.starts_with("to_")))
}

/// A `HashMap`/`BTreeMap`/`Vec` field that gets inserts or pushes in non-constructor code and is never removed
/// from, retained, cleared, drained, reassigned, or length-capped anywhere in its crate grows with every step
/// of a long run (VT: `since_branch` held an entry for every segment ever built). Matching is by field name.
fn unbounded_growth(infos: &HashMap<String, FileInfo>, files: &[&String], out: &mut Vec<Finding>) {
    let mut by_crate: BTreeMap<&str, Vec<&String>> = BTreeMap::new();
    for f in files {
        by_crate.entry(crate_key(f)).or_default().push(f);
    }
    for crate_files in by_crate.values() {
        let mut grows: HashMap<&str, bool> = HashMap::new();
        let mut shrinks: HashMap<&str, bool> = HashMap::new();
        let fields: Vec<(&String, &crate::source::FieldInfo)> = crate_files
            .iter()
            .flat_map(|f| infos[*f].fields.iter().filter(|fi| !infos[*f].in_test(fi.line)).map(move |fi| (*f, fi)))
            .collect();
        let mut vec_only: HashMap<&str, bool> = HashMap::new();
        for (_, fi) in &fields {
            grows.entry(&fi.name).or_insert(false);
            shrinks.entry(&fi.name).or_insert(false);
            let v = vec_only.entry(&fi.name).or_insert(true);
            *v &= fi.kind == "Vec";
        }
        for f in crate_files {
            let info = &infos[*f];
            for (i, l) in info.lines.iter().enumerate() {
                let ln = i + 1;
                let c = code(l);
                if !c.contains('.') || info.in_test(ln) {
                    continue;
                }
                let encl = info.enclosing(ln);
                let in_ctor = encl.is_none_or(|e| constructor_like(&e.name));
                let in_mut_self = encl.is_some_and(|e| e.mut_self);
                for name in grows.keys().copied().collect::<Vec<_>>() {
                    if !c.contains(name) {
                        continue;
                    }
                    // A Vec is also how results and reports are built, so it only counts as long-lived state
                    // when a `&mut self` method pushes to `self.<field>`; maps count wherever they are filled.
                    let vec_only = vec_only[name];
                    let persists = !in_ctor && (!vec_only || (in_mut_self && c.contains(&format!("self.{name}"))));
                    for u in field_uses(c, name) {
                        match u {
                            Use::Grow if persists => {
                                grows.insert(name, true);
                            }
                            Use::Shrink => {
                                shrinks.insert(name, true);
                            }
                            Use::Grow => {}
                        }
                    }
                }
            }
        }
        for (file, fi) in fields {
            if grows[fi.name.as_str()] && !shrinks[fi.name.as_str()] {
                out.push(Finding {
                    file: file.clone(),
                    line: fi.line,
                    severity: "low",
                    rule: "unbounded_growth".into(),
                    message: format!(
                        "`{}::{}` ({}) gets inserts but is never removed from, retained, cleared or drained anywhere in the crate; check it cannot grow without bound over a long run",
                        fi.strukt, fi.name, fi.kind
                    ),
                });
            }
        }
    }
}

/// One `const MAX_*` definition.
struct Cap {
    name: String,
    file: String,
    line: usize,
    value: Option<u128>,
}

/// Tiny evaluator for const initialisers: numbers, earlier consts, `* + -`, `<<`, parentheses, `as T`.
struct Eval<'a> {
    s: Vec<char>,
    i: usize,
    known: &'a HashMap<String, u128>,
}

impl Eval<'_> {
    fn ws(&mut self) {
        while self.s.get(self.i).is_some_and(|c| c.is_whitespace()) {
            self.i += 1;
        }
    }
    fn eat(&mut self, t: &str) -> bool {
        self.ws();
        let t: Vec<char> = t.chars().collect();
        if self.s[self.i..].starts_with(&t) {
            self.i += t.len();
            return true;
        }
        false
    }
    fn shift(&mut self) -> Option<u128> {
        let mut v = self.add()?;
        while self.eat("<<") {
            v = v.checked_shl(u32::try_from(self.add()?).ok()?)?;
        }
        Some(v)
    }
    fn add(&mut self) -> Option<u128> {
        let mut v = self.mul()?;
        loop {
            if self.eat("+") {
                v = v.checked_add(self.mul()?)?;
            } else if self.eat("-") {
                v = v.checked_sub(self.mul()?)?;
            } else {
                return Some(v);
            }
        }
    }
    fn mul(&mut self) -> Option<u128> {
        let mut v = self.atom()?;
        while self.eat("*") {
            v = v.checked_mul(self.atom()?)?;
        }
        Some(v)
    }
    fn atom(&mut self) -> Option<u128> {
        self.ws();
        let v = if self.eat("(") {
            let v = self.shift()?;
            self.eat(")").then_some(v)?
        } else {
            let start = self.i;
            while self.s.get(self.i).is_some_and(|c| c.is_alphanumeric() || *c == '_') {
                self.i += 1;
            }
            let word: String = self.s[start..self.i].iter().collect();
            if word.starts_with(|c: char| c.is_ascii_digit()) {
                let digits: String = word.chars().take_while(|c| c.is_ascii_digit() || *c == '_').filter(|c| *c != '_').collect();
                digits.parse().ok()?
            } else {
                *self.known.get(&word)?
            }
        };
        self.ws();
        if self.s[self.i..].starts_with(&['a', 's', ' ']) {
            self.i += 3;
            self.ws();
            while self.s.get(self.i).is_some_and(|c| c.is_alphanumeric() || *c == '_') {
                self.i += 1;
            }
        }
        Some(v)
    }
}

fn eval_const(expr: &str, known: &HashMap<String, u128>) -> Option<u128> {
    let mut e = Eval { s: expr.chars().collect(), i: 0, known };
    let v = e.shift()?;
    e.ws();
    (e.i == e.s.len()).then_some(v)
}

/// Every `const MAX_*` outside test code, with its value when it can be worked out.
fn caps(infos: &HashMap<String, FileInfo>, files: &[&String]) -> Vec<Cap> {
    let mut res: Vec<Cap> = Vec::new();
    let mut known: HashMap<String, u128> = HashMap::new();
    for f in files {
        let info = &infos[*f];
        for (i, l) in info.lines.iter().enumerate() {
            let c = code(l);
            let Some(p) = c.find("const MAX_") else { continue };
            if info.in_test(i + 1) || !c.trim_start().starts_with(['p', 'c']) {
                continue;
            }
            let rest = &c[p + 6..];
            let name = ident_at(rest).to_string();
            let value = rest.split_once('=').and_then(|(_, e)| e.split(';').next()).and_then(|e| eval_const(e, &known));
            if let Some(v) = value {
                known.insert(name.clone(), v);
            }
            res.push(Cap { name, file: (*f).clone(), line: i + 1, value });
        }
    }
    res
}

/// Name words of `MAX_PLANT_SEGMENTS` without the `MAX` and plural `S`: `[PLANT, SEGMENT]`.
fn words(name: &str) -> Vec<String> {
    name.split('_').skip(1).map(|w| if w.len() > 3 { w.trim_end_matches('S').to_string() } else { w.to_string() }).collect()
}

/// Is there a `const _` assertion mentioning every one of `names` anywhere in the crate files?
fn const_asserted(infos: &HashMap<String, FileInfo>, files: &[&String], names: &[&str]) -> bool {
    files.iter().any(|f| {
        let text = no_comments(&infos[*f].lines.join("\n"));
        text.lines().any(|l| l.contains("const _") && names.iter().all(|n| l.contains(n)))
    })
}

/// Capacity limits that must nest but are never related (VT: 256 plants x 2^14 segments per plant is 2^22,
/// the ledger holds 2^20). Two cheap forms, both low confidence:
/// a per-owner limit `MAX_<OWNER>_<THING>` times the owner limit `MAX_<OWNER>S` that exceeds the global
/// `MAX_<THING>`; and two `MAX_*` from different files multiplied or compared in one runtime expression.
/// Either way the fix is a `const _: () = assert!(..)` next to the limits.
fn capacity_nesting(infos: &HashMap<String, FileInfo>, files: &[&String], out: &mut Vec<Finding>) {
    let caps = caps(infos, files);
    let by_name: HashMap<&str, &Cap> = caps.iter().map(|c| (c.name.as_str(), c)).collect();
    for per in &caps {
        let pw = words(&per.name);
        for glob in caps.iter().filter(|g| g.name != per.name) {
            let gw = words(&glob.name);
            if gw.is_empty() || pw.len() <= gw.len() || !pw.ends_with(&gw) {
                continue;
            }
            let owner = caps.iter().find(|o| words(&o.name) == pw[..pw.len() - gw.len()]);
            let (Some(owner), Some(pv), Some(gv)) = (owner, per.value, glob.value) else { continue };
            let Some(ov) = owner.value else { continue };
            if ov.checked_mul(pv).is_some_and(|total| total > gv) && !const_asserted(infos, files, &[&owner.name, &per.name, &glob.name]) {
                out.push(Finding {
                    file: per.file.clone(),
                    line: per.line,
                    severity: "low",
                    rule: "capacity_nesting".into(),
                    message: format!(
                        "`{}` x `{}` can exceed `{}` ({}): low confidence, add `const _: () = assert!({} * {} <= {});` so the limits cannot drift apart",
                        owner.name, per.name, glob.name, glob.file, owner.name, per.name, glob.name
                    ),
                });
            }
        }
    }
    for f in files {
        let info = &infos[*f];
        for (i, l) in info.lines.iter().enumerate() {
            let c = code(l);
            if info.in_test(i + 1) || c.contains("const ") || !(c.contains('*') || c.contains(['<', '>']) || c.contains("==")) {
                continue;
            }
            let mut used: Vec<&str> = c
                .split(|ch: char| !ch.is_alphanumeric() && ch != '_')
                .filter(|w| by_name.contains_key(w))
                .collect();
            used.sort();
            used.dedup();
            for (a, b) in used.iter().enumerate().flat_map(|(k, a)| used[k + 1..].iter().map(move |b| (a, b))) {
                if by_name[a].file != by_name[b].file && !const_asserted(infos, files, &[a, b]) {
                    out.push(finding(f, i + 1, "low", "capacity_nesting", info, &format!(
                        "`{a}` ({}) and `{b}` ({}) are combined at runtime; low confidence: add `const _: () = assert!(..)` stating how the limits nest",
                        by_name[a].file, by_name[b].file
                    )));
                }
            }
        }
    }
}

/// All state rules over the parsed files (`files` is sorted).
pub fn check(infos: &HashMap<String, FileInfo>, files: &[&String], out: &mut Vec<Finding>) {
    for f in files {
        debug_only_invariant(f, &infos[*f], out);
    }
    unbounded_growth(infos, files, out);
    capacity_nesting(infos, files, out);
}

#[cfg(test)]
mod tests {
    use super::*;

    fn run(files: &[(&str, &str)]) -> Vec<(String, String, usize, String)> {
        let infos: HashMap<String, FileInfo> = files.iter().map(|(f, s)| (f.to_string(), FileInfo::new(f, s))).collect();
        let mut names: Vec<&String> = infos.keys().collect();
        names.sort();
        let mut out = Vec::new();
        check(&infos, &names, &mut out);
        out.into_iter().map(|f| (f.rule, f.file, f.line, f.message)).collect()
    }
    fn rules_of(r: &[(String, String, usize, String)], rule: &str) -> Vec<usize> {
        r.iter().filter(|x| x.0 == rule).map(|x| x.2).collect()
    }

    // VT ledger end_step before the fix: the conservation check only ran in debug builds.
    const VT_END_STEP: &str = "impl Ledger {\n    pub fn end_step(&mut self) -> Result<Vec<u8>, E> {\n        if cfg!(debug_assertions) {\n            self.check()?;\n        }\n        Ok(vec![])\n    }\n}\n";

    #[test]
    fn debug_only_check_is_flagged() {
        let r = run(&[("src/ledger.rs", VT_END_STEP)]);
        assert_eq!(rules_of(&r, "debug_only_invariant"), vec![3]);
        let attr = "fn a(w: &W) {\n    #[cfg(debug_assertions)]\n    w.verify_totals();\n}\nfn b(w: &W) {\n    debug_assert!(w.check().is_ok());\n}\n";
        assert_eq!(rules_of(&run(&[("src/a.rs", attr)]), "debug_only_invariant"), vec![2, 6]);
    }

    #[test]
    fn debug_only_near_misses_stay_quiet() {
        let src = "fn a(x: u32) {\n    debug_assert!(x > 0);\n    if cfg!(debug_assertions) {\n        println!(\"extra logging\");\n    }\n    let y = x.checked_add(1);\n    #[cfg(not(debug_assertions))]\n    w.check();\n    if cfg!(debug_assertions) { }\n    self.check();\n}\n#[cfg(debug_assertions)]\nfn check_all() {}\n#[cfg(test)]\nmod t {\n    fn h() {\n        if cfg!(debug_assertions) { x.check(); }\n    }\n}\n";
        assert!(rules_of(&run(&[("src/a.rs", src)]), "debug_only_invariant").is_empty());
    }

    #[test]
    fn debug_only_identity_survives_line_moves() {
        let a = run(&[("src/ledger.rs", VT_END_STEP)]);
        let moved = format!("// pad\n// pad\n{VT_END_STEP}");
        let b = run(&[("src/ledger.rs", moved.as_str())]);
        assert_eq!(a[0].3, b[0].3);
        assert_eq!(b[0].2, a[0].2 + 2);
    }

    // VT plant state before the fix: the map got an entry per built segment and shrank only when a segment died.
    const GROW: &str = "pub struct Plant {\n    pub name: String,\n    since_branch: BTreeMap<u32, u32>,\n    cache: Vec<u32>,\n    log: Vec<u32>,\n}\nimpl Plant {\n    pub fn new() -> Plant {\n        let mut p = Plant::default();\n        p.log.push(1);\n        p\n    }\n    pub fn grow(&mut self, s: u32) {\n        self.since_branch.insert(s, 1);\n        self.cache.push(s);\n    }\n    pub fn reset(&mut self) {\n        self.cache.clear();\n    }\n}\n";

    #[test]
    fn unbounded_growth_planted_and_near_misses() {
        let r = run(&[("src/plant.rs", GROW)]);
        // since_branch grows and never shrinks; cache is cleared; log only grows in the constructor.
        assert_eq!(rules_of(&r, "unbounded_growth"), vec![3]);
        assert!(r.iter().any(|x| x.3.contains("Plant::since_branch")));
        // A remove in another file of the same crate bounds it.
        let other = "fn die(p: &mut Plant) {\n    p.since_branch.remove(&1);\n}\n";
        assert!(rules_of(&run(&[("src/plant.rs", GROW), ("src/other.rs", other)]), "unbounded_growth").is_empty());
        // A removal in a different crate does not.
        let r2 = run(&[("crates/a/src/plant.rs", GROW), ("crates/b/src/other.rs", other)]);
        assert_eq!(rules_of(&r2, "unbounded_growth"), vec![3]);
        // A Vec filled by a free function (building a report) is not long-lived state; one a `&mut self` method
        // pushes to is.
        let report = "pub struct Report {\n    rows: Vec<u32>,\n}\nfn build() -> Report {\n    let mut r = Report { rows: vec![] };\n    r.rows.push(1);\n    r\n}\n";
        assert!(rules_of(&run(&[("src/r.rs", report)]), "unbounded_growth").is_empty());
        let state = "pub struct Log {\n    rows: Vec<u32>,\n}\nimpl Log {\n    pub fn record(&mut self, x: u32) {\n        self.rows.push(x);\n    }\n}\n";
        assert_eq!(rules_of(&run(&[("src/r.rs", state)]), "unbounded_growth"), vec![2]);
        // A length cap counts as a bound.
        let capped = GROW.replace("self.since_branch.insert(s, 1);", "if self.since_branch.len() >= MAX_BRANCH { return; }\n        self.since_branch.insert(s, 1);");
        assert!(rules_of(&run(&[("src/plant.rs", capped.as_str())]), "unbounded_growth").is_empty());
    }

    #[test]
    fn unbounded_growth_identity_survives_line_moves() {
        let moved = format!("// pad\n{GROW}");
        let (a, b) = (run(&[("src/plant.rs", GROW)]), run(&[("src/plant.rs", moved.as_str())]));
        assert_eq!(a[0].3, b[0].3);
        assert_eq!(b[0].2, a[0].2 + 1);
    }

    #[test]
    fn const_evaluator() {
        let known: HashMap<String, u128> = [("A".to_string(), 4u128)].into_iter().collect();
        assert_eq!(eval_const("1 << 20", &known), Some(1 << 20));
        assert_eq!(eval_const("A * 4_096usize + 1", &known), Some(16385));
        assert_eq!(eval_const("(1 << 4) as usize", &known), Some(16));
        assert_eq!(eval_const("B", &known), None);
    }

    const PLANT_MOD: &str = "pub const MAX_PLANTS: usize = 256;\npub const MAX_PLANT_SEGMENTS: usize = 1 << 14;\n";
    const SEG_MOD: &str = "pub const MAX_SEGMENTS: usize = 1 << 20;\n";

    #[test]
    fn capacity_nesting_planted_and_near_misses() {
        // 256 x 2^14 = 2^22 > 2^20 (the VT shape).
        let r = run(&[("src/plant.rs", PLANT_MOD), ("src/segments.rs", SEG_MOD)]);
        assert_eq!(rules_of(&r, "capacity_nesting"), vec![2]);
        // Fits: 64 x 2^14 = 2^20.
        let fits = PLANT_MOD.replace("256", "64");
        assert!(rules_of(&run(&[("src/plant.rs", fits.as_str()), ("src/segments.rs", SEG_MOD)]), "capacity_nesting").is_empty());
        // A const assert naming all three silences it.
        let asserted = format!("{SEG_MOD}const _: () = assert!(MAX_PLANTS * MAX_PLANT_SEGMENTS <= MAX_SEGMENTS);\n");
        assert!(rules_of(&run(&[("src/plant.rs", PLANT_MOD), ("src/segments.rs", asserted.as_str())]), "capacity_nesting").is_empty());
    }

    #[test]
    fn capacity_nesting_runtime_combination() {
        let user = "fn f(n: usize) -> bool {\n    n * MAX_DEMANDS > MAX_SEGMENTS\n}\nfn g(n: usize) -> bool {\n    n < MAX_SEGMENTS\n}\n";
        let r = run(&[("src/share.rs", "pub const MAX_DEMANDS: usize = 4096;\n"), ("src/segments.rs", SEG_MOD), ("src/use.rs", user)]);
        assert_eq!(rules_of(&r, "capacity_nesting"), vec![2]);
        // Same module: no finding.
        let same = format!("{SEG_MOD}pub const MAX_DEMANDS: usize = 4096;\n");
        assert!(rules_of(&run(&[("src/segments.rs", same.as_str()), ("src/use.rs", user)]), "capacity_nesting").is_empty());
    }
}
