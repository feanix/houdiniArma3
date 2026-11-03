"""Helpers shared by command modules: global flags, output, parser building."""
import argparse
import json
import re
import sys
from pathlib import Path

from ..invoke import cmd as client_cmd


def steer_line():
    """How to steer a running agent (#172); printed by spawn-context and lead resume."""
    return client_cmd('Steer a running agent: python -m aesir note <agent-id> "<text>" (ids: python -m aesir agent list).')


def common(default):
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--db", default=default, help="database path (default: <root>/.aesir/aesir.db or $AESIR_DB)")
    p.add_argument("--allow-migrate", action="store_true", default=default,
                   help="let this code migrate a db that belongs to another checkout (or AESIR_ALLOW_MIGRATE=1)")
    p.add_argument("--as", dest="actor", default=default,
                   help="calling role; honour-system attribution, not access control")
    p.add_argument("--json", action="store_true", default=default, help="JSON output (list/show/resume)")
    return p


_LEAF = common(argparse.SUPPRESS)


def text_arg(v):
    """'@path' reads the text from a file."""
    return Path(v[1:]).read_text(encoding="utf-8") if v and v.startswith("@") else v


def require_dir(path):
    """--dir checks (#162): a missing folder is a usage error (exit 2), not a traceback from deep inside git/subprocess.
    Windows paths with backslashes lose them in some shells, hence the hint. Returns the Path."""
    p = Path(path)
    if not p.is_dir():
        print(f"error: no such folder: {path}\nhint: quote Windows paths (\"C:\\dir\\wt\") or use forward slashes (C:/dir/wt)",
              file=sys.stderr)
        raise SystemExit(2)
    return p


def warn_branch(conn, tid, root):
    """Early, non-blocking warnings (to stderr, so --json stays clean) when task `tid` has no branch recorded or its
    branch touches paths outside the task scope. The same check blocks later, at in_review/merge (#83)."""
    from .. import service as S, tracks
    t = S.get_task(conn, tid)
    if not t["branch"]:
        print(client_cmd(f"warning: task {tid} has no branch; record it with: python -m aesir task set {tid} --branch <branch>"),
              file=sys.stderr)
        return
    try:
        bad = S.branch_out_of_scope(root, t["branch"], tracks.effective_scope(t, root), S.diff_base(conn, t, root))
    except S.AesirError:
        return  # branch not resolvable here (e.g. not created yet); nothing to compare
    if bad:
        print(f"warning: branch {t['branch']} touches paths outside task {tid} scope: {', '.join(bad[:10])}"
              + (f" (+{len(bad) - 10} more)" if len(bad) > 10 else ""), file=sys.stderr)


def proposal_id(v):
    """Core proposal id as resume prints it: '3', 'P3' or 'p3' (all mean 3)."""
    m = re.fullmatch(r"[Pp]?(\d+)", v.strip())
    if not m:
        raise argparse.ArgumentTypeError(f"invalid proposal id {v!r}: use a number or P<number> (e.g. 3, P3)")
    return int(m.group(1))


def add(parent, name, fn, *args):
    """Add subcommand `name` (accepting the global flags after it) calling fn(conn, args)."""
    p = parent.add_parser(name, parents=[_LEAF])
    for a, kw in args:
        p.add_argument(a, **kw)
    p.set_defaults(func=fn)
    return p


def grp(sub, name):
    """Subparsers of command group `name` (e.g. task); created once, shared by every module adding to it."""
    groups = sub.__dict__.setdefault("_aesir_groups", {})
    if name not in groups:
        groups[name] = sub.add_parser(name).add_subparsers(dest="sub", required=True)
    return groups[name]


def out(args, data, render):
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        render(data)


def line(*parts):
    print("  ".join(str(p) for p in parts))


# ---- observe: paging and next-command hints (text output only; --json keeps its shape) ----

PAGE_DEFAULT = 20


def paging(p):
    """Add --limit/--page to a list command's parser."""
    p.add_argument("--limit", type=int, help=f"rows per page (text default {PAGE_DEFAULT}; --json is unpaged unless given)")
    p.add_argument("--page", type=int, default=1, help="1-based page number")
    return p


def paged(a, rows, cmd):
    """Slice `rows` for this page. Returns (rows, footer or None). JSON stays the full list unless --limit/--page is given."""
    if a.page < 1 or (a.limit is not None and a.limit < 1):
        raise ValueError("--limit and --page must be >= 1")
    if a.json and a.limit is None and a.page == 1:
        return rows, None
    n = a.limit or PAGE_DEFAULT
    start = (a.page - 1) * n
    left = max(0, len(rows) - start - n)
    foot = f"{left} more: {client_cmd(cmd)} --page {a.page + 1}" + (f" --limit {n}" if a.limit else "") if left else None
    return rows[start:start + n], foot


def hint(a, *cmds):
    """Print up to 3 suggested next commands, '-> ' prefixed. Never in --json output."""
    if not a.json:
        for c in [c for c in cmds if c][:3]:
            print("-> " + client_cmd(c))


def out_list(a, rows, render, cmd, *hints):
    """out() for a list command: paging, 'N more' footer and hints in text mode."""
    page, foot = paged(a, rows, cmd)
    out(a, page, render)
    if foot and not a.json:
        print(foot)
    hint(a, *hints)
