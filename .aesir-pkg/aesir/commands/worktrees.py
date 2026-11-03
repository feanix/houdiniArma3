"""worktrees list/prune/clean-cache: kept/prunable worktrees with disk sizes, removal of clean pushed or merged ones,
and deletion of build-cache dirs only (#213)."""
from .. import db, worktrees
from ._common import out, line, add, grp


def _where(r):
    """Branch name, or the short head for a detached worktree."""
    return r["branch"] or f"(detached {r['head']})"


def _sz(r):
    """Human size of a scan row, or '-' when sizes were not computed (they are a slow disk walk, so opt-in)."""
    return worktrees.human(r["size"]) if "size" in r else "-"


def _total(rows):
    """Print the total-size line; only meaningful when the rows carry sizes."""
    print(f"total  {worktrees.human(sum(r.get('size', 0) for r in rows))} in {len(rows)} worktree(s)")


def _has_sizes(rows):
    return any("size" in r for r in rows)


def render_list(rs):
    """Text for `worktrees list`: one row per worktree (size column only with --sizes), then the total."""
    for r in rs:
        line("prunable" if r["action"] == "remove" else "keep", _sz(r), r["path"], _where(r), r["why"])
    if not rs:
        print("no worktrees")
    elif _has_sizes(rs):
        _total(rs)


def wt_list(c, a):
    out(a, worktrees.scan(c, db.project_root(), sizes=a.sizes), render_list)


def _render(res):
    for r in res["worktrees"]:
        line("remove" if r["action"] == "remove" else "keep", _sz(r), r["path"], _where(r), r["why"])
    for b in res["branches"]:
        line("branch", b)
    for d in res["dirs"]:
        line("dir", d)
    for p in res["problems"]:
        line("problem", p)
    if _has_sizes(res["worktrees"]):
        _total(res["worktrees"])
    if not any(res.values()):
        print("nothing to prune")


def wt_prune(c, a):
    out(a, worktrees.prune(c, db.project_root(), a.dry_run), _render)


def render_clean(rows):
    """Text for `worktrees clean-cache`: one row per worktree (cleaned, spared or skipped with why), then freed."""
    for r in rows:
        if r["skipped"]:
            line("skipped", "-", r["path"], r["skipped"])
        else:
            line("spared" if r["spared"] else "cleaned", worktrees.human(r["freed"]), r["path"], ", ".join(r["dirs"]))
    print(f"freed  {worktrees.human(sum(r['freed'] for r in rows))}" if rows else "no kept worktrees")


def wt_clean_cache(c, a):
    out(a, worktrees.clean_cache(c, db.project_root(), a.keep, a.dry_run), render_clean)


def register(sub):
    g = grp(sub, "worktrees")
    add(g, "list", wt_list, ("--sizes", {"action": "store_true", "help": "also walk each worktree for its disk size "
                                         "and print the total (slow with many large build dirs)"}))
    add(g, "prune", wt_prune, ("--dry-run", {"action": "store_true", "help": "show what would go, with sizes"}))
    add(g, "clean-cache", wt_clean_cache,
        ("--keep", {"type": int, "default": 0, "help": "spare the N most recently active worktrees (newest mtime "
                                                         "of the worktree's git HEAD and reflog)"}),
        ("--dry-run", {"action": "store_true"}))
