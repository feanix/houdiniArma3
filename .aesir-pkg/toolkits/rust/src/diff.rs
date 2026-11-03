//! Comment-only diff check: which changed files differ from the base branch only in comments, docs or layout.
//!
//! Conservative: a file counts as code unless both versions parse and their syntax trees are identical once
//! `#[doc = "..."]` attributes are dropped. Anything unparsable, added, deleted or odd is treated as code.

use crate::finding::Finding;
use quote::ToTokens;
use std::path::Path;
use std::process::Command;
use syn::visit_mut::{self, VisitMut};
use syn::{Attribute, Expr, ExprLit, Lit, LitStr, Meta, MetaNameValue};

/// Blank the text of every `#[doc = ".."]` so it can be cut from the printed tree.
struct BlankDocs;

impl VisitMut for BlankDocs {
    fn visit_attribute_mut(&mut self, a: &mut Attribute) {
        if let Meta::NameValue(nv) = &a.meta {
            if nv.path.is_ident("doc") {
                let value = Expr::Lit(ExprLit { attrs: vec![], lit: Lit::Str(LitStr::new("", proc_macro2::Span::call_site())) });
                a.meta = Meta::NameValue(MetaNameValue { path: nv.path.clone(), eq_token: nv.eq_token, value });
            }
        }
        visit_mut::visit_attribute_mut(self, a);
    }
}

/// Canonical text of the code in `src`, or None when it does not parse. Comments never reach the tree; doc
/// attributes (which `///` becomes) are blanked, then removed. Other attributes, `cfg_attr(doc ..)` included, stay.
pub fn normalize(src: &str) -> Option<String> {
    let mut ast = syn::parse_file(src).ok()?;
    BlankDocs.visit_file_mut(&mut ast);
    let text = ast.to_token_stream().to_string();
    let text = text.replace("# ! [doc = \"\"]", "").replace("# [doc = \"\"]", "");
    Some(text.split_whitespace().collect::<Vec<_>>().join(" ")) // removal leaves gaps
}

fn git(root: &Path, args: &[&str]) -> Option<Vec<u8>> {
    let o = Command::new("git").args(args).current_dir(root).output().ok()?;
    o.status.success().then_some(o.stdout)
}

/// Findings for modified `.rs` files (branch plus working tree vs the merge base with `base`) with no code change.
/// No such base (or no repository) means nothing to compare: no findings.
pub fn comment_only(root: &Path, base: &str) -> Vec<Finding> {
    let Some(mb) = git(root, &["merge-base", "HEAD", base]) else { return vec![] };
    let mb = String::from_utf8_lossy(&mb).trim().to_string();
    let Some(names) = git(root, &["diff", "--name-status", "--no-renames", "-z", &mb]) else { return vec![] };
    let names = String::from_utf8_lossy(&names).to_string();
    let mut parts = names.split('\0').filter(|s| !s.is_empty());
    let mut res = Vec::new();
    while let (Some(status), Some(path)) = (parts.next(), parts.next()) {
        if status != "M" || !path.ends_with(".rs") {
            continue;
        }
        let Some(old) = git(root, &["show", &format!("{mb}:{path}")]) else { continue };
        let old = String::from_utf8_lossy(&old).to_string();
        let Ok(new) = std::fs::read_to_string(root.join(path)) else { continue };
        let (Some(a), Some(b)) = (normalize(&old), normalize(&new)) else { continue };
        if a == b {
            res.push(Finding {
                file: path.to_string(),
                line: 0,
                severity: "info",
                rule: "diff::comment_only".into(),
                message: format!("no code change vs {base}: syntax tree identical apart from comments, docs and layout"),
            });
        }
    }
    res
}

#[cfg(test)]
mod tests {
    use super::normalize;

    #[test]
    fn docs_comments_and_layout_do_not_count() {
        let a = "//! A\n\n/// Doubles.\npub fn double(x: u32) -> u32 {\n    x\n}\n";
        let b = "//! B\n\n// note\n/// Doubles a number.\n/// More.\npub fn   double(x: u32) -> u32 {\n    x /* why */\n}\n";
        assert_eq!(normalize(a), normalize(b));
    }

    #[test]
    fn code_and_unparsable_count_as_code() {
        let a = "pub fn f() -> u8 { 1 }";
        assert_ne!(normalize(a), normalize("pub fn f() -> u8 { 2 }"));
        assert_ne!(normalize(a), normalize("#[inline]\npub fn f() -> u8 { 1 }"));
        assert_eq!(normalize("fn broken( {"), None);
    }
}
