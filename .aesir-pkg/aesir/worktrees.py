"""Worktree hygiene: list and prune the harness/queue worktrees under <root>/.claude/worktrees, report their disk
size, delete only build-cache dirs in the ones kept (#213), and report free space for the low-disk status flag.
[worktrees] in aesir.toml: cache_dirs (dir names), min_free_gb."""
import os
import shutil
import stat
import subprocess
from pathlib import Path

from . import gates, proc
from .queue import MAIN, _git
from .service import AesirError

OWNED_BRANCH = ("worktree-wf_", "task/")
DEFAULTS = {"cache_dirs": ["target", "__pycache__"], "min_free_gb": 10}
GB = 1024 ** 3


def _norm(p):
    return os.path.normcase(os.path.realpath(str(p)))


def _inside(path, base):
    path, base = _norm(path), _norm(base)
    return path == base or path.startswith(base + os.sep)


def _parse(root):
    out, cur = [], None
    for ln in _git(root, "worktree", "list", "--porcelain").stdout.splitlines() + [""]:
        if not ln.strip():
            if cur:
                out.append(cur)
            cur = None
        elif ln.startswith("worktree "):
            cur = {"path": ln[9:].strip(), "head": "", "branch": "", "detached": False, "locked": False}
        elif cur is not None:
            k, _, v = ln.partition(" ")
            if k == "HEAD":
                cur["head"] = v
            elif k == "branch":
                cur["branch"] = v.removeprefix("refs/heads/")
            elif k == "detached":
                cur["detached"] = True
            elif k == "locked":
                cur["locked"] = True
    return out


def merged(root, ref):
    """True when ref resolves and its tip is an ancestor of main (also used by status done_task, #246)."""
    return bool(ref) and _git(root, "merge-base", "--is-ancestor", ref, MAIN, check=False).returncode == 0


_merged = merged


def load_config(root):
    """[worktrees] in aesir.toml over DEFAULTS; a missing aesir.toml gives the defaults."""
    try:
        sec = gates.load_config(root).get("worktrees", {})
    except AesirError as e:
        if "no aesir.toml" in str(e):
            return dict(DEFAULTS)
        raise
    cfg = {**DEFAULTS, **sec}
    cd = cfg["cache_dirs"]
    if not isinstance(cd, list) or not all(isinstance(x, str) and x and "/" not in x and "\\" not in x for x in cd):
        raise AesirError("bad aesir.toml: [worktrees] cache_dirs must be a list of directory names")
    v = cfg["min_free_gb"]
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
        raise AesirError("bad aesir.toml: [worktrees] min_free_gb must be a number >= 0")
    return cfg


def is_link(path):
    """True for a symlink or a Windows junction (or any reparse point) at path, itself not followed. os.path.islink is
    False for junctions, so both are checked, plus the reparse-point attribute (#213 r1)."""
    try:
        if os.path.islink(path) or os.path.isjunction(path):
            return True
        return bool(getattr(os.lstat(path), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except OSError:
        return False


def dir_size(path):
    """Bytes under path; symlinks and junctions are never followed or counted, so a link cannot count another tree
    (or make the walk leave the worktree)."""
    total = 0
    stack = [str(path)]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    try:
                        if is_link(e.path):
                            continue
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        else:
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return total


def human(n):
    """Bytes as a short string: 512B, 5KB, 3MB, 1.5GB."""
    for u in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f}{u}" if u != "B" else f"{n}B"
        n /= 1024
    return f"{n:.1f}GB"


def free_bytes(root):
    """Free bytes on the drive holding the worktrees (the root's drive while none exist yet)."""
    base = Path(root) / ".claude" / "worktrees"
    return shutil.disk_usage(base if base.is_dir() else root).free


FETCH_TIMEOUT = 120


def fetch(root):
    """Refresh remote-tracking branches so 'pushed' is judged on current data. Never prompts (credential prompts would
    hang a headless run) and is time-limited. Returns '' when fine or when there is no remote, else why it failed."""
    if not _git(root, "remote", check=False).stdout.split():
        return ""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
    try:
        r = proc.run(["git", "fetch", "--quiet", "--prune"], cwd=str(root), capture_output=True, text=True,
                     encoding="utf-8", errors="replace", env=env, timeout=FETCH_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError) as e:
        return f"git fetch did not finish ({type(e).__name__})"
    return "" if r.returncode == 0 else (r.stderr or r.stdout).strip() or f"git fetch failed (rc {r.returncode})"


def pushed_in(root, ref):
    """Name of a remote-tracking branch containing ref ('' if none); origin/HEAD aliases are skipped."""
    if not ref:
        return ""
    r = _git(root, "for-each-ref", "--contains", ref, "--format=%(refname:short)", "refs/remotes", check=False)
    names = [n for n in r.stdout.split() if not n.endswith("/HEAD")]
    return sorted(names)[0] if r.returncode == 0 and names else ""


def _running(conn, path):
    for r in conn.execute("SELECT worktree FROM agents WHERE ended='' AND worktree!=''"):
        if _inside(r["worktree"], path) or _inside(path, r["worktree"]):
            return True
    return False


def scan(conn, root, sizes=False, pushed=True):
    """One row per worktree under .claude/worktrees: path, branch, action ('remove' or 'keep'), why (text) and code
    (stable reason: locked, missing, running, dirty, main, unmerged, detached, pushed, merged). pushed=False ignores
    remote branches (stale after a failed fetch). sizes=True adds 'size' bytes (a disk walk, slow on big caches)."""
    base = Path(root) / ".claude" / "worktrees"
    rows = []
    for w in _parse(root):
        p = Path(w["path"])
        if _norm(p) == _norm(root) or not _inside(p, base):
            continue
        row = {"path": str(p), "branch": w["branch"], "head": w["head"][:8], "action": "keep", "why": "",
               "code": ""}
        if w["locked"]:
            row["why"], row["code"] = "locked", "locked"
        elif not p.exists():
            row["why"], row["code"] = "directory missing (run git worktree prune)", "missing"
        elif _running(conn, p):
            row["why"], row["code"] = "running agent", "running"
        elif gates.dirty_files(p):
            row["why"], row["code"] = "dirty", "dirty"
        elif w["branch"] == MAIN:
            row["why"], row["code"] = "on main", "main"
        elif not _merged(root, w["branch"] or w["head"]):
            rb = pushed_in(root, w["head"]) if pushed else ""  # clean and fully on a remote: removing loses nothing
            if rb:
                row["action"], row["why"], row["code"] = "remove", f"pushed in {rb}", "pushed"
            else:
                row["why"] = "branch not merged" if w["branch"] else "detached HEAD not in main"
                row["code"] = "unmerged" if w["branch"] else "detached"
        else:
            row["action"], row["code"] = "remove", "merged"
        if sizes:
            row["size"] = dir_size(p) if p.exists() else 0
        rows.append(row)
    return rows


def prune(conn, root, dry_run=False):
    """Remove clean, unlocked worktrees with no running agent whose head is merged or on a remote branch (after a
    fetch), then their merged branches and empty leftover dirs. If the fetch fails, remote branches are stale and not
    trusted: pushed-only worktrees are kept and the failure is a problem line. Sizes (a slow disk walk) only on
    dry_run. Returns {"worktrees": rows, "branches": [...], "dirs": [...], "problems": [...]}."""
    root = Path(root)
    why = fetch(root)
    rows = scan(conn, root, sizes=dry_run, pushed=not why)
    res = {"worktrees": rows, "branches": [], "dirs": [],
           "problems": [f"fetch failed, pushed worktrees kept: {why}"] if why else []}
    for r in rows:
        if r["action"] != "remove":
            continue
        if not dry_run:
            g = _git(root, "worktree", "remove", r["path"], check=False)  # no --force: git re-checks clean/unlocked
            if g.returncode:
                r["action"], r["why"] = "keep", "remove failed"
                res["problems"].append(f"{r['path']}: {(g.stderr or g.stdout).strip()}")
                continue
        b = r["branch"]
        if b.startswith(OWNED_BRANCH) and _merged(root, b):
            if dry_run or _git(root, "branch", "-d", b, check=False).returncode == 0:
                res["branches"].append(b)
    base = root / ".claude" / "worktrees"
    live = {_norm(w["path"]) for w in _parse(root)}
    if base.is_dir():
        for d in sorted(base.iterdir()):
            if d.is_dir() and _norm(d) not in live and not any(d.iterdir()):
                if not dry_run:
                    try:
                        d.rmdir()
                    except OSError as e:
                        res["problems"].append(f"{d}: {e}")
                        continue
                res["dirs"].append(str(d))
    return res


def _gitdir(wt):
    """The git dir behind a worktree's .git file (None if unreadable)."""
    try:
        t = (Path(wt) / ".git").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return Path(t[8:].strip()) if t.startswith("gitdir:") else None


def recency(wt):
    """Last-activity proxy for --keep: newest mtime of the worktree's git HEAD and its reflog (they change on commit and
    checkout; unlike the index, `git status` does not touch them, and the directory mtime barely changes). Falls back
    to the directory mtime."""
    gd, best = _gitdir(wt), 0.0
    for f in ((gd / "HEAD", gd / "logs" / "HEAD") if gd else ()):
        try:
            best = max(best, f.stat().st_mtime)
        except OSError:
            pass
    return best or os.path.getmtime(wt)


def _cache_dirs(wt, names):
    """Cache dirs under worktree wt that are safe to delete: named in `names`, not a link/junction, git-ignored, with
    no tracked file inside, and really inside wt. Links are never followed. Fails closed: raises AesirError if any git
    check fails, so the caller skips the worktree."""
    ls = _git(wt, "ls-files", "-z", check=False)
    if ls.returncode:
        raise AesirError(f"git ls-files failed (rc {ls.returncode}): {(ls.stderr or '').strip()}")
    tracked = set()
    for f in ls.stdout.split("\0"):
        parts = f.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            tracked.add("/".join(parts[:i]))
    cand = []
    stack = [("", str(wt))]
    while stack:
        rel, dp = stack.pop()
        try:
            entries = list(os.scandir(dp))
        except OSError:
            continue
        for e in entries:
            if e.name == ".git" or not e.is_dir(follow_symlinks=False) or is_link(e.path):
                continue  # a junction or symlink is never entered, even if named like a cache dir
            rd = f"{rel}/{e.name}" if rel else e.name
            if e.name in names:
                if rd not in tracked and _inside(e.path, wt):
                    cand.append((rd, e.path))
            else:
                stack.append((rd, e.path))
    if not cand:
        return []
    ci = proc.run(["git", "check-ignore", "-z", "--stdin"], cwd=str(wt), capture_output=True, text=True,
                  encoding="utf-8", errors="replace", input="\0".join(rd + "/" for rd, _ in cand) + "\0")
    if ci.returncode not in (0, 1):
        raise AesirError(f"git check-ignore failed (rc {ci.returncode}): {(ci.stderr or '').strip()}")
    ignored = {x.rstrip("/") for x in ci.stdout.split("\0") if x}
    return [p for rd, p in cand if rd in ignored]


def _remove_tree(d):
    """Delete cache dir d without following links: links inside it are unlinked themselves (a junction to a shared
    cargo cache loses only the junction), then the rest is removed."""
    for dp, dns, fns in os.walk(d):
        for n in dns[:]:
            p = os.path.join(dp, n)
            if is_link(p):
                dns.remove(n)
                try:
                    os.rmdir(p)  # removes a junction or directory symlink itself, never its target
                except OSError:
                    os.unlink(p)
        for n in fns:
            p = os.path.join(dp, n)
            if is_link(p):
                os.unlink(p)
    shutil.rmtree(d, ignore_errors=True)


def clean_cache(conn, root, keep=0, dry_run=False):
    """Delete configured cache dirs (git-ignored, link-free, untracked: see _cache_dirs) in kept worktrees, never in
    ones prune would remove, nor locked, running or missing ones. Dirty worktrees ARE cleaned: only git-ignored dirs go,
    and those never hold uncommitted work. keep=N spares the N most recently active worktrees (see recency()).
    Returns [{path, spared, dirs, freed, skipped}]; skipped is why a worktree was left alone ('' if cleaned)."""
    root = Path(root)
    names = set(load_config(root)["cache_dirs"])
    rows = [r for r in scan(conn, root) if r["action"] == "keep" and r["code"] not in ("locked", "running", "missing")]
    rows.sort(key=lambda r: recency(r["path"]), reverse=True)
    res = []
    for i, r in enumerate(rows):
        row = {"path": r["path"], "spared": i < keep, "dirs": [], "freed": 0, "skipped": ""}
        if not row["spared"]:
            try:
                found = _cache_dirs(Path(r["path"]), names)
            except AesirError as e:
                row["skipped"] = str(e)
                found = []
            for d in found:
                row["freed"] += dir_size(d)
                if not dry_run:
                    _remove_tree(d)
                row["dirs"].append(d)
        res.append(row)
    return res
