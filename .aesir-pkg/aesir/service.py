"""Domain operations. Every write runs in one transaction and appends an event."""
import contextlib
import fnmatch
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone

from .db import event, has_fts, now, tx
from .rolenames import AGENT_TYPES, agent_role, normalise_role, reserved_prefix


class AesirError(Exception):
    pass


STATUSES = ["open", "claimed", "in_progress", "in_review", "changes_requested",
            "approved", "merged", "verified", "blocked", "escalated", "wontfix"]
TERMINAL = {"verified", "wontfix"}
TRANSITIONS = {
    "open": {"claimed"},
    "claimed": {"in_progress"},
    "in_progress": {"in_review", "blocked"},
    "blocked": {"in_progress"},
    "in_review": {"approved", "changes_requested"},
    "changes_requested": {"in_progress"},
    "approved": {"merged", "changes_requested"},  # queue failure sends approved work back
    "merged": {"verified"},
    "escalated": {"in_progress"},
}
# any non-terminal state may also go to escalated or wontfix
FIELDS = ["title", "owner", "requester", "scope", "spec", "branch", "gate_profile", "attempts"]
UNCLAIM_FROM = ("claimed", "in_progress")  # -> open with a reason (#177)
ACTIVE = ("open", "claimed", "in_progress", "blocked", "changes_requested", "escalated")


def parse_id(v, prefix):
    """Accept 11 or D11 (prefix, any case)."""
    s = str(v).strip()
    if s[:1].upper() == prefix:
        s = s[1:]
    try:
        return int(s)
    except ValueError:
        raise AesirError(f"bad id: {v} (use a number or {prefix}<number>)")


def _row(r):
    return dict(r) if r is not None else None


# ---- tasks ----------------------------------------------------------------

def get_task(conn, tid):
    t = _row(conn.execute("SELECT * FROM tasks WHERE id=?", (int(tid),)).fetchone())
    if not t:
        raise AesirError(f"no such task: {tid}")
    return t


def new_task(conn, actor, title, criteria="", requester=None, owner="", scope=""):
    requester, owner = normalise_role(conn, requester), normalise_role(conn, owner)
    with tx(conn):
        ts = now()
        cur = conn.execute(
            "INSERT INTO tasks (title, owner, requester, scope, spec, created, updated) VALUES (?,?,?,?,?,?,?)",
            (title, owner or "", requester or actor, normalise_scope(scope), criteria or "", ts, ts))
        tid = cur.lastrowid
        event(conn, actor, "task", tid, "new", {"title": title})
    return tid


def list_tasks(conn, status=None, owner=None):
    q, a = "SELECT id, title, status, owner, requester, scope, target_release FROM tasks WHERE 1=1", []
    if status:
        q += " AND status=?"; a.append(status)
    if owner:
        q += " AND owner=?"; a.append(owner)
    return [dict(r) for r in conn.execute(q + " ORDER BY id", a)]


RELEASE_PLACERS = ("advisor", "owner", "lead")  # besides the task's requester; the lead applies the advisor's placements
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def set_release(conn, actor, tid, version):
    """Place a task in a planned release (#228): 'X.Y.Z', or 'none' to clear. Requester, advisor or owner only."""
    v = "" if (version or "").strip().lower() in ("none", "") else version.strip().lstrip("v")
    if v and not VERSION_RE.match(v):
        raise AesirError(f"--release must be X.Y.Z or none, got {version!r}")
    with tx(conn):
        t = get_task(conn, tid)
        if actor != t["requester"] and actor not in RELEASE_PLACERS:
            raise AesirError(f"only the requester ({t['requester'] or 'unset'}), advisor, owner or lead can set a target release; caller is {actor}")
        if t["target_release"] == v:
            return False
        conn.execute("UPDATE tasks SET target_release=?, updated=? WHERE id=?", (v, now(), int(tid)))
        event(conn, actor, "task", tid, "set", {"target_release": {"from": t["target_release"], "to": v}})
        conn.execute("INSERT INTO task_log (task_id, ts, actor, text) VALUES (?,?,?,?)",
                     (int(tid), now(), actor, f"target release: {t['target_release'] or 'none'} -> {v or 'none'}"))
    return True


def show_task(conn, tid):
    t = get_task(conn, tid)
    t["log"] = [dict(r) for r in conn.execute(
        "SELECT ts, actor, text FROM task_log WHERE task_id=? ORDER BY id", (t["id"],))]
    from . import tracks
    t["track"] = tracks.task_track(t)
    t["links"] = [f"{r['b_type']}:{r['b_id']}" for r in conn.execute(
        "SELECT b_type, b_id FROM links WHERE a_type='task' AND a_id=?", (str(t["id"]),))]
    t["links"] += [f"{r['a_type']}:{r['a_id']}" for r in conn.execute(
        "SELECT a_type, a_id FROM links WHERE b_type='task' AND b_id=?", (str(t["id"]),))]
    from . import taskorder
    t["after"] = [{"id": i, "status": s} for i, s in taskorder.preds(conn, t["id"])]
    t["blocks"] = [{"id": i, "status": s} for i, s in taskorder.succs(conn, t["id"])]
    mid = load_extra(t).get("base_task")  # #177
    b = conn.execute("SELECT status FROM tasks WHERE id=?", (mid,)).fetchone() if mid else None
    t["base"] = {"id": mid, "status": b["status"] if b else "missing"} if mid else None
    return t


def split_task(conn, actor, tid, title, branch, criteria=None, after=None, repo=None):
    """Split off part of task `tid` as its own task (#177): same requester, owner and scope, its own branch, so it goes
    through review and the queue while `tid` stays as it is. Logs both ways; an 'after' link only when `after` is given
    (the id the new task runs after). Returns the new task id."""
    t = get_task(conn, tid)
    if not (title or "").strip():
        raise AesirError("task split needs --title")
    if not (branch or "").strip():
        raise AesirError("task split needs --branch")
    if after is not None:
        get_task(conn, after)
    spec = criteria if (criteria or "").strip() else f"split from #{t['id']}: {title}"
    new = new_task(conn, actor, title, spec, t["requester"], t["owner"], t["scope"])
    set_task(conn, actor, new, branch=branch, repo=repo)
    log_task(conn, actor, t["id"], f"split off #{new}: {title}")
    log_task(conn, actor, new, f"split from #{t['id']}: {title}")
    if after is not None:
        from . import taskorder
        taskorder.link(conn, actor, new, after)
    return new


def _check_transition(t, new, actor, reason, landed=False, carried=False):
    cur = t["status"]
    if new not in STATUSES:
        raise AesirError(f"unknown status: {new}")
    if cur in TERMINAL:
        raise AesirError(f"task is {cur}; no further transitions")
    if new == cur:
        raise AesirError(f"task is already {cur}")
    if landed and (new != "merged" or cur not in LANDED_FROM):
        raise AesirError(f"--landed only moves {' or '.join(LANDED_FROM)} to merged (task is {cur}, asked {new})")
    if carried and (new != "merged" or cur not in CARRIED_FROM):
        raise AesirError(f"--carried-by only moves {', '.join(CARRIED_FROM)} to merged (task is {cur}, asked {new})")
    no_code = new == "verified" and cur in NO_CODE_FROM  # #174/#182: the no-code close, checked further in set_task
    requeue = new == "approved" and cur == "changes_requested"  # #177, checked in set_task
    unclaim = new == "open" and cur in UNCLAIM_FROM  # #177, checked in set_task
    if new not in ("escalated", "wontfix") and new not in TRANSITIONS.get(cur, ()) and not (
            landed or carried or no_code or requeue or unclaim):
        raise AesirError(f"illegal transition {cur} -> {new}")
    if new == "claimed" and not t["spec"].strip():
        raise AesirError("cannot claim a task without acceptance criteria")
    if new == "verified" and actor != t["requester"]:
        raise AesirError(f"only the requester ({t['requester'] or 'unset'}) can verify; caller is {actor}")
    if new == "wontfix":
        if actor != "lead":
            raise AesirError("only lead can set wontfix")
        if not reason:
            raise AesirError("wontfix requires --reason")


def scope_entries(scope):
    """The one scope parser (#249): entries split on commas and whitespace, backslashes to '/', empties dropped.
    Entries therefore cannot contain spaces. Splitting on read too keeps old rows that stored 'a/ b/' as one entry working."""
    return [p.replace("\\", "/") for p in re.split(r"[,\s]+", scope or "") if p]


def normalise_scope(scope):
    """Comma-joined canonical form of a scope string, as stored by task new / task set --scope (#249)."""
    return ",".join(scope_entries(scope))


def in_scope(path, scope):
    """scope: comma- or space-separated; 'dir/' or 'dir' = whole subtree, otherwise a glob. Empty scope = unrestricted."""
    pats = scope_entries(scope)
    if not pats:
        return True
    nc = os.path.normcase  # case-insensitive on Windows, like fnmatch
    return any(nc(path).startswith(nc(p.rstrip("/") + "/")) or fnmatch.fnmatch(path, p) or _rust_module_root(nc(path), nc(p))
               for p in pats)


def scope_unmatched(repo, scope):
    """Scope entries that match no tracked path at the repo's main head (#209, reports#125/#224): a typo or a
    not-yet-created folder would otherwise make every edit refusal a surprise. Uses scope_entries and in_scope, so globs,
    'dir', 'dir/' and the Rust module root rule all count. No main ref or not a git repo: [] (nothing to check against)."""
    from . import gates  # local: gates imports this module at top level (circular otherwise)
    entries = scope_entries(scope)
    if not entries:
        return []
    try:
        paths = gates.git(repo or ".", "ls-tree", "-r", "-z", "--name-only", "main").split("\0")  # -z: git quotes non-ASCII names otherwise
        paths = [p for p in paths if p]
    except (AesirError, OSError):
        return []
    return [e for e in entries if not any(in_scope(p, e) for p in paths)]


def scope_warnings(repo, scope):
    """One 'warning: ...' line per unmatched scope entry (task new/set and spawn-context print these)."""
    return [f"warning: scope entry {e} matches no tracked path or directory" for e in scope_unmatched(repo, scope)]


def _rust_module_root(path, entry):
    """Rust module root (#201, reports#112): a plain directory entry `src/persist` also covers `src/persist.rs` (exact
    name only, so `src/persist2.rs` stays out). Matching on the path ending in X + '.rs' needs no filesystem access;
    `X/mod.rs` is already covered by the subtree rule. Globs and entries with a trailing slash never get this."""
    return not entry.endswith("/") and not any(c in entry for c in "*?[") and path == entry + ".rs"


def load_extra(t):
    try:
        x = json.loads(t.get("extra") or "{}")
    except ValueError:
        return {}
    return x if isinstance(x, dict) else {}


def resolve_branch_head(repo, ref):
    """(ref_used, sha) for a task branch ref (reports#119, #139). When `origin/<ref>` exists and the local ref is an
    ancestor of it (a fix round pushed from another worktree, local lags), the origin head is the branch head; else
    the local head. Uses refs as they are: never fetches. Raises AesirError when neither resolves.
    Every reader of a task branch head (evidence staleness, gate check, scope diff, docs-only, base task) calls this."""
    from . import gates
    repo = repo or "."
    remote = "origin/" + ref
    try:
        local = gates.git(repo, "rev-parse", "--verify", ref + "^{commit}")
    except AesirError:
        local = None
    try:
        origin = gates.git(repo, "rev-parse", "--verify", "refs/remotes/" + remote + "^{commit}")
    except AesirError:
        origin = None
    if local is None:
        if origin is None:
            raise AesirError(f"cannot resolve branch '{ref}'")
        return remote, origin
    if origin and origin != local:
        try:
            gates.git(repo, "merge-base", "--is-ancestor", local, origin)
            return remote, origin
        except AesirError:
            pass
    return ref, local


def task_for_head(conn, cwd, repo=None):
    """The task whose resolved branch head equals the HEAD of checkout `cwd` (reports#139): the credit target of a
    `gate run` without --task. None when no task, or more than one, matches (ambiguous is not credited)."""
    from . import gates
    try:
        head = gates.git(cwd, "rev-parse", "HEAD")
    except (AesirError, OSError):
        return None
    try:
        main_head = gates.git(repo or cwd, "rev-parse", "--verify", "main^{commit}")
    except (AesirError, OSError):
        main_head = None
    hits = []
    for r in conn.execute("SELECT id, branch FROM tasks WHERE branch!='' AND status NOT IN ('merged','verified','wontfix') ORDER BY id"):
        try:
            bh = resolve_branch_head(repo or cwd, r["branch"])[1]
            if bh == head and bh != main_head:  # a fresh branch (head == main) is not a credit target
                hits.append(r["id"])
        except (AesirError, OSError):
            continue
    return hits[0] if len(hits) == 1 else None


def diff_base(conn, t, repo):
    """What task t's branch is diffed against (#177): the branch of its --base-task while that task is not merged and the
    branch resolves, else main. Every scope / docs-only / audit check goes through this one answer."""
    mid = load_extra(t).get("base_task")
    if not mid:
        return "main"
    try:
        m = get_task(conn, mid)
    except AesirError:
        return "main"
    if m["status"] in ("merged", "verified") or not m["branch"]:
        return "main"
    from . import gates
    try:
        return resolve_branch_head(repo, m["branch"])[0]  # origin/<branch> when the local ref lags it
    except (AesirError, OSError):
        return "main"


def checkout_diff_base(conn, cwd, tid=None):
    """diff_base for the task of an audited checkout: task `tid`, else the task whose branch is the checkout's branch."""
    from . import gates
    try:
        if tid is not None:
            return diff_base(conn, get_task(conn, tid), cwd)
        br = gates.git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
        row = conn.execute("SELECT * FROM tasks WHERE branch=? ORDER BY id DESC LIMIT 1", (br,)).fetchone()
        return diff_base(conn, dict(row), cwd) if row else "main"
    except (AesirError, OSError):
        return "main"


def branch_out_of_scope(repo, branch, scope, base="main"):
    """Paths changed on `branch` since it left `base` that fall outside `scope` (the tool-independent backstop)."""
    if not (scope or "").strip():
        return []
    return [p for p in _branch_diff(repo, branch, base) if not in_scope(p, scope)]


def _branch_diff(repo, branch, base="main"):
    """Paths `branch` changes vs `base` (three-dot: what is not already on base). Raises AesirError when git cannot diff."""
    from . import gates
    branch = resolve_branch_head(repo, branch)[0]  # the task's own ref lags origin too (reports#139)
    if base != "main":
        try:
            base = resolve_branch_head(repo, base)[0]
        except AesirError:
            pass  # a sha (the queue's main_sha) or an already-resolved ref
    out = gates.git(repo, "-c", "core.quotepath=off", "diff", "--name-only", "--no-renames", f"{base}...{branch}")
    return [p for p in out.splitlines() if p]


NO_CODE_FROM = ("open", "claimed", "in_progress")  # #182: a task nobody moved on (VT #221) can still close without merge
LANDED_FROM = ("in_progress", "in_review")  # the docs-only landed path (#145) is the only route from these to merged
DOCS_ONLY = ("docs/",)  # plus the top-level README.md; everything else changes behaviour or policy


# Files a docs task's commit may carry without that making it a code change (#242): what Aesir writes (state export,
# roadmap, vendored package) and CLAUDE.md, which nothing in this repo writes but client projects regenerate with their
# own tooling. Names come from their owners (releases, backup, install); imported inside the function because those
# modules import service. Agent defs and policy files stay refused.
LANDED_PATH_CAP = 10  # refusal lists this many paths, then "(+N more)"


def generated_paths():
    """(files, dir prefixes) a docs-only commit may touch besides docs."""
    from . import backup, install, releases
    return ("CLAUDE.md", releases.ROADMAP_FILE), (backup.STATE_DIR + "/", install.PKG_DIR + "/")


def is_docs_path(p):
    return p == "README.md" or p.startswith(DOCS_ONLY)


def is_generated_path(p):
    files, dirs = generated_paths()
    return p in files or p.startswith(dirs)


def check_landed(repo, spec, scope="", tid=None):
    """`<sha>` or `<base>..<sha>`: sha must be an ancestor of main (or origin/main) and the range touch only docs paths,
    generated files (#242) and paths inside the task's `scope` (same in_scope matcher as the merge queue; an empty
    scope allows nothing extra, it does not mean unrestricted here). Returns the full sha. Never a gate bypass for code."""
    from . import gates
    repo = repo or "."
    base, _, sha = spec.rpartition("..") if ".." in spec else ("", "", spec)
    try:
        full = gates.git(repo, "rev-parse", "--verify", sha + "^{commit}")
        base_full = gates.git(repo, "rev-parse", "--verify", base + "^{commit}") if base else None
    except AesirError:
        raise AesirError(f"--landed: cannot resolve {spec} to a commit")
    for main in ("main", "origin/main"):
        try:
            gates.git(repo, "merge-base", "--is-ancestor", full, main)
            break
        except AesirError:
            continue
    else:
        raise AesirError(f"--landed: {full[:8]} is not an ancestor of main (or origin/main)")
    if base_full is None:
        try:
            base_full = gates.git(repo, "rev-parse", "--verify", full + "^")
        except AesirError:  # root commit: diff against the empty tree
            base_full = gates.git(repo, "hash-object", "-t", "tree", "/dev/null")
    out = gates.git(repo, "-c", "core.quotepath=off", "diff", "--name-only", "--no-renames", base_full, full)
    # an empty scope would make in_scope say "everything", which must never widen the docs-only rule
    bad = [p for p in out.splitlines() if not (is_docs_path(p) or is_generated_path(p) or
                                                ((scope or "").strip() and in_scope(p, scope)))]
    if bad:
        n = tid if tid is not None else "<N>"
        files, dirs = generated_paths()
        shown = ", ".join(bad[:LANDED_PATH_CAP]) + (f" (+{len(bad) - LANDED_PATH_CAP} more)" if len(bad) > LANDED_PATH_CAP else "")
        raise AesirError(
            f"--landed takes docs/, README.md, generated files ({', '.join(files + dirs)}) and "
            f"paths inside the task scope ({scope or 'none set'}); refused paths: {shown}. Widen scope with "
            f"'task set {n} --scope \"<current>,<path>\"' if they belong to this task; a task with no code to merge "
            f"closes with 'task set {n} --status verified --reason \"<finding>\"' (requester only; refused when its "
            f"branch carries code); code goes the normal route (in_review, approved, merged)")
    return full


def branch_paths(repo, branch, base="main", strict=False):
    """Paths the branch changes vs main. [] when no branch is recorded; an unresolvable branch is [] unless strict (then it raises)."""
    if not branch:
        return []
    try:
        return _branch_diff(repo or ".", branch, base)
    except AesirError:
        if strict:
            raise
        return []


def check_no_code(t, reason, repo, base="main"):
    """open/claimed/in_progress -> verified (#174, #182, D178): finish with nothing to merge. Needs a reason; refused when the branch carries code."""
    if not (reason or "").strip():
        raise AesirError("closing without merge requires --reason (the finding / what was done)")
    try:
        paths = branch_paths(repo, t["branch"], base, strict=True)
    except AesirError:  # a recorded branch we cannot diff may carry code: never read that as "no code"
        raise AesirError(f"task {t['id']} branch '{t['branch']}' cannot be diffed against main; fix or clear --branch, "
                         f"or use the normal path: in_review -> approved -> merged -> verified")
    code = [p for p in paths if not is_docs_path(p)]
    if code:
        raise AesirError(f"task {t['id']} branch {t['branch']} has code not on main ({', '.join(code[:10])}); "
                         f"use the normal path: in_review -> approved -> merged -> verified")


CARRIED_FROM = ("in_progress", "in_review", "approved")


def gated_merge_sha(conn, tid, repo):
    """Newest passing full gate run of task `tid` whose sha is an ancestor of main (or origin/main): the sha the queue
    merged (#234). Fallback for tasks the queue merged before it recorded merge_commit. None when there is none."""
    from . import proc
    rows = conn.execute("SELECT sha FROM gate_runs WHERE task_id=? AND profile='full' AND passed=1 AND sha!='' ORDER BY id DESC",
                        (int(tid),)).fetchall()
    for r in rows:
        for ref in ("main", "origin/main"):
            try:
                ok = proc.run(["git", "merge-base", "--is-ancestor", r["sha"], ref], cwd=str(repo or "."),
                              capture_output=True, text=True).returncode == 0
            except OSError:
                ok = False
            if ok:
                return r["sha"]
    return None


def check_carried_by(conn, t, carrier, repo=None):
    """--carried-by B (#174): B must be merged/verified with a merge_commit (#234: or a passing full gate run on main). Returns B's merge commit."""
    b = get_task(conn, parse_id(str(carrier).lstrip("#"), "T"))
    if b["id"] == t["id"]:
        raise AesirError("--carried-by cannot name the task itself")
    if b["status"] not in ("merged", "verified"):
        raise AesirError(f"--carried-by: task {b['id']} is {b['status']}, not merged or verified")
    try:
        sha = json.loads(b["extra"] or "{}").get("merge_commit")
    except ValueError:
        sha = None
    if not sha:
        sha = gated_merge_sha(conn, b["id"], repo)
    if not sha:
        raise AesirError(f"--carried-by: task {b['id']} has no merge_commit recorded and no passing full gate run "
                         f"that is an ancestor of main or origin/main")
    return b["id"], sha


def backfill_merge_commits(conn, actor, repo):
    """#234: give every merged/verified task without extra.merge_commit the sha of its newest passing full gate run on
    main. Idempotent. Returns (done [(id, sha)], left [id])."""
    done, left = [], []
    with tx(conn):
        for r in conn.execute("SELECT id, extra FROM tasks WHERE status IN ('merged','verified') ORDER BY id").fetchall():
            try:
                extra = json.loads(r["extra"] or "{}")
            except ValueError:
                extra = {}
            if extra.get("merge_commit"):
                continue
            sha = gated_merge_sha(conn, r["id"], repo)
            if not sha:
                left.append(r["id"])
                continue
            extra["merge_commit"] = sha
            conn.execute("UPDATE tasks SET extra=?, updated=? WHERE id=?", (json.dumps(extra, ensure_ascii=False), now(), r["id"]))
            event(conn, actor, "task", r["id"], "set", {"extra.merge_commit": {"from": None, "to": sha}})
            conn.execute("INSERT INTO task_log (task_id, ts, actor, text) VALUES (?,?,?,?)",
                         (r["id"], now(), actor, f"merge_commit backfilled from gate run: {sha[:12]}"))
            done.append((r["id"], sha))
    return done, left


def check_base_task(conn, t, base, repo=None):
    """--base-task M (#177): N is built on M's branch. Returns M's id, or None for '' / 0 / none (clears it)."""
    if str(base).strip().lower() in ("", "0", "none"):
        return None
    m = get_task(conn, parse_id(str(base).lstrip("#"), "T"))
    if m["id"] == t["id"]:
        raise AesirError("--base-task cannot name the task itself")
    if m["branch"] and m["branch"] == t["branch"]:
        raise AesirError(f"--base-task: task {m['id']} is on the same branch ({m['branch']}) as task {t['id']}")
    if m["branch"] and t["branch"]:
        mh, nh = approved_head(repo, m["branch"]), approved_head(repo, t["branch"])
        if mh and mh == nh:
            raise AesirError(f"--base-task: task {m['id']}'s branch head equals task {t['id']}'s own head; nothing to build on")
    return m["id"]


def approved_head(repo, branch):
    """Branch head sha recorded when a task is approved (the requeue check compares against it); None when unresolvable."""
    from . import gates
    if not branch:
        return None
    try:
        return resolve_branch_head(repo, branch)[1]
    except (AesirError, OSError):
        return None


def check_requeue(t, actor, reason, repo):
    """changes_requested -> approved (#177): lead only, needs a reason, and only while the branch head is still the one
    recorded at its last approval (a queue refusal with an unchanged branch). Returns that head."""
    if actor != "lead":
        raise AesirError(f"only lead can requeue (caller is {actor})")
    if not (reason or "").strip():
        raise AesirError("requeue (changes_requested -> approved) requires --reason")
    was = load_extra(t).get("approved_head")
    if not was:
        raise AesirError(f"task {t['id']} has no head recorded at an approval; use the normal path: in_progress -> in_review")
    now_head = approved_head(repo, t["branch"])
    if now_head != was:
        raise AesirError(f"task {t['id']} branch head {(now_head or 'unresolvable')[:12]} is not the approved head {was[:12]}; "
                         f"the branch moved, use the normal path: in_progress -> in_review")
    return was


def check_unclaim(conn, t, reason):
    """claimed / in_progress -> open (#177): needs a reason and no running agent bound to the task."""
    if not (reason or "").strip():
        raise AesirError("unclaim (-> open) requires --reason")
    a = conn.execute("SELECT agent_id FROM agents WHERE task_id=? AND ended='' ORDER BY id LIMIT 1", (t["id"],)).fetchone()
    if a:
        raise AesirError(f"task {t['id']} has a running agent bound to it ({a['agent_id']}); end it first")


GATE_NEEDED = {"in_review": "quick", "merged": "full"}
DOCS_ONLY_NOTE = "docs-only branch: code gate not needed"


def _check_gate(conn, t, new, actor, reason, repo, skip, gated_sha=None):
    """Entering in_review (quick) or merged (full) needs a branch with a passing gate on its head.
    gated_sha: the queue's merge commit, the sha the full gate ran on (the branch head itself is never gated, D154)."""
    need = GATE_NEEDED.get(new)
    if not need:
        return
    if gated_sha and new == "merged" and not skip:
        from . import gates
        if not gates.has_pass(conn, t["id"], need, gated_sha):
            raise AesirError(f"no passing {need} gate for task {t['id']} on merge commit {gated_sha[:8]}")
        return
    if not t["branch"] and not skip:
        raise AesirError(f"task {t['id']} has no branch, so no gate can be checked: set --branch or --force-gate-skip --reason")
    if skip:
        if actor != "lead":
            raise AesirError("only lead can --force-gate-skip")
        if not reason:
            raise AesirError("--force-gate-skip requires --reason")
        from . import friction
        friction.record(conn, actor, "gate_skip", f"{need} gate skipped: {reason}", task=t["id"])
        return
    from . import gates
    try:
        ref_used, sha = resolve_branch_head(repo, t["branch"])
    except AesirError:
        raise AesirError(f"cannot resolve branch '{t['branch']}' to check the {need} gate")
    if new == "in_review":
        from . import tracks
        scope = tracks.effective_scope(t, repo or ".")
        base = diff_base(conn, t, repo)
        bad = branch_out_of_scope(repo or ".", t["branch"], scope, base)
        if bad:
            raise AesirError(f"task {t['id']} branch touches paths outside scope ({scope}): {', '.join(bad[:10])}")
    if new == "in_review":
        paths = branch_paths(repo or ".", t["branch"], base)
        if paths and all(is_docs_path(p) for p in paths):  # #174: docs-only branch needs no code gate
            return DOCS_ONLY_NOTE
    if not gates.has_pass(conn, t["id"], need, sha):
        raise AesirError(f"no passing {need} gate for task {t['id']} on {ref_used} head {sha[:8]}; "
                         f"run: aesir gate run {need} --task {t['id']} (in the task worktree)")


def _check_evidence(conn, t, actor, reason, repo, skip):
    """Entering in_review needs executed evidence per numbered criterion at the branch head (task #55)."""
    from . import evidence
    if skip:
        if actor != "lead":
            raise AesirError("only lead can --force-evidence-skip")
        if not reason:
            raise AesirError("--force-evidence-skip requires --reason")
        from . import friction
        friction.record(conn, actor, "evidence_skip", f"evidence check skipped: {reason}", task=t["id"])
        return []
    return evidence.check_in_review(conn, t, repo)


AUTO_END_STATUSES = ("merged", "verified", "wontfix")


def set_task(conn, actor, tid, status=None, reason=None, repo=None, force_gate_skip=False,
             force_evidence_skip=False, landed=None, gated_sha=None, carried_by=None, base_task=None, **fields):
    """Update fields and/or status. Unknown field names are an error."""
    bad = set(fields) - set(FIELDS)
    if bad:
        raise AesirError(f"unknown fields: {sorted(bad)}")
    changes = {k: v for k, v in fields.items() if v is not None}
    notes = []  # non-blocking evidence notes for the caller (RAN criteria, command-match warnings)
    for k in ("owner", "requester"):
        if k in changes:
            changes[k] = normalise_role(conn, changes[k])
    if "scope" in changes:
        changes["scope"] = normalise_scope(changes["scope"])
    with tx(conn):
        t = get_task(conn, tid)
        t.update(changes)  # so a same-call --criteria satisfies the claim check
        landed_sha = carrier = gate_note = None
        extra = load_extra(t)
        extra0 = dict(extra)
        more_log = []  # task_log lines after the status line (#177)
        if base_task is not None:
            bt = check_base_task(conn, t, base_task, repo)
            if bt is None:
                extra.pop("base_task", None)
            else:
                extra["base_task"] = bt
            if extra != extra0:
                more_log.append(f"base task: {'#' + str(bt) if bt else 'cleared'}")
        if landed and carried_by:
            raise AesirError("--landed and --carried-by are exclusive")
        if (landed or carried_by) and not status:
            raise AesirError(f"{'--landed' if landed else '--carried-by'} needs --status merged")
        if status:
            _check_transition(t, status, actor, reason, landed=bool(landed), carried=bool(carried_by))
            if status == "approved" and t["status"] == "changes_requested":
                head = check_requeue(t, actor, reason, repo)
                more_log.append(f"requeued unchanged head {head[:12]}: {reason}")
            if status == "open" and t["status"] in UNCLAIM_FROM:
                check_unclaim(conn, t, reason)
                more_log.append(f"unclaimed: {reason}")
            if status == "approved" and t["status"] == "in_review" and repo:  # repo: the CLI always passes it
                head = approved_head(repo, t["branch"])
                if head:
                    extra["approved_head"] = head
            if landed:
                from . import tracks
                landed_sha = check_landed(repo, landed, tracks.effective_scope(t, repo or "."), t["id"])
            if carried_by:
                carrier = check_carried_by(conn, t, carried_by, repo)
                landed_sha = carrier[1]
            if status == "verified" and t["status"] in NO_CODE_FROM:
                check_no_code(t, reason, repo, diff_base(conn, t, repo))
            if status == "in_review" and not force_gate_skip:
                from . import observe
                observe.lint_ui(t)
            if not landed_sha:
                gate_note = _check_gate(conn, t, status, actor, reason, repo, force_gate_skip, gated_sha)
            if status == "in_review":
                notes = _check_evidence(conn, t, actor, reason, repo, force_evidence_skip)
            changes["status"] = status
            if status == "in_review":
                changes["attempts"] = t["attempts"] + 1
            if status == "escalated":
                changes["escalations"] = t["escalations"] + 1
            if status == "wontfix":
                changes["wontfix_reason"] = reason
            if landed_sha:
                extra["merge_commit"] = landed_sha
            elif gated_sha and status == "merged":  # the queue's merge commit (#234)
                extra["merge_commit"] = gated_sha
        if extra != extra0:
            changes["extra"] = json.dumps(extra, ensure_ascii=False)
        if not changes:
            raise AesirError("nothing to change")
        old = get_task(conn, tid)
        sets = ", ".join(f"{k}=?" for k in changes)
        conn.execute(f"UPDATE tasks SET {sets}, updated=? WHERE id=?", [*changes.values(), now(), int(tid)])
        event(conn, actor, "task", tid, "set",
              {k: {"from": old[k], "to": v} for k, v in changes.items()} | ({"reason": reason} if reason else {}))
        if status:
            why = f" ({reason})" if reason else ""
            if force_gate_skip and GATE_NEEDED.get(status):
                why += " [gate skipped]"
            if force_evidence_skip and status == "in_review":
                why += " [evidence skipped]"
            _log_status(conn, actor, old, status, reason, force_gate_skip, force_evidence_skip, carrier, landed_sha, gate_note, more_log)
            from . import taskorder  # #176: starting before a predecessor is done is allowed, but loudly
            warn = taskorder.start_warning(conn, actor, tid, status)
            if warn:
                notes = [*notes, warn]
        else:
            for text in more_log:
                conn.execute("INSERT INTO task_log (task_id, ts, actor, text) VALUES (?,?,?,?)", (int(tid), now(), actor, text))
    if status == "merged":  # a merged review-pass task records its file's review (#135)
        from . import rank
        rank.on_merged(conn, actor, get_task(conn, tid), repo)
    _after_status(conn, actor, tid, status)
    return notes


def _log_status(conn, actor, old, status, reason, gate_skip, evidence_skip, carrier, landed_sha, gate_note, more=()):
    """The status line plus the extra log lines (landed/carried, docs-only gate note, closed without merge)."""
    tid = old["id"]
    why = f" ({reason})" if reason else ""
    if gate_skip and GATE_NEEDED.get(status):
        why += " [gate skipped]"
    if evidence_skip and status == "in_review":
        why += " [evidence skipped]"
    lines = [f"status {old['status']} -> {status}{why}"]
    if landed_sha:
        lines.append(f"landed via #{carrier[0]} at {landed_sha[:12]}" if carrier else f"landed at {landed_sha[:12]}")
    if gate_note:
        lines.append(gate_note)
    if status == "verified" and old["status"] in NO_CODE_FROM:
        lines.append(f"closed without merge: {reason}")
    lines += more
    for text in lines:
        conn.execute("INSERT INTO task_log (task_id, ts, actor, text) VALUES (?,?,?,?)", (int(tid), now(), actor, text))


def _after_status(conn, actor, tid, status):
    if status in AUTO_END_STATUSES:  # #143: agents that never got a SubagentStop
        end_task_agents(conn, actor, tid, status)


def log_task(conn, actor, tid, text):
    with tx(conn):
        get_task(conn, tid)
        conn.execute("INSERT INTO task_log (task_id, ts, actor, text) VALUES (?,?,?,?)",
                     (int(tid), now(), actor, text))
        event(conn, actor, "task", tid, "log", {"text": text})


def add_link(conn, actor, a_type, a_id, b_type, b_id):
    with tx(conn):
        conn.execute("INSERT OR IGNORE INTO links VALUES (?,?,?,?)", (a_type, str(a_id), b_type, str(b_id)))
        event(conn, actor, a_type, a_id, "link", {"to": f"{b_type}:{b_id}"})


def unlink_upstream(conn, actor, tid, issue):
    """Remove a task's upstream issue link (#259): a wrong or superseded link made the release plan announce an issue
    that was only partly fixed. Only the lead or the task's requester; logged in task_log with the actor."""
    t = get_task(conn, tid)
    if actor != "lead" and actor != t["requester"]:
        raise AesirError(f"only the lead or the requester ({t['requester'] or 'unset'}) can remove an upstream link; caller is {actor}")
    m = re.search(r"(\d+)$", str(issue).strip())
    if not m:
        raise AesirError(f"not an issue number or url: {issue}")
    n = m.group(1)
    with tx(conn):
        rows = [r[0] for r in conn.execute("SELECT b_id FROM links WHERE a_type='task' AND a_id=? AND b_type='upstream'",
                                           (str(int(tid)),))]
        hit = [b for b in rows if re.search(r"(?:^|/issues/)(\d+)$", b) and re.search(r"(\d+)$", b).group(1) == n]
        if not hit:
            raise AesirError(f"task #{int(tid)} has no upstream link to issue {n}")
        for b in hit:
            conn.execute("DELETE FROM links WHERE a_type='task' AND a_id=? AND b_type='upstream' AND b_id=?",
                         (str(int(tid)), b))
        conn.execute("INSERT INTO task_log (task_id, ts, actor, text) VALUES (?,?,?,?)",
                     (int(tid), now(), actor, f"unlinked upstream issue #{n}"))
        event(conn, actor, "task", tid, "unlink", {"upstream": n})
    return n


# ---- decisions --------------------------------------------------------------

def add_decision(conn, actor, title, context="", choice="", alternatives="", decided_by="",
                 reversible="yes", tasks=(), supersedes=(), _inner=False):
    with (contextlib.nullcontext() if _inner else tx(conn)):
        old = [_decision_id(conn, s) for s in supersedes]
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        cur = conn.execute(
            "INSERT INTO decisions (date, title, context, choice, alternatives, decided_by, reversible, ts)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (date, title, context, choice, alternatives, decided_by or actor, reversible, now()))
        did = cur.lastrowid
        for t in tasks:
            get_task(conn, t)
            conn.execute("INSERT OR IGNORE INTO links VALUES ('decision',?,'task',?)", (str(did), str(int(t))))
        for o in old:
            conn.execute("INSERT OR IGNORE INTO decision_supersedes VALUES (?,?,?,?)", (did, o, actor, now()))
        event(conn, actor, "decision", did, "add", {"title": title, "tasks": list(tasks), "supersedes": old})
    return did


def _decision_id(conn, did):
    n = parse_id(did, "D")
    if not conn.execute("SELECT 1 FROM decisions WHERE id=?", (n,)).fetchone():
        raise AesirError(f"no such decision: {did}")
    return n


def amend_decision(conn, actor, did, note):
    """Append a correction; the decision row itself is never edited (history is append-only)."""
    if not (note or "").strip():
        raise AesirError("amendment note is empty")
    with tx(conn):
        n = _decision_id(conn, did)
        cur = conn.execute("INSERT INTO decision_amendments (decision_id, note, actor, ts) VALUES (?,?,?,?)",
                           (n, note.strip(), actor, now()))
        event(conn, actor, "decision", n, "amend", {"amendment": cur.lastrowid})
    return cur.lastrowid


def list_decisions(conn):
    sup = {}
    for old, new in conn.execute("SELECT old_id, new_id FROM decision_supersedes ORDER BY new_id"):
        sup.setdefault(old, []).append(new)
    rows = [dict(r) for r in conn.execute(
        "SELECT id, date, title, decided_by, reversible,"
        " (SELECT COUNT(*) FROM decision_amendments a WHERE a.decision_id=decisions.id) AS amendments"
        " FROM decisions ORDER BY id")]
    for r in rows:
        r["superseded_by"] = sup.get(r["id"], [])
    return rows


def show_decision(conn, did):
    d = _row(conn.execute("SELECT * FROM decisions WHERE id=?", (parse_id(did, "D"),)).fetchone())
    if not d:
        raise AesirError(f"no such decision: {did}")
    d["tasks"] = [r["b_id"] for r in conn.execute(
        "SELECT b_id FROM links WHERE a_type='decision' AND a_id=? AND b_type='task'", (str(d["id"]),))]
    d["amendments"] = [dict(r) for r in conn.execute(
        "SELECT id, note, actor, ts FROM decision_amendments WHERE decision_id=? ORDER BY id", (d["id"],))]
    d["supersedes"] = [r[0] for r in conn.execute(
        "SELECT old_id FROM decision_supersedes WHERE new_id=? ORDER BY old_id", (d["id"],))]
    d["superseded_by"] = [r[0] for r in conn.execute(
        "SELECT new_id FROM decision_supersedes WHERE old_id=? ORDER BY new_id", (d["id"],))]
    return d


# ---- tips -------------------------------------------------------------------

def _tags(tags):
    """Comma/space separated tags -> normalised 'a,b' (lower case, no duplicates)."""
    seen = []
    for t in re.split(r"[,\s]+", (tags or "").lower()):
        if t and t not in seen:
            seen.append(t)
    return ",".join(seen)


def add_tip(conn, actor, body, topic="", tags=""):
    with tx(conn):
        if conn.execute("SELECT 1 FROM tips WHERE topic=? AND body=?", (topic, body)).fetchone():
            return None  # check first: INSERT OR IGNORE would burn an AUTOINCREMENT id
        cur = conn.execute("INSERT INTO tips (topic, body, ts, tags) VALUES (?,?,?,?)",
                           (topic, body, now(), _tags(tags)))
        if has_fts(conn):
            conn.execute("INSERT INTO tips_fts (rowid, topic, body) VALUES (?,?,?)", (cur.lastrowid, topic, body))
        event(conn, actor, "tip", cur.lastrowid, "add", {"topic": topic, "tags": _tags(tags)})
        return cur.lastrowid


def _tip(conn, tid):
    t = _row(conn.execute("SELECT id, topic, body FROM tips WHERE id=?", (parse_id(tid, "T"),)).fetchone())
    if not t:
        raise AesirError(f"no such tip: {tid}")
    return t


def edit_tip(conn, actor, tid, body, topic=None, tags=None):
    """Replace a tip's text (and its topic, if given)."""
    if not body or not body.strip():
        raise AesirError("tip text must not be empty")
    with tx(conn):
        t = _tip(conn, tid)
        topic = t["topic"] if topic is None else topic
        if conn.execute("SELECT 1 FROM tips WHERE topic=? AND body=? AND id!=?", (topic, body, t["id"])).fetchone():
            raise AesirError("another tip already has that topic and text")
        conn.execute("UPDATE tips SET topic=?, body=? WHERE id=?", (topic, body, t["id"]))
        if tags is not None:
            conn.execute("UPDATE tips SET tags=? WHERE id=?", (_tags(tags), t["id"]))
        if has_fts(conn):
            conn.execute("DELETE FROM tips_fts WHERE rowid=?", (t["id"],))
            conn.execute("INSERT INTO tips_fts (rowid, topic, body) VALUES (?,?,?)", (t["id"], topic, body))
        event(conn, actor, "tip", t["id"], "edit", {"topic": topic, "old": t["body"], "new": body})


def rm_tip(conn, actor, tid):
    with tx(conn):
        t = _tip(conn, tid)
        conn.execute("DELETE FROM tips WHERE id=?", (t["id"],))
        conn.execute("DELETE FROM tip_impressions WHERE tip_id=?", (t["id"],))
        if has_fts(conn):
            conn.execute("DELETE FROM tips_fts WHERE rowid=?", (t["id"],))
        event(conn, actor, "tip", t["id"], "rm", {"topic": t["topic"], "body": t["body"]})


def _retire_tip(conn, actor, tid, kind, ref):
    """Mark a tip promoted (kept for history, no longer surfaced or searched) and link it to its replacement.
    Caller holds the transaction."""
    conn.execute("UPDATE tips SET retired=? WHERE id=?", (now(), tid))
    conn.execute("INSERT OR IGNORE INTO links VALUES ('tip',?,?,?)", (str(tid), kind, str(ref)))
    event(conn, actor, "tip", tid, "retire", {"to": f"{kind}:{ref}"})


def search_tips(conn, words):
    words = [w for w in words if w.strip()]
    if not words:
        raise AesirError("give at least one search word")
    if has_fts(conn):
        q = " ".join('"%s"*' % w.replace('"', '""') for w in words)
        rows = conn.execute("SELECT tips.id, tips.topic, tips.body FROM tips_fts JOIN tips ON tips.id = tips_fts.rowid"
                            " WHERE tips_fts MATCH ? AND tips.retired='' ORDER BY rank", (q,))
    else:
        cond = " AND ".join("(topic LIKE ? OR body LIKE ?)" for _ in words)
        args = [f"%{w}%" for w in words for _ in (0, 1)]
        rows = conn.execute(f"SELECT id, topic, body FROM tips WHERE retired='' AND {cond} ORDER BY id", args)
    return [dict(r) for r in rows]


# ---- roles ------------------------------------------------------------------

def checkin(conn, actor, role, session=""):
    with tx(conn):
        conn.execute("INSERT INTO roles (role, session, last_checkin) VALUES (?,?,?)"
                     " ON CONFLICT(role) DO UPDATE SET session=excluded.session,"
                     " previous_checkin=roles.last_checkin, last_checkin=excluded.last_checkin",
                     (role, session, now()))
        event(conn, actor, "role", role, "checkin", {"session": session})


def set_session_name(conn, role, name):
    """Record the role's real session name (#120); no-op when unknown or the role never checked in."""
    name = (name or "").strip().lower()
    if name:
        with tx(conn):
            conn.execute("UPDATE roles SET session_name=? WHERE role=? AND session_name!=?", (name, role, name))


def role_note(conn, actor, role, text):
    with tx(conn):
        conn.execute("INSERT INTO role_notes (role, ts, actor, text) VALUES (?,?,?,?)", (role, now(), actor, text))
        event(conn, actor, "role", role, "note", {"text": text})


def show_role(conn, role):
    r = _row(conn.execute("SELECT * FROM roles WHERE role=?", (role,)).fetchone()) or \
        {"role": role, "session": "", "session_name": "", "last_checkin": "", "previous_checkin": ""}
    r["notes"] = [dict(n) for n in conn.execute(
        "SELECT ts, actor, text FROM role_notes WHERE role=? ORDER BY id", (role,))]
    return r


def _insert_agent(conn, actor, role, agent_id, task, model, session, agent_type="", tool_use_id="", strict=True):
    """Caller holds the transaction. A restarted (ended) id gets a new row; history is kept."""
    if task is not None:
        if strict:
            get_task(conn, task)
        elif not conn.execute("SELECT 1 FROM tasks WHERE id=?", (int(task),)).fetchone():
            task = None
    if conn.execute("SELECT 1 FROM agents WHERE role=? AND agent_id=? AND ended=''", (role, agent_id)).fetchone():
        raise AesirError(f"agent {agent_id} is already running for {role}")
    conn.execute("INSERT INTO agents (role, agent_id, task_id, model, session, started, agent_type, tool_use_id)"
                 " VALUES (?,?,?,?,?,?,?,?)",
                 (role, agent_id, task, model or "", session or "", now(), agent_type, tool_use_id))
    event(conn, actor, "agent", agent_id, "start", {"role": role, "task": task, "model": model or ""})


def agent_start(conn, actor, role, agent_id, task=None, model="", session=""):
    with tx(conn):
        _insert_agent(conn, actor, role, agent_id, task, model, session)


def end_agent(conn, actor, a, result="", ok=False, start=None, cols=None, ev=None):
    """The ONE way a running agent row ends (#143): SubagentStop, `agent end`, `agent end --stale`, the status sweep
    and the task-status auto-end all come here, so they cannot drift. Updates the row (`cols` = extra columns to set,
    `ev` = extra event fields), then runs the shared cleanup: undelivered steering notes expire with a sender notice
    (#141) and the scratch folder is removed on a clean finish (`ok`) or kept then swept (#138). `start` is a directory
    to find the project from (the agent's cwd). Returns False when the row had already ended."""
    cols = dict(cols or {})
    if "tokens_in" not in cols:  # ended without a SubagentStop (stale, killed, superseded): keep what the transcript holds
        tk = _transcript_tokens(conn, a)
        if tk:
            cols.update({"tokens_" + k: v for k, v in tk.items()})
            ev = {**(ev or {}), "tokens": tk}
    with tx(conn):
        sets = "".join(f", {k}=?" for k in cols)
        cur = conn.execute(f"UPDATE agents SET ended=?, result=?{sets} WHERE id=? AND ended=''",
                           [now(), result or "", *cols.values(), a["id"]])
        if not cur.rowcount:
            return False
        event(conn, actor, "agent", a["agent_id"], "end", {"role": a["role"], **(ev or {})})
    _end_cleanup(conn, actor, a, ok, start)
    return True


def _prior_tokens(conn, harness_id, row_id):
    """(in, out, cache_read, cache_create) already held by the other rows of one resumed harness instance."""
    return tuple(conn.execute("SELECT COALESCE(SUM(tokens_in),0), COALESCE(SUM(tokens_out),0),"
                              " COALESCE(SUM(tokens_cache_read),0), COALESCE(SUM(tokens_cache_create),0)"
                              " FROM agents WHERE harness_agent_id=? AND id!=?", (harness_id, row_id)).fetchone())


def _transcript_tokens(conn, a):
    """{in, out, cache_read, cache_create} read from the row's transcript (agenthook.transcript_usage, as stop_agent
    and rounds do), less what earlier rows of the same resumed instance already hold; None without a readable file."""
    path = a["transcript_path"] if "transcript_path" in a.keys() else ""
    if not path or not os.path.isfile(path):
        return None
    from .agenthook import transcript_usage  # lazy: agenthook imports this module
    tot = transcript_usage(path)[0]
    prior = _prior_tokens(conn, a["harness_agent_id"], a["id"]) if a["harness_agent_id"] else (0, 0, 0, 0)
    prior = dict(zip(("in", "out", "cache_read", "cache_create"), prior))
    return {k: max(0, tot.get(s, 0) - prior[k]) for k, s in
            (("in", "input"), ("out", "output"), ("cache_read", "cache_read"), ("cache_create", "cache_create"))}


def transcript_idle_min(a, at=None):
    """Minutes since the agent's transcript was last written (#195); None when it has no readable transcript."""
    path = a["transcript_path"] if "transcript_path" in a.keys() else ""
    try:
        m = os.path.getmtime(path) if path else None
    except OSError:
        m = None
    if m is None:
        return None
    return max(((at or datetime.now(timezone.utc)).timestamp() - m) / 60, 0.0)


def end_finished_agents(conn, actor, idle_min, at=None, orphan_min=None):
    """#195 (reports#136): a running agent whose transcript has had no write for idle_min and holds no open tool call
    has finished (its Workflow ended without a SubagentStop). End it, tokens kept (end_agent reads the transcript).
    #246 (reports#136 case 2, implementer-226-a listed running for days): orphan_min (minutes) also ends a row whose
    session is long gone but that the rule above never catches: one with no readable transcript that started over
    orphan_min ago, and one whose transcript has been silent that long even with an open tool call (the session was
    killed mid-call). Returns the ids."""
    from .rounds import open_calls
    at = at or datetime.now(timezone.utc)
    done = []
    for r in [dict(r) for r in conn.execute("SELECT * FROM agents WHERE ended='' ORDER BY id")]:
        idle = transcript_idle_min(r, at)
        if idle is None:
            try:
                age = (at - datetime.strptime(r["started"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)).total_seconds() / 60
            except (ValueError, TypeError):
                continue
            if orphan_min is None or age < orphan_min:
                continue
            why = "ended stale: no transcript, session gone"
            idle = age
        elif idle >= idle_min and not open_calls(r["transcript_path"]):
            why = "ended stale on resume"
        elif orphan_min is not None and idle >= orphan_min:
            why = "ended stale: transcript silent with a tool call open, session gone"
        else:
            continue
        if end_agent(conn, actor, r, why, ev={"result": why, "stale": True, "idle_min": int(idle)}):
            done.append(r["agent_id"])
    return done


def _workflow_run(path):
    """The Workflow run directory a transcript lives in ('.../subagents/workflows/<wf id>/'), else ''. Agents of one
    run share it; an Agent-tool spawn's transcript is not under workflows/, so its run cannot be told (#195)."""
    if not path:
        return ""
    p = os.path.normpath(path)
    return os.path.dirname(p) if "workflows" in p.split(os.sep)[:-1] else ""


def _idle_setting():
    """The no-activity minutes (aesir.toml [observe.aesir] agent_idle_min); the default when config cannot be read."""
    from . import db
    from .commands import status
    try:
        return status.load_config(db.project_root())["agent_idle_min"]
    except (AesirError, OSError, ValueError):
        return status.DEFAULTS["agent_idle_min"]


def supersede_earlier(conn, actor, new_id, role, task):
    """#195: a re-spin (a new implementer for the same task) ends the earlier bound running row of that role and
    task, marked 'superseded by <new id>'. Unbound rows are spawns still waiting for their bind (parallel spawns of
    one task must still bind in order); the unbound sweep ends those. Siblings (rows of the same Workflow run as the
    new row) work in parallel and are left alone. When either run cannot be told, the earlier row is superseded only
    once it has been quiet past the no-activity setting. Outside any transaction. Returns the ids."""
    if role != "implementer" or not task:
        return []
    me = conn.execute("SELECT transcript_path FROM agents WHERE agent_id=?", (new_id,)).fetchone()
    new_run = _workflow_run(me["transcript_path"] if me else "")
    idle_min = None
    done = []
    for r in [dict(r) for r in conn.execute(
            "SELECT * FROM agents WHERE ended='' AND harness_agent_id!='' AND task_id=? AND agent_id!=? ORDER BY id",
            (int(task), new_id))]:
        if agent_role(r) != role:  # #248: by type, not by id prefix (a free name like 'checker-261-a' is an implementer too)
            continue
        old_run = _workflow_run(r["transcript_path"])
        if new_run and old_run:
            if new_run == old_run:
                continue
        else:
            if idle_min is None:
                idle_min = _idle_setting()
            idle = transcript_idle_min(r)
            if idle is not None and idle < idle_min:  # still writing, and maybe a sibling: leave it running
                continue
        why = f"superseded by {new_id}"
        if end_agent(conn, actor, r, why, ev={"result": why, "superseded_by": new_id}):
            done.append(r["agent_id"])
    return done


def _end_cleanup(conn, actor, a, ok, start):
    """Fail open, loudly (D115): a cleanup problem must not undo or block the end."""
    from . import friction, messages, scratch
    for what, fn in (("steering notes not expired", lambda: messages.expire_notes(conn, "aesir-hook", a["agent_id"])),
                     ("scratch folder not cleaned", lambda: scratch.finish(
                         _project_root(a["worktree"] or start), a["agent_id"], ok))):
        try:
            fn()
        except (AesirError, OSError, sqlite3.Error, ValueError) as e:
            print(f"aesir: {what}: {e}", file=sys.stderr)
            try:
                with tx(conn):
                    friction.record(conn, actor, "hook_failure", f"{what}: {e}", agent=a["agent_id"])
            except sqlite3.Error as e2:
                print(f"aesir: friction not recorded: {e2}", file=sys.stderr)


def _project_root(start):
    from . import db
    return db.project_root(start or None)


def agent_end(conn, actor, role, agent_id, result="", start=None):
    row = conn.execute("SELECT * FROM agents WHERE role=? AND agent_id=? AND ended='' ORDER BY id DESC LIMIT 1",
                       (role, agent_id)).fetchone()
    if not row or not end_agent(conn, actor, dict(row), result, start=start):
        raise AesirError(f"no running agent {agent_id} for {role}")


def end_task_agents(conn, actor, tid, status):
    """#143: a task reached `status` (merged/verified/wontfix); its agents still running will never report (their
    workflow stopped, was superseded or killed). End them, with a friction note naming the reason. Returns ids."""
    from . import friction
    done = []
    for r in [dict(r) for r in conn.execute("SELECT * FROM agents WHERE task_id=? AND ended='' ORDER BY id",
                                            (int(tid),))]:
        why = f"auto-ended: task #{int(tid)} reached {status} while the agent was still running"
        if end_agent(conn, actor, r, why, ev={"auto": True, "reason": why}):
            with tx(conn):
                friction.record(conn, actor, "agent_timeout", why, agent=r["agent_id"], task=int(tid))
            done.append(r["agent_id"])
    return done


DEFAULT_MODELS = {"implementer": "sonnet", "reviewer": "sonnet", "runner": "haiku",
                  "researcher": "sonnet"}


def _letter_n(s):
    n = 0
    for ch in s:
        n = n * 26 + ord(ch) - 96
    return n


def _letters(n):
    out = ""
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(97 + r) + out
    return out


def spawn_agent(conn, actor, role, task, parent="lead", model=None, session="", agent_type="", tool_use_id="",
                strict=True, name=None):
    """Allocate the next <role>-<task>-<letter> id, record it as running under `parent`, return its facts.
    strict=False (hooks): task may be None or unknown, the id then uses 000."""
    with tx(conn):
        g = _spawn_locked(conn, actor, role, task, parent, model, session, agent_type, tool_use_id, strict, name)
    supersede_earlier(conn, actor, g["id"], role, task)
    return g


# The spelling of an agent id a spawn prompt may ask for (#237, #248, reports#153): '<prefix>-<task N>[-]<letters>'.
# Groups: prefix, task number, letters (optional). The prefix is free text (not tied to the agent type: 'checker-261'
# for a reviewer is fine; the row's role and type come from the agent type), and the dash before the letters is
# optional so a near miss like 'implementer-332a' still binds task 332 instead of silently allocating '-000-'.
AGENT_NAME_PAT = r"([a-z][a-z0-9_]*)-(\d+)(?:-?([a-z]+))?"
AGENT_NAME_RE = re.compile(AGENT_NAME_PAT)


def name_allowed(conn, name, atype, role=""):
    """The AGENT_NAME_RE match for a prompt-given id when it may name an agent of type `atype`, else None (#248). Any
    free prefix is fine for a known type, except a persistent role name or another type ('lead-261-a' for a reviewer):
    role and limits follow the type, and such a prefix would read as someone else. An agent of an unknown type
    (general-purpose) keeps its own role as the prefix, since the prefix is all that says what it is."""
    nm = AGENT_NAME_RE.fullmatch(name or "")
    if not nm:
        return None
    pre, own = nm.group(1), (atype if atype in AGENT_TYPES else role or atype)
    if pre == own or (atype in AGENT_TYPES and not reserved_prefix(conn, pre)):
        return nm
    return None


def _spawn_locked(conn, actor, role, task, parent, model, session, agent_type, tool_use_id, strict, name=None):
    """Caller holds the transaction. `name` (#207): the id a spawn prompt asked for; used whatever its prefix (#248) when
    well-formed, carrying this task's number (when there is one), and not taken, else the next <role>-<task>-<letter>
    is allocated. The row's role stays `role`: the name never decides it."""
    if not re.fullmatch(r"[a-z][a-z0-9_]*", role):
        raise AesirError(f"bad role name: {role} (lowercase letters, digits, underscore)")
    model = DEFAULT_MODELS.get(role, "opus") if model is None else model
    if strict:
        get_task(conn, task)
    prefix = f"{role}-{int(task or 0):03d}-"
    used = [_letter_n(r[0][len(prefix):]) for r in conn.execute(
        "SELECT agent_id FROM agents WHERE substr(agent_id, 1, ?) = ?", (len(prefix), prefix))
        if re.fullmatch(r"[a-z]+", r[0][len(prefix):])]
    agent_id = prefix + _letters(max(used, default=0) + 1)
    nm = name_allowed(conn, name, agent_type or role, role)  # well-formed, not a borrowed role/type prefix
    if nm and (not task or int(nm.group(2)) == int(task)) and not conn.execute(
            "SELECT 1 FROM agents WHERE agent_id=?", (name,)).fetchone():
        agent_id = name
    _insert_agent(conn, actor, parent, agent_id, task and int(task), model, session, agent_type, tool_use_id, strict)
    return {"id": agent_id, "role": role, "task": int(task or 0), "model": model, "parent": parent}


def _agent_started_task(conn, actor, agent_id, task_id):
    """#182: an agent row just got bound to a task. A task still open or claimed is now being worked, so move it to
    in_progress ('started by <agent-id>'); any other status is left alone (also makes a re-bind a no-op). Runs inside
    the bind transaction. The #176 predecessor warning is logged as for any move to in_progress."""
    if not task_id:
        return
    t = conn.execute("SELECT status FROM tasks WHERE id=?", (int(task_id),)).fetchone()
    if not t or t["status"] not in ("open", "claimed"):
        return
    conn.execute("UPDATE tasks SET status='in_progress', updated=? WHERE id=?", (now(), int(task_id)))
    event(conn, actor, "task", task_id, "set", {"status": {"from": t["status"], "to": "in_progress"},
                                                "reason": f"started by {agent_id}"})
    conn.execute("INSERT INTO task_log (task_id, ts, actor, text) VALUES (?,?,?,?)",
                 (int(task_id), now(), actor, f"status {t['status']} -> in_progress (started by {agent_id})"))
    from . import taskorder
    taskorder.start_warning(conn, actor, task_id, "in_progress")


def adopt_agent(conn, actor, role, task, harness_id, agent_type="", worktree="", model="", session="", transcript="",
                name=None):
    """A harness agent no PreToolUse(Agent) saw (a workflow agent): allocate an id, record and bind it in one
    transaction. The row's role is the agent's own (implementer/reviewer/runner/researcher), not the spawning session's (#163).
    Model is whatever is known at start (else empty, filled in at stop). `session` is the spawning Claude session id
    (what `role checkin` stored for the lead), so `resume` can list the agents of a role's session; `transcript` is the
    agent's transcript path, kept from bind time so rounds can read live tokens (#165). `name` is the id the spawn
    prompt gave (#207). Returns the row (the existing one if already bound)."""
    with tx(conn):
        a = find_agent(conn, harness_id)
        if a:
            return a
        g = _spawn_locked(conn, actor, role, task, role, model, "", agent_type, "", False, name)
        conn.execute("UPDATE agents SET harness_agent_id=?, bound=?, worktree=?, session=?, transcript_path=?"
                     " WHERE agent_id=? AND ended='' AND harness_agent_id=''",
                     (harness_id, now(), worktree, session, transcript, g["id"]))
        event(conn, actor, "agent", g["id"], "bind", {"harness_agent_id": harness_id, "worktree": worktree,
                                                      "source": "workflow"})
        _agent_started_task(conn, actor, g["id"], task)
    supersede_earlier(conn, actor, g["id"], role, task)
    return find_agent(conn, harness_id)


def bind_task(conn, actor, agent_id, task, source="manual"):
    """#207: give a running agent row that has no task (or, by hand, another one) its task: the row, the task's start
    (#182) and the agent's earlier messages (notes to it, its own replies) follow, so `task show` lists them. Logged
    on the task. Returns the row; None when the row already has that task."""
    task = get_task(conn, task)["id"]
    with tx(conn):
        a = conn.execute("SELECT * FROM agents WHERE agent_id=? AND ended='' ORDER BY id DESC LIMIT 1",
                         (agent_id,)).fetchone()
        if not a:
            raise AesirError(f"no running agent {agent_id} (see: agent list --running)")
        if a["task_id"] == task:
            return None
        conn.execute("UPDATE agents SET task_id=? WHERE id=?", (task, a["id"]))
        conn.execute("UPDATE messages SET task_id=? WHERE task_id IS NULL AND (recipient=? OR sender=?)",
                     (task, agent_id, agent_id))
        event(conn, actor, "agent", agent_id, "task_bind", {"task": task, "was": a["task_id"], "source": source})
        conn.execute("INSERT INTO task_log (task_id, ts, actor, text) VALUES (?,?,?,?)",
                     (task, now(), actor, f"agent {agent_id} bound to this task ({source})"))
        _agent_started_task(conn, actor, agent_id, task)
    return _row(conn.execute("SELECT * FROM agents WHERE id=?", (a["id"],)).fetchone())


def rename_agent(conn, actor, agent_id, new, root=None):
    """#237: give a running agent row the name its prompt asked for, after the row was created without it. Refused
    (None) when `new` is not a well-formed name (any prefix, #248), is taken by any row, or the old id is already in use: a
    message to or from it, an event it acted in (the agent was told its id and used it), or, when `root` is given,
    its scratch folder exists (the agent may have been told that path, and a rename would orphan it). Earlier events keep the
    old id as history; a 'rename' event records the change. Returns the renamed row."""
    with tx(conn):
        a = conn.execute("SELECT * FROM agents WHERE agent_id=? AND ended='' ORDER BY id DESC LIMIT 1",
                         (agent_id,)).fetchone()
        nm = a and name_allowed(conn, new, agent_role(a), agent_role(a))
        if not a or not nm:
            return None
        from . import scratch
        if root and scratch.path(root, agent_id).exists():
            return None
        if (conn.execute("SELECT 1 FROM agents WHERE agent_id=?", (new,)).fetchone()
                or conn.execute("SELECT 1 FROM messages WHERE sender=? OR recipient=?", (agent_id, agent_id)).fetchone()
                or conn.execute("SELECT 1 FROM events WHERE agent_id=? OR actor=?", (agent_id, agent_id)).fetchone()):
            return None
        conn.execute("UPDATE agents SET agent_id=? WHERE id=?", (new, a["id"]))
        event(conn, actor, "agent", new, "rename", {"from": agent_id, "to": new})
    return _row(conn.execute("SELECT * FROM agents WHERE id=?", (a["id"],)).fetchone())


def resolve_agent(conn, ref):
    """The running agent a `note` target names (#207): an agent id, or a task ('#207', '207', 'task/207-slug') for the
    one running agent bound to that task. Several on the task: refused, listing the candidate ids."""
    ref = (ref or "").strip()
    if conn.execute("SELECT 1 FROM agents WHERE agent_id=? AND ended=''", (ref,)).fetchone():
        return ref
    m = re.fullmatch(r"#?(\d+)|task/(\d+)(?:-.*)?", ref)
    if not m:
        return ref  # the caller reports the unknown id
    tid = int(m.group(1) or m.group(2))
    ids = [r[0] for r in conn.execute("SELECT agent_id FROM agents WHERE task_id=? AND ended='' ORDER BY id", (tid,))]
    if not ids:
        raise AesirError(f"no running agent on task #{tid} (see: agent list --running)")
    if len(ids) > 1:
        raise AesirError(f"{len(ids)} running agents on task #{tid}: {', '.join(ids)}; name one")
    return ids[0]


def reopen_agent(conn, actor, harness_id):
    """Activity from a harness agent whose row already ended (SendMessage resumed it): start a fresh running row
    for the same instance id, so the next stop ends it. Returns the running row (None if unknown)."""
    with tx(conn):
        a = find_agent(conn, harness_id)
        if not a or not a["ended"]:
            return a
        conn.execute("INSERT INTO agents (role, agent_id, task_id, model, session, started, agent_type, tool_use_id,"
                     " harness_agent_id, bound, worktree, transcript_path) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (a["role"], a["agent_id"], a["task_id"], a["model"], a["session"], now(), a["agent_type"], "",
                      harness_id, now(), a["worktree"], a["transcript_path"]))
        event(conn, actor, "agent", a["agent_id"], "resume", {"role": a["role"], "harness_agent_id": harness_id})
    return find_agent(conn, harness_id)


def parse_duration(v):
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*", str(v))
    if not m:
        raise AesirError(f"bad duration: {v} (e.g. 90s, 30m, 2h, 1d)")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def end_stale_agents(conn, actor, older_than, result="stale: ended by sweep"):
    """End every running agent row started more than `older_than` (a duration like 2h) ago. Returns the ids."""
    cutoff = datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() - parse_duration(older_than),
                                    timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = [dict(r) for r in conn.execute("SELECT * FROM agents WHERE ended='' AND started<? ORDER BY id", (cutoff,))]
    return [r["agent_id"] for r in rows if end_agent(conn, actor, r, result, ev={"result": result, "stale": True})]


def find_agent(conn, harness_id):
    return _row(conn.execute("SELECT * FROM agents WHERE harness_agent_id=? ORDER BY id DESC LIMIT 1",
                             (harness_id,)).fetchone())


def bind_agent(conn, actor, agent_id, harness_id, worktree="", model="", transcript=""):
    """Tie the harness's agent id (and worktree) to the pending instance. Returns the row, or None if no such pending."""
    with tx(conn):
        cur = conn.execute("UPDATE agents SET harness_agent_id=?, bound=?, worktree=?,"
                           " model=CASE WHEN ?!='' THEN ? ELSE model END,"
                           " transcript_path=CASE WHEN ?!='' THEN ? ELSE transcript_path END"
                           " WHERE agent_id=? AND ended='' AND harness_agent_id=''",
                           (harness_id, now(), worktree, model, model, transcript, transcript, agent_id))
        if not cur.rowcount:
            return None
        event(conn, actor, "agent", agent_id, "bind", {"harness_agent_id": harness_id, "worktree": worktree})
        r = conn.execute("SELECT task_id FROM agents WHERE agent_id=? AND harness_agent_id=?",
                         (agent_id, harness_id)).fetchone()
        _agent_started_task(conn, actor, agent_id, r and r["task_id"])
    return _row(conn.execute("SELECT * FROM agents WHERE agent_id=? AND harness_agent_id=?",
                             (agent_id, harness_id)).fetchone())


def pending_agents(conn, agent_type=None, within=120):
    """Allocated but not yet bound instances, newest last (optionally one agent type, recent only)."""
    cutoff = datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() - within, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    q, a = "SELECT * FROM agents WHERE ended='' AND harness_agent_id='' AND tool_use_id!='' AND started>=?", [cutoff]
    if agent_type is not None:
        q += " AND agent_type=?"
        a.append(agent_type)
    return [dict(r) for r in conn.execute(q + " ORDER BY id", a)]


def stop_agent(conn, actor, harness_id, transcript_path="", tokens=None, result="", model="", start=None):
    """SubagentStop: mark the bound instance ended; store transcript path and token sums."""
    tokens = tokens or {}
    a = find_agent(conn, harness_id)
    if not a or a["ended"]:
        return None
    # a resumed agent's transcript is cumulative: count only what its earlier rows did not
    prior = _prior_tokens(conn, harness_id, a["id"])
    tokens = {k: max(0, tokens.get(k, 0) - p)
              for k, p in zip(("input", "output", "cache_read", "cache_create"), prior)}
    from . import scratch
    cols = {"transcript_path": transcript_path, "tokens_in": tokens.get("input", 0),
            "tokens_out": tokens.get("output", 0), "tokens_cache_read": tokens.get("cache_read", 0),
            "tokens_cache_create": tokens.get("cache_create", 0)}
    if not a["model"]:
        cols["model"] = model
    if not end_agent(conn, actor, a, a["result"] or (result or "")[:500], scratch.succeeded(result), start, cols,
                     {"tokens": tokens}):
        return None
    return a


def list_agents(conn, task=None, role=None, running=False):
    q, a = "SELECT * FROM agents WHERE 1=1", []
    if task is not None:
        q += " AND task_id=?"
        a.append(int(task))
    if role:
        # #248: the id prefix counts only for a row with no known type
        q += (" AND (role=? OR agent_type=? OR (agent_type NOT IN ('implementer','reviewer','runner','researcher')"
              " AND agent_id LIKE ?))")
        a += [role, role, role + "-%"]
    if running:
        q += " AND ended=''"
    return [dict(r) for r in conn.execute(q + " ORDER BY id", a)]


def show_agent(conn, agent_id):
    """Newest row for the id (a restarted id has several), plus every event it made or was the subject of."""
    a = _row(conn.execute("SELECT * FROM agents WHERE agent_id=? ORDER BY id DESC LIMIT 1", (agent_id,)).fetchone())
    if not a:
        raise AesirError(f"no such agent: {agent_id}")
    a["tokens"] = {k: a.pop("tokens_" + k) for k in ("in", "out", "cache_read", "cache_create")}
    a["events"] = [dict(r) for r in conn.execute(
        "SELECT id, ts, actor, entity, entity_id, action, payload FROM events"
        " WHERE agent_id=? OR (entity='agent' AND entity_id=?) ORDER BY id", (agent_id, agent_id))]
    return a


def agent_for_worktree(conn, path):
    """Newest bound instance whose worktree contains `path` (the git trailer uses this)."""
    norm = lambda p: os.path.normcase(os.path.realpath(p))
    path = norm(path)
    for r in conn.execute("SELECT * FROM agents WHERE worktree!='' AND harness_agent_id!='' ORDER BY id DESC"):
        w = norm(r["worktree"])
        if path == w or path.startswith(w + os.sep):
            return dict(r)
    return None


def resume(conn, role, notes=5, log_lines=2):
    r = show_role(conn, role)
    r["notes"] = r["notes"][-notes:]
    mine = [dict(t) for t in conn.execute(
        f"SELECT id, title, status, owner, requester FROM tasks WHERE owner=? AND status IN "
        f"({','.join('?' * len(ACTIVE))}) ORDER BY id", (role, *ACTIVE))]
    to_verify = [dict(t) for t in conn.execute(
        "SELECT id, title, status, owner, requester FROM tasks WHERE requester=? AND status='merged' ORDER BY id",
        (role,))]
    from . import taskorder
    wait = taskorder.waiting(conn)
    for t in mine + to_verify:
        t["waits"] = wait.get(t["id"], "")
        t["latest_log"] = [dict(x) for x in conn.execute(
            "SELECT ts, actor, text FROM task_log WHERE task_id=? ORDER BY id DESC LIMIT ?",
            (t["id"], log_lines))][::-1]
    if r["previous_checkin"]:  # the checkin before this session's: run checkin, then resume
        decs = conn.execute("SELECT id, date, title, decided_by FROM decisions WHERE ts > ? ORDER BY id",
                            (r["previous_checkin"],))
    else:
        decs = conn.execute("SELECT id, date, title, decided_by FROM decisions ORDER BY id DESC LIMIT 10")
    r["tasks"], r["awaiting_verification"], r["new_decisions"] = mine, to_verify, [dict(d) for d in decs]
    # a Workflow agent's row holds its own role (#163), so also list the agents spawned from this role's session (#165)
    r["agents"] = [dict(a) for a in conn.execute(
        "SELECT agent_id, task_id, model, started FROM agents WHERE ended='' AND (role=? OR (session!='' AND session="
        "(SELECT session FROM roles WHERE role=?))) ORDER BY started, id", (role, role))]
    r["core"] = show_core(conn, role)["rows"]
    r["priorities"] = space_list(conn, role, "priorities")
    # proposals: the advisor sees everything awaiting the owner; any role sees the ones it created
    cols = "id, role, section, key, text, reason, status, proposed_by, decided_by, decision_note"
    props = [dict(x) for x in conn.execute(f"SELECT {cols} FROM core_proposals WHERE proposed_by=? "
                                           "ORDER BY id DESC LIMIT 10", (role,))][::-1]
    if role == "advisor":
        seen = {x["id"] for x in props}
        props += [dict(x) for x in conn.execute(f"SELECT {cols} FROM core_proposals WHERE status='pending' ORDER BY id")
                  if x["id"] not in seen]
        props.sort(key=lambda x: x["id"])
    r["core_proposals"] = props
    if role == "advisor":  # everything waiting on the owner or on the advisor's verification
        r["owner_pending"] = {
            "proposals": [x for x in props if x["status"] == "pending"],
            "escalated": [dict(t) for t in conn.execute(
                "SELECT id, title, status, owner, requester FROM tasks WHERE status='escalated' ORDER BY id")],
            "to_verify": to_verify}
    return r


# ---- identity core ----------------------------------------------------------
# Rows are (role, section, key). rule 'owner': change needs recorded owner approval.
# rule 'lead': the role itself (lead, advisor, ...) changes it directly with a reason; a decision is logged.
# The stored value stays 'lead' (schema CHECK, no migration) but means "self-adopted by the owning role".

SCOPES = ("owner", "role", "project")  # core row scope: how this owner works / any lead or advisor / this project
CORE_SECTIONS = {"purpose": "owner", "decision_rights": "owner", "commitments": "lead", "conventions": "lead"}

_LEAD_SEED = [
    ("purpose", "role", "owner",
     "Persistent role (Opus); sessions are replaceable. Breaks down work, spawns subagents, merges, keeps the design coherent."),
    ("purpose", "notes", "owner",
     "Does not implement except small contract edits. Implementers never merge; merging is a lead function via the merge queue. Does not route work through the owner."),
    ("decision_rights", "decides-alone", "owner",
     "Task breakdown, assignment, contracts between modules, merge order, when to escalate."),
    ("decision_rights", "escalates", "owner",
     "Owner-only decisions; Fable calls past budget."),
    ("decision_rights", "owner-only-design", "owner",
     "Game/product design intent and feel; anything player- or user-facing that changes the experience."),
    ("decision_rights", "owner-only-architecture", "owner",
     "Irreversible architecture: module/crate boundaries, persistence and network models, the simulation model."),
    ("decision_rights", "owner-only-outward", "owner",
     "Outward-facing actions: pushes to shared remotes, releases, publishing, anything costing money beyond the set budget."),
    ("decision_rights", "owner-only-charter", "owner", "Changes to the charter."),
    ("decision_rights", "reversible", "owner", "All other reversible decisions: team decides, then logs; owner can veto."),
    ("decision_rights", "owner-input-tags", "owner",
     "Owner input confidence: firm = push back once with reasons, then follow; lean = follow unless a better-argued "
     "option exists, say why; spitballing = team expertise wins; untagged = lean."),
    ("commitments", "state-in-aesir", "lead", "State lives in Aesir, not in sessions (charter s2.3)."),
    ("commitments", "requester-verifies", "lead", "The requester verifies, not the implementer (charter s5)."),
    ("commitments", "one-owner", "lead",
     "One owner per thing; duplication is a review failure (charter s2.6)."),
    ("commitments", "main-green", "lead", "Main is always green (charter s6)."),
    ("conventions", "owner-via-advisor", "lead",
     "Owner questions go through the advisor, with a recommendation; the owner does not read the lead session."),
    ("conventions", "default-subagents", "lead",
     "Default to subagents: runner (Haiku) for gates/tests/checks, reviewer (Sonnet) before merge, implementer "
     "(Sonnet) to build; lead's Opus pass only for contract or architecture changes."),
    ("conventions", "reversible-decisions", "lead",
     "Decide reversible things yourself and log them; only charter s4 owner-only decisions go to the owner."),
    ("conventions", "owner-questions", "lead",
     "A question from the owner is usually them trying to understand: answer and explain, don't change course. Owner input is a lean unless tagged firm."),
    ("conventions", "owner-ideas", "lead", "Weigh owner ideas on merit; say plainly when one seems wrong and why."),
    ("conventions", "plain-terms", "lead", "Explain in plain terms; define jargon the first time it appears."),
    ("conventions", "docs-style", "lead", "Keep docs terse. No em dashes; use -- or rewrite the sentence."),
    ("conventions", "comms-find", "lead", "Find the other role with ListAgents; address peers as '<project>-<role>' (e.g. tetris-lead) in client projects; Aesir itself uses 'lead'/'advisor'."),
    ("conventions", "comms-subject", "lead",
     "First line of every message is a self-contained subject. Refer to tasks as #NNN."),
    ("conventions", "comms-task", "lead",
     "Cite the task (#NNN) in every substantive message: the hook records SendMessage to the db (shown in `thread N`), "
     "and ask/reply print a notify line to send. The db is the record; transcripts are not."),
    ("conventions", "comms-durable", "lead",
     "Messages are conversation; anything durable goes in the database (state/ until it is migrated)."),
    ("conventions", "comms-notify", "lead",
     "To message a peer once it is free, send with notify_when_idle: true and act on the idle notice; don't poll ListAgents."),
]


_ADVISOR_SEED = [
    ("purpose", "role", "owner",
     "Persistent role (Opus). The owner's proxy, not the lead's assistant: keeps the owner off the critical path."),
    ("purpose", "notes", "owner",
     "Turns owner intent into tasks with acceptance specs, helps the owner understand what is happening, verifies "
     "results against what the owner meant. In a game project also plays the build and files behaviour issues."),
    ("decision_rights", "decides-alone", "owner",
     "How to read the owner's intent; acceptance specs; whether a result matches intent."),
    ("decision_rights", "escalates", "owner",
     "Real ambiguity in intent, asked one decision at a time with a recommendation."),
    ("decision_rights", "owner-only-charter", "owner", "Changes to the charter."),
    ("decision_rights", "owner-input-tags", "owner",
     "Owner input confidence: firm = push back once with reasons, then follow; lean = follow unless a better-argued "
     "option exists, say why; spitballing = team expertise wins; untagged = lean."),
    ("decision_rights", "owner-decisions-relayed", "owner",
     "Owner decisions reach the system only as the owner's own words relayed by the advisor (--as advisor --for owner --quote)."),
    ("commitments", "state-in-aesir", "lead", "State lives in Aesir, not in sessions (charter s2.3)."),
    ("commitments", "requester-verifies", "lead", "The advisor verifies the tasks it requested, against the spec (charter s5)."),
    ("conventions", "owner-only-advisor", "lead",
     "The owner talks only to the advisor; the lead and subagents do not route work through the owner."),
    ("conventions", "relay-verbatim", "lead",
     "Relay owner words verbatim, in quotes, when recording an owner decision."),
    ("conventions", "owner-confidence-tags", "lead",
     "Record owner input with a confidence tag (firm, lean, spitballing); untagged is lean."),
    ("conventions", "plain-terms", "lead", "Explain to the owner in plain terms; define jargon the first time it appears."),
    ("conventions", "batch-owner-questions", "lead",
     "One owner decision per message, each with a recommendation; wait for the answer before the next."),
    ("conventions", "verify-as-requester", "lead",
     "As requester, verify merged tasks against the owner's intent and the spec, then set verified. Read every "
     "evidence log first (task show lists the paths); [OK] means the command ran, not that the criterion is met."),
    ("conventions", "record-lessons", "lead",
     "When a discussion turns up a lesson not already captured by a task, decision or report, record it: "
     "lesson add --scope aesir|project \"<what happened>\" --lesson \"<lesson>\". At project close run lesson review "
     "with the owner, then lesson send."),
]

_SEEDS = {"lead": _LEAD_SEED, "advisor": _ADVISOR_SEED}


def _slug(text):
    return "-".join(re.findall(r"[a-z0-9]+", text.lower())[:5]) or "item"


def _core_rule(conn, role, section, key):
    if section not in CORE_SECTIONS:
        raise AesirError(f"unknown core section: {section} (use {', '.join(CORE_SECTIONS)})")
    row = conn.execute("SELECT rule FROM core WHERE role=? AND section=? AND key=?", (role, section, key)).fetchone()
    return row["rule"] if row else CORE_SECTIONS[section]


def show_core(conn, role):
    return {"role": role,
            "rows": [dict(r) for r in conn.execute(
                "SELECT id, section, key, text, rule, scope, updated, updated_by FROM core WHERE role=? ORDER BY id", (role,))],
            "pending": [dict(r) for r in conn.execute(
                "SELECT id, section, key, text, reason, proposed_by, ts FROM core_proposals"
                " WHERE role=? AND status='pending' ORDER BY id", (role,))]}


def _core_title(conn, base):
    """Decision titles are unique per day: suffix on collision."""
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    title, n = base, 1
    while conn.execute("SELECT 1 FROM decisions WHERE date=? AND title=?", (date, title)).fetchone():
        n += 1
        title = f"{base} ({n})"
    return title


def _apply_core(conn, actor, role, section, key, text, reason, rule, decided_by, relay=None, scope=None):
    """Write the row, add the linked decision and the event. Caller holds the transaction.
    scope None: a new row is 'project', an existing row keeps its scope."""
    conn.execute(
        "INSERT INTO core (role, section, key, text, rule, scope, updated, updated_by) VALUES (?,?,?,?,?,?,?,?)"
        " ON CONFLICT(role, section, key) DO UPDATE SET text=excluded.text, updated=excluded.updated,"
        " updated_by=excluded.updated_by, scope=COALESCE(?, scope)",
        (role, section, key, text, rule, scope or "project", now(), actor, scope))
    row = conn.execute("SELECT id FROM core WHERE role=? AND section=? AND key=?", (role, section, key)).fetchone()
    title = _core_title(conn, f"Core change: {role}/{section}/{key}")
    did = add_decision(conn, actor, title, context=reason + (f" | owner said: {relay['quote']}" if relay and relay["quote"] else ""),
                       choice=text, decided_by=decided_by, reversible="yes", _inner=True)
    conn.execute("INSERT OR IGNORE INTO links VALUES ('decision',?,'core',?)", (str(did), str(row["id"])))
    event(conn, actor, "core", row["id"], "change",
          {"role": role, "section": section, "key": key, "rule": rule, "decision": did, "reason": reason} | (relay or {}))
    return did


def set_core(conn, actor, role, section, text, reason, key="", scope=None):
    """Direct change to a lead-rule row (or a new row in a lead-rule section). Reason required."""
    if not (reason or "").strip():
        raise AesirError("core set requires --reason")
    if not text.strip():
        raise AesirError("text is empty")
    key = key or _slug(text)
    with tx(conn):
        if _core_rule(conn, role, section, key) == "owner":
            raise AesirError("owner-rule row: use `core propose`; it needs owner approval")
        _apply_core(conn, actor, role, section, key, text, reason, "lead", actor, scope=scope)
    return key


def rm_core(conn, actor, role, section, key, reason):
    """Delete a lead-rule core row (the row goes; events and the decision keep the history)."""
    if not (reason or "").strip():
        raise AesirError("core rm requires --reason")
    with tx(conn):
        row = _row(conn.execute("SELECT * FROM core WHERE role=? AND section=? AND key=?",
                                (role, section, key)).fetchone())
        if not row:
            raise AesirError(f"no such core row: {role}/{section}/{key}")
        if row["rule"] == "owner":
            raise AesirError("owner-rule row: cannot rm; use `core propose` (needs owner approval)")
        conn.execute("DELETE FROM core WHERE id=?", (row["id"],))
        title = _core_title(conn, f"Core removal: {role}/{section}/{key}")
        did = add_decision(conn, actor, title, context=reason, choice=f"removed: {row['text']}",
                           decided_by=actor, reversible="yes", _inner=True)
        event(conn, actor, "core", row["id"], "rm",
              {"role": role, "section": section, "key": key, "text": row["text"], "decision": did, "reason": reason})
    return did


def propose_core(conn, actor, role, section, text, reason, key="", scope=None):
    if not (reason or "").strip():
        raise AesirError("core propose requires --reason")
    if not text.strip():
        raise AesirError("text is empty")
    key = key or _slug(text)
    with tx(conn):
        if scope != "owner" and _core_rule(conn, role, section, key) != "owner":  # owner-scope rows always need approval
            raise AesirError("lead-rule row: use `core set --reason`; no proposal needed")
        cur = conn.execute(
            "INSERT INTO core_proposals (role, section, key, text, reason, proposed_by, ts, scope)"
            " VALUES (?,?,?,?,?,?,?,?)", (role, section, key, text, reason, actor, now(), scope or "project"))
        event(conn, actor, "core_proposal", cur.lastrowid, "propose",
              {"role": role, "section": section, "key": key, "reason": reason})
    return cur.lastrowid


def _owner_authority(actor, for_, quote, verb):
    """Return (decided_by, relay payload). Owner acts directly; advisor relays the owner's decision."""
    if actor == "owner":
        return "owner", {"quote": quote or ""} if quote else {}
    if actor == "advisor":
        if for_ != "owner":
            raise AesirError(f"{verb} by advisor requires --for owner")
        if not (quote or "").strip():
            raise AesirError(f"{verb} by advisor requires a non-empty --quote (the owner's words or a reference)")
        return "owner (relayed by advisor)", {"relayed_by": "advisor", "for": "owner", "quote": quote}
    raise AesirError(f"only the owner, or the advisor relaying the owner (--as advisor --for owner --quote ...), "
                     f"can {verb} core proposals; caller is {actor}")


def _pending(conn, pid):
    p = conn.execute("SELECT * FROM core_proposals WHERE id=?", (int(pid),)).fetchone()
    if not p:
        raise AesirError(f"no such proposal: {pid}")
    if p["status"] != "pending":
        raise AesirError(f"proposal {pid} is already {p['status']}")
    return p


def approve_core(conn, actor, pid, for_=None, quote=""):
    """Apply a pending proposal. Honour-system: owner directly, or advisor relaying the owner with a quote."""
    decided_by, relay = _owner_authority(actor, for_, quote, "approve")
    with tx(conn):
        p = _pending(conn, pid)
        _apply_core(conn, actor, p["role"], p["section"], p["key"], p["text"], p["reason"], "owner", decided_by,
                    relay, scope=p["scope"])
        conn.execute("UPDATE core_proposals SET status='approved', decided_by=?, decided_ts=?, decision_note=?"
                     " WHERE id=?", (decided_by, now(), quote or "", p["id"]))
        event(conn, actor, "core_proposal", p["id"], "approve", relay)
        row = conn.execute("SELECT id FROM core WHERE role=? AND section=? AND key=?",
                           (p["role"], p["section"], p["key"])).fetchone()
        for r in conn.execute("SELECT a_id FROM links WHERE a_type='tip' AND b_type='core_proposal' AND b_id=?",
                              (str(p["id"]),)).fetchall():  # tips promoted through this proposal retire on approval
            _retire_tip(conn, actor, int(r["a_id"]), "core", row["id"])


def reject_core(conn, actor, pid, reason, for_=None, quote=""):
    decided_by, relay = _owner_authority(actor, for_, quote, "reject")
    if not (reason or "").strip():
        raise AesirError("core reject requires --reason")
    with tx(conn):
        p = _pending(conn, pid)
        conn.execute("UPDATE core_proposals SET status='rejected', decided_by=?, decided_ts=?, decision_note=?"
                     " WHERE id=?", (decided_by, now(), reason, p["id"]))
        event(conn, actor, "core_proposal", p["id"], "reject", {"reason": reason} | relay)


def seed_core(conn, actor, role):
    """Fill a role's core from charter s3/s4 (owner rule) and working rules (self rule). Idempotent."""
    if role not in _SEEDS:
        raise AesirError(f"only `core seed {'|'.join(_SEEDS)}` is defined")
    added = 0
    with tx(conn):
        for section, key, rule, text in _SEEDS[role]:
            cur = conn.execute(
                "INSERT OR IGNORE INTO core (role, section, key, text, rule, updated, updated_by) VALUES (?,?,?,?,?,?,?)",
                (role, section, key, text, rule, now(), actor))
            if cur.rowcount:
                added += 1
                event(conn, actor, "core", cur.lastrowid, "seed", {"role": role, "section": section, "key": key})
    return added


# ---- own space ---------------------------------------------------------------

def space_put(conn, actor, role, kind, key, body):
    with tx(conn):
        conn.execute("INSERT INTO space (role, kind, key, body, ts) VALUES (?,?,?,?,?)"
                     " ON CONFLICT(role, kind, key) DO UPDATE SET body=excluded.body, ts=excluded.ts",
                     (role, kind, key, json.dumps(body, ensure_ascii=False), now()))
        event(conn, actor, "space", f"{role}/{kind}/{key}", "put", {})


def _space(r):
    d = dict(r)
    d["body"] = json.loads(d["body"])
    return d


def space_get(conn, role, kind, key):
    r = conn.execute("SELECT * FROM space WHERE role=? AND kind=? AND key=?", (role, kind, key)).fetchone()
    if not r:
        raise AesirError(f"no such space entry: {role}/{kind}/{key}")
    return _space(r)


def space_list(conn, role, kind=None):
    q, a = "SELECT * FROM space WHERE role=?", [role]
    if kind:
        q += " AND kind=?"
        a.append(kind)
    return [_space(r) for r in conn.execute(q + " ORDER BY kind, key", a)]


def space_rm(conn, actor, role, kind, key):
    with tx(conn):
        if not conn.execute("DELETE FROM space WHERE role=? AND kind=? AND key=?", (role, kind, key)).rowcount:
            raise AesirError(f"no such space entry: {role}/{kind}/{key}")
        event(conn, actor, "space", f"{role}/{kind}/{key}", "rm", {})
