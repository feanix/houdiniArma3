//! Doc coverage: pub items without a doc comment, one finding per file, ranked by how many are missing.

use crate::finding::Finding;
use std::path::Path;
use syn::{Attribute, ImplItem, Item, Meta, Visibility};

/// Folders whose code is not public API.
fn skipped(file: &str) -> bool {
    file.split('/').any(|c| c == "tests" || c == "benches" || c == "examples" || c == "target")
}

fn documented(attrs: &[Attribute]) -> bool {
    attrs.iter().any(|a| match &a.meta {
        Meta::NameValue(nv) => nv.path.is_ident("doc"),
        Meta::List(l) => l.path.is_ident("doc"), // doc(hidden) and friends: deliberate, not a gap
        _ => false,
    })
}

/// Pub items counted so far, and (line, "kind name") of the undocumented ones.
#[derive(Default)]
struct Acc {
    total: usize,
    missing: Vec<(usize, String)>,
}

impl Acc {
    fn note(&mut self, attrs: &[Attribute], vis: &Visibility, kind: &str, id: &proc_macro2::Ident) {
        if matches!(vis, Visibility::Public(_)) {
            self.total += 1;
            if !documented(attrs) {
                self.missing.push((id.span().start().line, format!("{kind} {id}")));
            }
        }
    }
}

fn scan(items: &[Item], acc: &mut Acc) {
    for it in items {
        match it {
            Item::Fn(i) => acc.note(&i.attrs, &i.vis, "fn", &i.sig.ident),
            Item::Struct(i) => acc.note(&i.attrs, &i.vis, "struct", &i.ident),
            Item::Enum(i) => acc.note(&i.attrs, &i.vis, "enum", &i.ident),
            Item::Union(i) => acc.note(&i.attrs, &i.vis, "union", &i.ident),
            Item::Trait(i) => acc.note(&i.attrs, &i.vis, "trait", &i.ident),
            Item::Type(i) => acc.note(&i.attrs, &i.vis, "type", &i.ident),
            Item::Const(i) => acc.note(&i.attrs, &i.vis, "const", &i.ident),
            Item::Static(i) => acc.note(&i.attrs, &i.vis, "static", &i.ident),
            Item::Mod(i) => {
                // `pub mod x;` is documented by `//!` in its own file
                if let Some((_, inner)) = &i.content {
                    acc.note(&i.attrs, &i.vis, "mod", &i.ident);
                    scan(inner, acc);
                }
            }
            Item::Impl(i) if i.trait_.is_none() => {
                for m in &i.items {
                    match m {
                        ImplItem::Fn(f) => acc.note(&f.attrs, &f.vis, "fn", &f.sig.ident),
                        ImplItem::Const(c) => acc.note(&c.attrs, &c.vis, "const", &c.ident),
                        ImplItem::Type(t) => acc.note(&t.attrs, &t.vis, "type", &t.ident),
                        _ => {}
                    }
                }
            }
            _ => {}
        }
    }
}

/// One finding per file with undocumented pub items, most missing first.
pub fn coverage(root: &Path, files: &[String]) -> Vec<Finding> {
    let mut res: Vec<(usize, Finding)> = Vec::new();
    for file in files.iter().filter(|f| !skipped(f)) {
        let Ok(src) = std::fs::read_to_string(root.join(file)) else { continue };
        let Ok(ast) = syn::parse_file(&src) else { continue }; // unparsable: the clippy run reports it
        let mut acc = Acc::default();
        scan(&ast.items, &mut acc);
        let (total, missing) = (acc.total, acc.missing);
        let n = missing.len();
        if n == 0 {
            continue;
        }
        let severity = if n >= 20 || (n >= 5 && n * 2 > total) {
            "medium"
        } else if n >= 5 {
            "low"
        } else {
            "info"
        };
        let first: Vec<String> = missing.iter().take(3).map(|(l, w)| format!("{w} (line {l})")).collect();
        res.push((
            n,
            Finding {
                file: file.clone(),
                line: 0,
                severity,
                rule: "doc_coverage".into(),
                message: format!("{n} of {total} pub items have no doc comment, e.g. {}", first.join(", ")),
            },
        ));
    }
    res.sort_by(|a, b| b.0.cmp(&a.0).then(a.1.file.cmp(&b.1.file)));
    res.into_iter().map(|(_, f)| f).collect()
}
