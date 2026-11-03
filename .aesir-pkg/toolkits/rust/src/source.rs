//! One parsed source file: its lines, its functions with line ranges, and which lines are test code.
//!
//! Findings use this to name the enclosing function (stable identity when lines move) and to skip tests.

use quote::ToTokens;
use syn::visit::{self, Visit};
use syn::{Attribute, ImplItemFn, ItemFn, ItemMod, TraitItemFn};

/// A function and its line range (1-based, inclusive).
pub struct FnInfo {
    pub name: String,
    pub start: usize,
    pub end: usize,
    /// `#[test]`, or inside a `cfg(test)` module.
    pub test: bool,
    /// Carries a test attribute itself (`#[test]`, `#[tokio::test]`), unlike helpers in a `cfg(test)` module.
    pub test_attr: bool,
    /// Carries `#[should_panic]`: the panic is its assertion.
    pub should_panic: bool,
    /// Returns a `Result`, so `?` in the body fails the test.
    pub returns_result: bool,
    /// Takes `&mut self`: a method that changes the value it belongs to.
    pub mut_self: bool,
}

/// A named struct field whose type is a growable collection (`HashMap`, `BTreeMap`, `Vec`).
pub struct FieldInfo {
    pub strukt: String,
    pub name: String,
    /// The collection type's name, e.g. `BTreeMap`.
    pub kind: String,
    pub line: usize,
}

/// Lines and structure of one file. Unparsable files keep their lines but have no functions.
pub struct FileInfo {
    pub lines: Vec<String>,
    pub fns: Vec<FnInfo>,
    pub fields: Vec<FieldInfo>,
    test_ranges: Vec<(usize, usize)>,
    all_test: bool,
}

/// Folders whose files are test or example code as a whole.
pub fn test_path(file: &str) -> bool {
    file.split('/').any(|c| c == "tests" || c == "benches" || c == "examples") || file.ends_with("/tests.rs")
}

fn is_test_attr(a: &Attribute) -> bool {
    a.path().segments.last().is_some_and(|s| s.ident == "test")
}

fn is_cfg_test(a: &Attribute) -> bool {
    a.path().is_ident("cfg")
        && a.meta.to_token_stream().to_string().split(|c: char| !c.is_alphanumeric() && c != '_').any(|w| w == "test")
}

struct Collector {
    fns: Vec<FnInfo>,
    fields: Vec<FieldInfo>,
    test_ranges: Vec<(usize, usize)>,
    depth_test: usize,
}

impl Collector {
    fn add(&mut self, attrs: &[Attribute], sig: &syn::Signature, block: &syn::Block) {
        let start = sig.fn_token.span.start().line;
        let end = block.brace_token.span.close().end().line;
        let test_attr = attrs.iter().any(is_test_attr);
        let test = self.depth_test > 0 || test_attr;
        let should_panic = attrs.iter().any(|a| a.path().is_ident("should_panic"));
        let returns_result = matches!(&sig.output, syn::ReturnType::Type(_, t) if t.to_token_stream().to_string().contains("Result"));
        let mut_self = sig.receiver().is_some_and(|r| r.reference.is_some() && r.mutability.is_some());
        self.fns.push(FnInfo { name: sig.ident.to_string(), start, end, test, test_attr, should_panic, returns_result, mut_self });
    }
}

impl<'a> Visit<'a> for Collector {
    fn visit_item_fn(&mut self, i: &'a ItemFn) {
        self.add(&i.attrs, &i.sig, &i.block);
        visit::visit_item_fn(self, i);
    }
    fn visit_impl_item_fn(&mut self, i: &'a ImplItemFn) {
        self.add(&i.attrs, &i.sig, &i.block);
        visit::visit_impl_item_fn(self, i);
    }
    fn visit_trait_item_fn(&mut self, i: &'a TraitItemFn) {
        if let Some(b) = &i.default {
            self.add(&i.attrs, &i.sig, b);
        }
        visit::visit_trait_item_fn(self, i);
    }
    fn visit_item_struct(&mut self, i: &'a syn::ItemStruct) {
        if let syn::Fields::Named(named) = &i.fields {
            for f in &named.named {
                let (Some(name), syn::Type::Path(tp)) = (&f.ident, &f.ty) else { continue };
                let Some(last) = tp.path.segments.last() else { continue };
                let kind = last.ident.to_string();
                if ["HashMap", "BTreeMap", "Vec"].contains(&kind.as_str()) {
                    self.fields.push(FieldInfo { strukt: i.ident.to_string(), name: name.to_string(), kind, line: name.span().start().line });
                }
            }
        }
        visit::visit_item_struct(self, i);
    }
    fn visit_item_mod(&mut self, i: &'a ItemMod) {
        let test = i.attrs.iter().any(is_cfg_test);
        if test {
            if let Some((brace, _)) = &i.content {
                self.test_ranges.push((i.mod_token.span.start().line, brace.span.close().end().line));
            }
            self.depth_test += 1;
        }
        visit::visit_item_mod(self, i);
        if test {
            self.depth_test -= 1;
        }
    }
}

impl FileInfo {
    /// Parse `src`; `file` decides whether the whole file counts as test code.
    pub fn new(file: &str, src: &str) -> FileInfo {
        let lines = src.lines().map(String::from).collect();
        let mut c = Collector { fns: vec![], fields: vec![], test_ranges: vec![], depth_test: 0 };
        if let Ok(ast) = syn::parse_file(src) {
            c.visit_file(&ast);
        }
        FileInfo { lines, fns: c.fns, fields: c.fields, test_ranges: c.test_ranges, all_test: test_path(file) }
    }

    /// Innermost function containing `line`.
    pub fn enclosing(&self, line: usize) -> Option<&FnInfo> {
        self.fns.iter().filter(|f| f.start <= line && line <= f.end).min_by_key(|f| f.end - f.start)
    }

    /// Is `line` test code (test file, `cfg(test)` module, or `#[test]` function)?
    pub fn in_test(&self, line: usize) -> bool {
        self.all_test
            || self.test_ranges.iter().any(|&(a, b)| a <= line && line <= b)
            || self.enclosing(line).is_some_and(|f| f.test)
    }

    /// Source text of lines `a..=b` (1-based, clamped), joined.
    pub fn text(&self, a: usize, b: usize) -> String {
        let (a, b) = (a.max(1), b.min(self.lines.len()));
        if a > b {
            return String::new();
        }
        self.lines[a - 1..b].join("\n")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const SRC: &str =
        "fn a() {\n    let x = 1;\n}\n\n#[cfg(test)]\nmod t {\n    fn helper() {}\n    #[test]\n    fn b() {\n        a();\n    }\n}\n";

    #[test]
    fn functions_ranges_and_tests() {
        let f = FileInfo::new("src/x.rs", SRC);
        assert_eq!(f.enclosing(2).map(|f| f.name.as_str()), Some("a"));
        assert!(!f.in_test(2));
        assert!(f.in_test(7) && f.in_test(10));
        assert_eq!(f.enclosing(10).map(|f| (f.start, f.end)), Some((9, 11)));
        assert!(FileInfo::new("crates/x/tests/it.rs", "fn z() {}").in_test(1));
    }
}
