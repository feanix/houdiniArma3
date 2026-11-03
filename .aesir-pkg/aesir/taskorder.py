"""Task order (#176): 'A after B' links in task_after. B must be done before A; a wontfix B needs a human check."""
from .db import event, now, tx
from .service import AesirError, get_task

DONE = ("merged", "verified")
WAITING_STATES = ("open", "claimed", "in_progress")  # tasks that show a 'waits on' line


def is_done(status):
    return status in DONE


def fmt_status(status):
    return "wontfix (check)" if status == "wontfix" else status


def preds(conn, tid):
    """[(id, status)] of the tasks `tid` runs after, by id."""
    return [(r["after_id"], r["status"]) for r in conn.execute(
        "SELECT a.after_id, t.status FROM task_after a JOIN tasks t ON t.id=a.after_id WHERE a.task_id=? ORDER BY a.after_id",
        (int(tid),))]


def succs(conn, tid):
    """[(id, status)] of the tasks that run after `tid`, by id."""
    return [(r["task_id"], r["status"]) for r in conn.execute(
        "SELECT a.task_id, t.status FROM task_after a JOIN tasks t ON t.id=a.task_id WHERE a.after_id=? ORDER BY a.task_id",
        (int(tid),))]


def unfinished_preds(conn, tid):
    return [(i, s) for i, s in preds(conn, tid) if not is_done(s)]


def _find_cycle(conn, a, b):
    """Adding 'a after b' closes a cycle iff b already (transitively) runs after a. Returns the path b ... a, or None."""
    stack, seen = [(b, [b])], {b}
    while stack:
        node, path = stack.pop()
        if node == a:
            return path
        for r in conn.execute("SELECT after_id FROM task_after WHERE task_id=? ORDER BY after_id", (node,)):
            if r["after_id"] not in seen:
                seen.add(r["after_id"])
                stack.append((r["after_id"], path + [r["after_id"]]))
    return None


def link(conn, actor, a, b):
    a, b = int(a), int(b)
    if a == b:
        raise AesirError(f"task #{a} cannot run after itself")
    get_task(conn, a)
    get_task(conn, b)
    with tx(conn):
        path = _find_cycle(conn, a, b)
        if path:  # a after b, b after ..., ... after a
            raise AesirError("cycle: " + " after ".join(f"#{i}" for i in [a] + path))
        conn.execute("INSERT OR IGNORE INTO task_after VALUES (?,?)", (a, b))
        event(conn, actor, "task", a, "link", {"after": b})


def unlink(conn, actor, a, b):
    a, b = int(a), int(b)
    get_task(conn, a)
    get_task(conn, b)
    with tx(conn):
        n = conn.execute("DELETE FROM task_after WHERE task_id=? AND after_id=?", (a, b)).rowcount
        if not n:
            raise AesirError(f"no link: #{a} is not after #{b}")
        event(conn, actor, "task", a, "unlink", {"after": b})


def waits_line(conn, tid):
    """'waits on #B [status], ...' for the predecessors not done, or '' when none."""
    w = unfinished_preds(conn, tid)
    return "waits on " + ", ".join(f"#{i:03d} [{fmt_status(s)}]" for i, s in w) if w else ""


def waiting(conn):
    """{task id: waits line} for every open/claimed/in_progress task with an unfinished predecessor."""
    q = ",".join("?" * len(WAITING_STATES))
    out = {}
    for r in conn.execute(f"SELECT id FROM tasks WHERE status IN ({q}) ORDER BY id", WAITING_STATES):
        line = waits_line(conn, r["id"])
        if line:
            out[r["id"]] = line
    return out


def ready(conn):
    """Open tasks whose predecessors are all done (or have none), id order."""
    return [dict(r) for r in conn.execute(
        "SELECT id, title, status, owner FROM tasks t WHERE status='open' AND NOT EXISTS ("
        "SELECT 1 FROM task_after a JOIN tasks p ON p.id=a.after_id WHERE a.task_id=t.id AND p.status NOT IN ('merged','verified')"
        ") ORDER BY id")]


def path(conn, goal):
    """Longest chain of unfinished tasks `goal` transitively waits on, goal last: [(id, status)]. Ties go to the lowest ids."""
    t = get_task(conn, goal)
    memo = {}

    def longest(i):  # longest chain ending at i over unfinished predecessors (acyclic by construction)
        if i in memo:
            return memo[i]
        best = []
        for p, _ in unfinished_preds(conn, i):
            c = longest(p)
            if len(c) > len(best):
                best = c
        memo[i] = best + [i]
        return memo[i]

    chain = longest(t["id"])
    status = {r["id"]: r["status"] for r in conn.execute(
        f"SELECT id, status FROM tasks WHERE id IN ({','.join('?' * len(chain))})", chain)}
    return [(i, status[i]) for i in chain]


def start_warning(conn, actor, tid, status):
    """On moving to claimed/in_progress: the warning text (and a task_log line) when a predecessor is not done, else None."""
    if status not in ("claimed", "in_progress"):
        return None
    w = unfinished_preds(conn, tid)
    if not w:
        return None
    names = ", ".join(f"#{i:03d} [{fmt_status(s)}]" for i, s in w)
    text = f"warning: starting while predecessor not done: {names}"
    conn.execute("INSERT INTO task_log (task_id, ts, actor, text) VALUES (?,?,?,?)", (int(tid), now(), actor, text))
    return text
