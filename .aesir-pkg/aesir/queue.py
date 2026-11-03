"""Merge queue: serialised merge commit in a detached worktree, scope check, full gate, ff main, state export, push, cleanup."""
import os
import re
import time
from pathlib import Path

from . import proc
from . import backup, friction, gates, invoke, skew, tracks
from . import service as S
from .db import event, now, tx
from .rolenames import agent_role
from .service import AesirError

MAIN = "main"
STALE_SECS = 2 * 3600
TAIL = 1500


# Safe to retry: they change nothing, so a second run cannot double-apply anything.
READ_ONLY = ("rev-parse", "status", "remote", "rev-list")


def _git(cwd, *args, check=True):
    """Run git. A read-only call that fails with no output at all (the child died before printing, e.g. the OS failed
    to start it under load, #161) is retried once; the error carries the return code."""
    def go():
        return proc.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
                        errors="replace")
    r = go()
    retried = False
    if r.returncode and args and args[0] in READ_ONLY and not (r.stderr or r.stdout).strip():
        r, retried = go(), True
    if check and r.returncode:
        again = " after retry" if retried else ""
        raise AesirError(f"git {' '.join(args)} failed{again} (rc {r.returncode}): {(r.stderr or r.stdout).strip()}")
    return r


def _lead(actor):
    if actor != "lead":
        raise AesirError(f"only lead can use the merge queue (caller is {actor})")


def _runner(conn, actor):
    """True when actor is a verified, running agent of type runner (the CLI downgrades unverified ids to
    'unverified:...'). The type decides, not the id: a runner may carry a free name (#248, reports#153)."""
    row = conn.execute("SELECT agent_id, agent_type FROM agents WHERE agent_id=? AND ended=''",
                       (actor or "",)).fetchone()
    return bool(row) and agent_role(row) == "runner"


def add(conn, actor, tid):
    _lead(actor)
    t = S.get_task(conn, tid)
    if t["status"] != "approved":
        raise AesirError(f"task {t['id']} is {t['status']}; queue needs approved")
    if not t["branch"]:
        raise AesirError(f"task {t['id']} has no branch set")
    with tx(conn):
        if conn.execute("SELECT 1 FROM merge_queue WHERE task_id=?", (t["id"],)).fetchone():
            raise AesirError(f"task {t['id']} is already queued")
        conn.execute("INSERT INTO merge_queue (task_id, added, actor) VALUES (?,?,?)", (t["id"], now(), actor))
        event(conn, actor, "queue", t["id"], "add", {})


def remove(conn, actor, tid):
    """Take a queued task out of the queue; its status is untouched (a held approved task stays approved)."""
    _lead(actor)
    t = S.get_task(conn, tid)
    with tx(conn):
        if not conn.execute("DELETE FROM merge_queue WHERE task_id=?", (t["id"],)).rowcount:
            raise AesirError(f"task {t['id']} is not queued")
    S.log_task(conn, actor, t["id"], f"removed from merge queue (status stays {t['status']})")


def items(conn):
    return [dict(r) for r in conn.execute(
        "SELECT q.seq, q.task_id, q.added, t.status, t.branch, t.title FROM merge_queue q"
        " JOIN tasks t ON t.id=q.task_id ORDER BY q.seq")]


# ---- lock -------------------------------------------------------------------

_pid_alive = gates.pid_alive


def acquire(conn, actor, stale=STALE_SECS):
    """Take the single queue lock; recover it when its holder is dead or it is older than `stale` seconds."""
    with tx(conn):
        row = conn.execute("SELECT pid, taken FROM queue_lock WHERE id=1").fetchone()
        if row:
            age = time.time() - row["taken"]
            if age < stale and _pid_alive(row["pid"]):
                raise AesirError(f"queue is locked by pid {row['pid']} ({int(age)}s ago)")
            event(conn, actor, "queue", "lock", "stale_recovered", {"pid": row["pid"], "age": int(age)})
            conn.execute("DELETE FROM queue_lock WHERE id=1")
        conn.execute("INSERT INTO queue_lock (id, pid, taken) VALUES (1, ?, ?)", (os.getpid(), time.time()))


def release(conn):
    conn.execute("DELETE FROM queue_lock WHERE id=1 AND pid=?", (os.getpid(),))


# ---- helpers ----------------------------------------------------------------

in_scope = S.in_scope  # shared matcher lives in service


def _worktree_of(root, branch):
    cur = None
    for ln in _git(root, "worktree", "list", "--porcelain").stdout.splitlines():
        if ln.startswith("worktree "):
            cur = ln[9:].strip()
        elif ln.strip() == f"branch refs/heads/{branch}":
            return Path(cur)
    return None


def _drop(conn, tid):
    with tx(conn):
        conn.execute("DELETE FROM merge_queue WHERE task_id=?", (tid,))


def _fail(conn, actor, t, reason, detail=""):
    with tx(conn):
        friction.queue_failure(conn, actor, t["id"], reason)
    S.set_task(conn, actor, t["id"], status="changes_requested", reason=reason[:200],
               attempts=t["attempts"] + 1)
    if detail.strip():
        S.log_task(conn, actor, t["id"], f"queue output:\n{detail.strip()[-TAIL:]}")
    _drop(conn, t["id"])
    return {"task": t["id"], "result": "changes_requested", "reason": reason}


def _head_mover(root):
    """#168: ` (HEAD moved <time>: <reflog message>)` for the newest HEAD reflog entry, so a refusal says what
    moved the shared checkout (a subagent's checkout, say). Empty when the reflog is unreadable."""
    r = _git(root, "reflog", "-1", "--date=iso", "--format=%gd | %gs", "HEAD", check=False)
    line = r.stdout.strip() if not r.returncode else ""
    m = re.match(r"HEAD@\{(.+?)\} \| (.+)", line)
    return f" (HEAD moved {m.group(1)}: {m.group(2)})" if m else ""


def _check_main_clean(root):
    on = _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if on != MAIN:
        raise AesirError(f"main checkout is not on {MAIN}: it is on {on}{_head_mover(root)}")
    if _git(root, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        raise AesirError("main checkout is not clean; commit or stash first")


def _evidence_mismatch(conn, t, head):
    """Error text when the task has evidence but none at `head` (commits added after review), else None."""
    rows = conn.execute("SELECT sha FROM evidence WHERE task_id=? AND sha!='' ORDER BY id", (t["id"],)).fetchall()
    if rows and head not in {r["sha"] for r in rows}:
        return (f"branch head {head[:8]} is not the sha its evidence was recorded at ({rows[-1]['sha'][:8]}); "
                "commits added after review are not merged unreviewed")
    return None


def _merge_commit(root, tmp, t, main_sha, sha):
    """Detached worktree at main plus a merge commit of `sha`: (sha, None, "") or (None, conflict files, git output)."""
    if tmp.exists():
        _git(root, "worktree", "remove", "--force", str(tmp), check=False)
    _git(root, "worktree", "prune", check=False)
    tmp.parent.mkdir(parents=True, exist_ok=True)
    _git(root, "worktree", "add", "--detach", str(tmp), main_sha)
    r = _git(tmp, "merge", "--no-ff", "-m", f"{t['title']} (#{t['id']})", sha, check=False)
    if r.returncode:
        files = _git(tmp, "diff", "--name-only", "--diff-filter=U", check=False).stdout.split()
        _git(tmp, "merge", "--abort", check=False)
        return None, (", ".join(files[:10]) or (r.stdout + r.stderr).strip()[-TAIL:]), r.stdout + r.stderr
    return _git(tmp, "rev-parse", "HEAD").stdout.strip(), None, ""


def _remove_worktree(conn, actor, root, t, wt, notes=None):
    """Remove the merged task's own worktree (#164) only when it is clean and unlocked: no --force, so git re-checks
    both, and a locked one (a running Workflow agent holds it) or a dirty one is left and named in the merge output.
    Never the main checkout. A failure is a problem string, not a failed merge. On Windows git can delete the files and
    unregister the worktree, then fail on the empty folder (another process has it as cwd, #269): that is a note in
    `notes`, not a problem, and the branch delete still runs."""
    from . import worktrees  # lazy: worktrees imports this module
    if worktrees._norm(wt) == worktrees._norm(root):
        return []
    held = next((w for w in worktrees._parse(root) if worktrees._norm(w["path"]) == worktrees._norm(wt)), None)
    if held is not None and held["locked"]:
        why = "locked"
    elif Path(wt).exists() and gates.dirty_files(wt):
        why = "has uncommitted changes"
    else:
        r = _git(root, "worktree", "remove", str(wt), check=False)
        if not r.returncode:
            return []
        why = f"remove failed: {(r.stderr or r.stdout).strip()[-TAIL:]}"
        if not any(worktrees._norm(w["path"]) == worktrees._norm(wt) for w in worktrees._parse(root)):
            note = invoke.cmd(f"worktree removed; empty folder left (in use): {wt}; prune it later: python -m aesir worktrees prune")
            S.log_task(conn, actor, t["id"], f"queue: {note} ({why})")
            if notes is not None:
                notes.append(note)
            return []
    S.log_task(conn, actor, t["id"], f"queue: worktree {wt} left in place ({why})")
    return [f"worktree left in place, {why.split(':')[0]}: {wt}"]


def _cleanup(conn, actor, root, t, wt, head, notes=None):
    """Post-merge: remove the task worktree and delete the merged branch. Returns short problem strings; non-problems
    (an unregistered worktree's empty folder left in use) are appended to `notes`."""
    branch = t["branch"]
    problems = []
    if wt is not None:
        problems += _remove_worktree(conn, actor, root, t, wt, notes)
        if problems:  # the worktree still holds the branch: deleting it would only add a second problem line
            return problems
    if _git(root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 1:
        return problems  # origin-only branch (merged from origin/<branch>): no local ref to delete
    r = _git(root, "branch", "-d", branch, check=False)
    if r.returncode and _git(root, "merge-base", "--is-ancestor", head, MAIN, check=False).returncode == 0:
        r = _git(root, "branch", "-D", branch, check=False)  # head is on main; -d only objects to a differing upstream (#73)
    if r.returncode:
        problems.append("git branch delete failed")
        S.log_task(conn, actor, t["id"], f"queue: branch delete: {(r.stderr or r.stdout).strip()[-TAIL:]}")
    return problems


def _unmerged_base(conn, t, root):
    """#177: the queue merges against main, so a task built on a base task that is not merged yet would list main's
    unrelated work as out of scope. Refusal text, or None."""
    if S.diff_base(conn, t, root) == "main":
        return None
    mid = S.load_extra(t).get("base_task")
    return f"base task #{mid} is not merged: merge #{mid} first, or clear --base-task"


def _export_roadmap(root, conn, problems) -> bool:
    """Write ROADMAP.md next to the state export; True when it should be committed. ROADMAP.md is a convenience:
    its failure never breaks a merge that already landed (notes go to problems)."""
    from . import releases  # late: releases pulls in install, which the queue otherwise does not need
    try:
        written = releases.write_roadmap(conn, root)
    except (OSError, UnicodeError, ValueError) as e:
        problems.append(f"ROADMAP.md not written: {type(e).__name__}: {e}")
        return False
    if written is None:
        problems.append("ROADMAP.md exists and is not generated (no generated header); left untouched")
        return False
    if _git(root, "check-ignore", "-q", "ROADMAP.md", check=False).returncode == 0:
        problems.append("ROADMAP.md is gitignored; not committed")
        return False
    return True


def _migrations_at(root, ref):
    """Migration count in db.py at git `ref` (aesir/ or the vendored .aesir-pkg/), None when neither is there: a project
    without Aesir's package has nothing to skew, so the caller does not hold."""
    for rel in skew.DB_PATHS:
        r = _git(root, "show", f"{ref}:{'/'.join(rel)}", check=False)
        if not r.returncode:
            return skew.migrations_in_text(r.stdout)
    return None


def _schema_hold(conn, root, t):
    """#266 (aesir-reports#169, T96): merging a branch that adds a db migration leaves every running agent whose worktree
    was cut earlier in schema skew (its hooks degrade). Returns (agents, count): the running agents behind the branch's
    migration count, [] when the branch adds none or nobody is behind. Re-evaluated each run, so a hold clears itself."""
    br = _git(root, "rev-parse", "--verify", t["branch"] + "^{commit}", check=False)
    if br.returncode:
        return [], 0  # process() reports the missing branch
    have, main = _migrations_at(root, br.stdout.strip()), _migrations_at(root, MAIN)
    if have is None or main is None or have <= main:
        return [], 0
    return skew.worktree_flags(conn, have), have


def _hold_result(t, agents, count):
    """The 'held' result: names each agent and worktree with its migration count, and the commands that clear it."""
    who = "; ".join(f"{a} at {wt} ({n} of {count} migrations)" for a, wt, n, _ in agents)
    return {"task": t["id"], "result": "held", "agents": [a for a, *_ in agents],
            "reason": invoke.cmd(
                f"task {t['id']} adds a db migration (schema {count}) and running agents are on an older schema: {who}. "
                "If one is finished: python -m aesir agent end <id>; or re-spin its worktree from main. "
                "Then re-run: python -m aesir queue run --once. "
                "(lead override: queue run --force-schema-hold --reason TEXT)")}


def _schema_gate(conn, actor, root, t, force_reason):
    """None to go on with the merge; else the 'held' result. A forced pass is logged on the task and as an event."""
    agents, count = _schema_hold(conn, root, t)
    if not agents:
        return None
    if not force_reason:
        return _hold_result(t, agents, count)
    names = [f"{a} ({wt})" for a, wt, _, _ in agents]
    S.log_task(conn, actor, t["id"], f"queue: schema hold forced ({force_reason}); agents skipped: {', '.join(names)}")
    with tx(conn):
        event(conn, actor, "queue", t["id"], "force_schema_hold", {"reason": force_reason, "agents": names})
    return None


def process(conn, actor, root, t, push=True):
    """Merge the reviewed branch head as it is: a merge commit built in a detached worktree is scope-checked and
    gated, then main fast-forwards to it. The task branch is only read, never rebased, reset or pushed (D154)."""
    branch = t["branch"]
    wt = _worktree_of(root, branch)
    if wt is not None:
        dirty = gates.dirty_files(wt)
        if dirty:
            return _fail(conn, actor, t, f"task worktree has uncommitted changes ({wt})", "\n".join(gates.dirty_detail(conn, wt, dirty)))
    try:  # origin/<branch> when the local ref lags it, as approved_head resolves (reports#119, #139)
        ref, head = S.resolve_branch_head(root, branch)
    except AesirError as e:
        return _fail(conn, actor, t, f"branch {branch} not found", str(e))
    if ref != branch:
        S.log_task(conn, actor, t["id"], f"queue: local {branch} lags; merging {ref} at {head[:8]}")
    # Anyone can push origin/<branch> after approval: the head merged must be the one approved (absent for tasks
    # approved before #177, which keep only the evidence check).
    approved = S.load_extra(t).get("approved_head")
    if approved and approved != head:
        return _fail(conn, actor, t, f"branch head {head[:8]} ({ref}) is not the approved head {approved[:8]}; re-review: lead runs task set {t['id']} --status in_review, reviews, then approves again")
    bad_ev = _evidence_mismatch(conn, t, head) or _unmerged_base(conn, t, root)
    if bad_ev:
        return _fail(conn, actor, t, bad_ev)
    main_sha = _git(root, "rev-parse", MAIN).stdout.strip()
    tmp = Path(root) / ".claude" / "worktrees" / f"queue-{t['id']}"
    try:
        merge_sha, conflict, detail = _merge_commit(root, tmp, t, main_sha, head)
    except AesirError as e:
        return _fail(conn, actor, t, "cannot create worktree", str(e))
    if conflict:
        return _fail(conn, actor, t, f"merge conflict with {MAIN}: {conflict}", detail)
    scope = tracks.effective_scope(t, root)
    bad = S.branch_out_of_scope(root, merge_sha, scope, main_sha)
    if bad:
        return _fail(conn, actor, t, f"outside scope ({scope}): {', '.join(bad[:10])}")
    g = gates.run_profile(conn, actor, "full", tmp, t["id"], config_root=root, locked=True, changed_base=main_sha)
    if not g["passed"]:
        bad = next((x for x in g["steps"] if x["result"] == "FAIL"), None)
        why = f"full gate failed on {g['sha'][:8]}" + (f": step {bad['step']} ({bad['why']})" if bad else "")
        return {**_fail(conn, actor, t, why, g["tail"]), "steps": g["steps"]}
    if g["sha"] != merge_sha:
        return _fail(conn, actor, t, f"gated {g['sha'][:8]} is not the merge commit {merge_sha[:8]}")
    _check_main_clean(root)
    if _git(root, "rev-parse", MAIN).stdout.strip() != main_sha:
        S.log_task(conn, actor, t["id"], f"queue: {MAIN} moved during the gate; task stays approved and queued, branch untouched")
        return {"task": t["id"], "result": "main_moved",
                "reason": f"{MAIN} moved while task {t['id']} was gated; it stays approved and queued, run the queue again"}
    r = _git(root, "merge", "--ff-only", merge_sha, check=False)
    if r.returncode:
        return _fail(conn, actor, t, "ff-merge failed", r.stdout + r.stderr)
    qrow = conn.execute("SELECT actor FROM merge_queue WHERE task_id=?", (t["id"],)).fetchone()
    queued_by = (qrow["actor"] if qrow else None) or "lead"
    S.set_task(conn, actor, t["id"], status="merged", reason="merge queue", repo=root, gated_sha=merge_sha)
    _drop(conn, t["id"])
    backup.export(conn, Path(root) / backup.STATE_DIR)  # a state-export failure propagates, as before #228
    export_problems = []
    paths = [backup.STATE_DIR]
    if _export_roadmap(root, conn, export_problems):
        paths.append("ROADMAP.md")
    _git(root, "add", "-A", *paths)
    if _git(root, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        _git(root, "commit", "-qm", f"Merge #{t['id']:03d} {t['title']}; state export")
    sha = _git(root, "rev-parse", "--short", "HEAD").stdout.strip()
    res = {"task": t["id"], "result": "merged", "sha": sha, "pushed": False, "steps": g["steps"]}
    notes = []
    problems = _cleanup(conn, actor, root, t, wt, head, notes) + export_problems
    if push and not _has_origin(root):
        res["reason"] = NO_ORIGIN
        if not conn.execute("SELECT 1 FROM events WHERE entity='queue' AND entity_id='hint' AND action='no_origin'").fetchone():
            with tx(conn):
                event(conn, actor, "queue", "hint", "no_origin", {})
            res["hint"] = NO_ORIGIN_HINT
    elif push:
        err = _push(root)
        if err:
            problems.append(f"push failed: {err}")
            res["result"] = "push_failed"
        else:
            res["pushed"] = True
    S.log_task(conn, actor, t["id"], f"merged by {actor} (queued by {queued_by})")
    if notes:
        res["notes"] = notes
    if problems:
        res["problems"] = problems
        S.log_task(conn, actor, t["id"], f"queue: merged at {sha}; " + "; ".join(problems)[-TAIL:])
    else:
        S.log_task(conn, actor, t["id"], f"queue: merged at {sha}{', pushed' if res['pushed'] else ''}{', ' + NO_ORIGIN if res.get('reason') == NO_ORIGIN else ''}")
    return res


def settings(root):
    """[queue] in aesir.toml: push (bool, default true). push = false: the queue merges but never pushes."""
    try:
        sec = gates.load_config(root).get("queue", {})
    except (AesirError, OSError, ValueError):
        sec = {}
    push = sec.get("push", True)
    if not isinstance(push, bool):
        raise AesirError("bad aesir.toml: [queue] push must be true or false")
    return {"push": push}


def _has_origin(root):
    return "origin" in _git(root, "remote", check=False).stdout.split()


NO_ORIGIN = "not pushed: no origin remote"
NO_ORIGIN_HINT = "hint: this project has no origin remote; set [queue] push = false in aesir.toml to silence this"


def _push(root):
    """None on success, else the error text."""
    r = _git(root, "push", "origin", MAIN, check=False)
    return (r.stderr or r.stdout).strip()[-TAIL:] if r.returncode else None


def _ahead(root):
    r = _git(root, "rev-list", "--count", f"origin/{MAIN}..{MAIN}", check=False)
    return r.returncode == 0 and int(r.stdout.strip() or 0) > 0


def run(conn, actor, root, once=False, push=True, lock_wait=None, force_schema_hold=None):
    """lock_wait: seconds to wait for the machine gate lock before an item (default aesir.toml [gate] lock_wait).
    The lock is taken before the item's merge worktree, so a busy lock leaves it queued and untouched; the run
    then stops with a 'busy' result. force_schema_hold: lead-only reason text that merges a schema-bump task despite
    running agents on an older schema (#266); without it such a task is 'held': it stays queued in its place, is listed in
    the results with its reason, and the run goes on with the next queue row (D218)."""
    if not _runner(conn, actor):
        _lead(actor)
    if force_schema_hold is not None:
        _lead(actor)
        if not force_schema_hold.strip():
            raise AesirError("--force-schema-hold needs a --reason")
    root = Path(root)
    if lock_wait is None:
        lock_wait = gates.gate_settings(root)[0]
    push = push and settings(root)["push"]
    acquire(conn, actor)
    results = []
    try:
        _check_main_clean(root)
        if push and _has_origin(root) and _ahead(root):
            err = _push(root)
            if err:
                results.append({"task": None, "result": "push_failed", "reason": f"main ahead of origin: {err}"})
        skipped = []  # held this run: left queued, not looked at again (else the LIMIT 1 pick would spin on it)
        while True:
            row = conn.execute(f"SELECT task_id FROM merge_queue WHERE task_id NOT IN ({','.join('?' * len(skipped))}) "
                               "ORDER BY seq LIMIT 1", skipped).fetchone()
            if not row:
                break
            t = S.get_task(conn, row["task_id"])
            if t["status"] != "approved" or not t["branch"]:
                _drop(conn, t["id"])
                results.append({"task": t["id"], "result": "dropped", "reason": f"task is {t['status']}"})
                continue
            held = _schema_gate(conn, actor, root, t, force_schema_hold)
            if held:
                results.append(held)
                skipped.append(t["id"])
                continue
            try:
                with gates.gate_lock(f"{gates.project_name(root)} task {t['id']}", wait=lock_wait, key=root,
                                    prio=gates.PRIO_QUEUE):
                    res = process(conn, actor, root, t, push)
            except gates.GateLockBusy as e:
                results.append({"task": t["id"], "result": "busy", "reason": str(e)})
                break
            finally:
                tmp = root / ".claude" / "worktrees" / f"queue-{t['id']}"
                if tmp.exists():
                    _git(root, "worktree", "remove", "--force", str(tmp), check=False)
            results.append(res)
            if res["result"] == "main_moved":
                break
            if once:
                break
    finally:
        release(conn)
    return results
