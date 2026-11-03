"""#138: per-agent scratch folder, so scratch output has a home that is neither bare /tmp (shared, T65) nor the main
checkout (a stray file there dirties the tree and blocks every gate).

Form: <system temp>/aesir/<project name>/<agent id>/. Created at SubagentStart and lazily by `spawn-context`.
Unbound agent: removed at SubagentStop when it succeeded; kept (marked with .aesir-kept) when it failed, so the lead
can look, and swept once older than [scratch] keep_failed_hours (default 24) by the next spawn/stop.
Agent bound to a task (#275, reports#175): kept after a clean finish too, because a later agent of the same workflow
may need its extracts; the sweep removes it once the task is merged/verified/wontfix (a failed one also waits out
keep_failed_hours). A task row that is missing or unreadable falls back to the hours rule, so nothing lives forever.
Everything here is best-effort: callers wrap it so a problem never blocks the agent (D115)."""
import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

from . import gates
from .service import AUTO_END_STATUSES, AesirError

KEEP_HOURS = 24
KEEP_BOUND_DAYS = 14  # D234: hard age cap on a bound folder, so a task that never closes cannot keep it forever
MARK = ".aesir-kept"


def base(root):
    """AESIR_SCRATCH_ROOT (tests) replaces <system temp>/aesir."""
    top = os.environ.get("AESIR_SCRATCH_ROOT") or Path(tempfile.gettempdir()) / "aesir"
    return Path(top) / gates.project_name(root)


def path(root, agent_id):
    return base(root) / agent_id


def ensure(root, agent_id):
    """Create (idempotent) and return the agent's folder."""
    p = path(root, agent_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


def keep_hours(root):
    """[scratch] keep_failed_hours from aesir.toml (hours a failed agent's folder is kept); default 24 when absent/bad."""
    try:
        v = gates.load_config(root).get("scratch", {}).get("keep_failed_hours", KEEP_HOURS)
    except (AesirError, OSError, ValueError, AttributeError):
        return KEEP_HOURS
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else KEEP_HOURS


def keep_bound_days(root):
    """[scratch] keep_bound_days from aesir.toml (days a task-bound folder may live while its task is still open);
    default 14 when absent/bad."""
    try:
        v = gates.load_config(root).get("scratch", {}).get("keep_bound_days", KEEP_BOUND_DAYS)
    except (AesirError, OSError, ValueError, AttributeError):
        return KEEP_BOUND_DAYS
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else KEEP_BOUND_DAYS


def succeeded(last_message):
    """What the hook knows: SubagentStop carries no exit status, only the final message. An agent that reached its
    required report ending (a `deviation:` line, see the agent defs) succeeded; none (empty, cut off, crashed) did not."""
    return any(ln.strip().lower().lstrip("`*- ").startswith("deviation:") for ln in (last_message or "").splitlines())


def finish(root, agent_id, ok, bound=False):
    """Agent ended: an unbound clean agent's folder is deleted; otherwise (failed, or bound to a task) it is kept and
    stamped `<time> ok|failed`. An empty folder is always removed."""
    p = path(root, agent_id)
    if not p.is_dir():
        return
    if (ok and not bound) or not any(x.name != MARK for x in p.iterdir()):
        shutil.rmtree(p, ignore_errors=True)
        return
    (p / MARK).write_text(f"{int(time.time())} {'ok' if ok else 'failed'}", encoding="utf-8")


def _task_closed(conn, agent_id):
    """True/False when the agent's task is closed/open; None when unknown (no bound row, no task row, db trouble)."""
    try:
        r = conn.execute("SELECT task_id FROM agents WHERE agent_id=? AND task_id IS NOT NULL AND task_id!=0 "
                         "ORDER BY id DESC LIMIT 1", (agent_id,)).fetchone()
        if r is None:
            return None
        t = conn.execute("SELECT status FROM tasks WHERE id=?", (r[0],)).fetchone()
    except (sqlite3.Error, AttributeError):
        return None
    return None if t is None else t[0] in AUTO_END_STATUSES


def sweep(root, running=(), conn=None):
    """Remove kept folders whose time is up, and any other folder untouched past keep_failed_hours whose agent is not
    running (an agent that never stopped). With `conn`, a kept folder of a task-bound agent is judged by its task: a
    clean one goes once the task is closed, a failed one once closed AND past the hours; an open task keeps both up to keep_bound_days. A
    task that cannot be read gets the hours rule. Returns the removed ids."""
    now = time.time()
    cutoff = now - keep_hours(root) * 3600
    cap = now - keep_bound_days(root) * 86400  # D234: older than this goes even while its task is open
    gone = []
    b = base(root)
    if not b.is_dir():
        return gone
    for d in b.iterdir():
        if not d.is_dir() or d.name in running:
            continue
        try:
            m = d / MARK
            t = (m if m.exists() else d).stat().st_mtime
            old = t < cutoff
            closed = _task_closed(conn, d.name) if conn is not None and m.exists() else None
            if closed is None:
                drop = old
            elif not closed:
                drop = t < cap
            else:  # closed: a clean folder goes now, a failed one after the hours
                drop = old or m.read_text(encoding="utf-8").split()[-1:] == ["ok"]
            if drop:
                shutil.rmtree(d, ignore_errors=True)
                gone.append(d.name)
        except OSError:
            continue
    return gone


def context_line(p, hours=KEEP_HOURS, task=None, days=KEEP_BOUND_DAYS):
    """The spawn-context line naming the folder `p` and the rule for it (`task`: the bound task id, else None)."""
    keep = (f"kept until task #{task} is merged, verified or wontfix, at most {days:g} days (a failed run also at "
            f"least {hours:g}h); "
            "outputs a later agent needs go in the task's docs/, not here" if task else
            f"deleted when you finish cleanly and kept {hours:g}h if you fail")
    return (f"Scratch folder: {p} -- put scratch files, logs and temp scripts here. Not bare /tmp (shared with other "
            f"sessions) and not the main checkout (a stray file there blocks every gate). It is {keep}.")
