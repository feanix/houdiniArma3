"""queue add/rm/list/run."""
import sys

from .. import db, gates, queue, tips
from ._common import out, line, add, grp


def queue_add(c, a):
    queue.add(c, a.actor, a.task)
    print(f"queued task {a.task}")
    tips.print_for(c, a, "commits", a.task)


def queue_rm(c, a):
    queue.remove(c, a.actor, a.task)
    print(f"removed task {a.task} from the queue")


def queue_list(c, a):
    out(a, queue.items(c), lambda rs: [line(f"#{r['task_id']:03d}", f"{r['status']:<10}", r["branch"], r["title"])
                                       for r in rs])


def _result(r):
    return f"merged ({r['reason']})" if r["result"] == "merged" and r.get("reason") else r["result"]


def queue_run(c, a):
    if a.reason and not a.force_schema_hold:
        raise SystemExit("--reason only goes with --force-schema-hold")
    reason = (a.reason or "") if a.force_schema_hold else None
    res = queue.run(c, a.actor, db.project_root(), a.once, not a.no_push, a.lock_wait, reason)
    out(a, res, lambda rs: [line(f"#{r['task']:03d}" if r["task"] else "-", _result(r), r.get("sha", ""),
                            "" if r["result"] == "merged" else r.get("reason", ""), "; ".join(r.get("problems", [])))
                            for r in rs] or print("queue empty"))
    for r in res if not a.json else ():
        steps = r.get("steps") or []
        if len(steps) > 1 or any(x["result"] == "skipped" for x in steps):  # every check and its result (#154)
            for x in steps:
                print(f"  #{r['task']:03d} gate step {x['result']}: {x['step']}" + (f" ({x['why']})" if x["why"] else ""))
        if r.get("hint"):
            print(r["hint"])
        for n in r.get("notes", []):  # #269: cleanup notes (e.g. an empty folder left) are not problems
            print(f"  #{r['task']:03d} note: {n}")
    if any(r["result"] == "busy" for r in res):
        sys.exit(gates.LOCK_BUSY_EXIT)
    if any(r["result"] in ("changes_requested", "push_failed", "main_moved", "held") for r in res):
        sys.exit(2)


def register(sub):
        q = grp(sub, "queue")
        add(q, "add", queue_add, ("task", {"type": int}))
        add(q, "rm", queue_rm, ("task", {"type": int}))
        add(q, "list", queue_list)
        add(q, "run", queue_run, ("--once", {"action": "store_true"}), ("--no-push", {"action": "store_true"}),
            ("--lock-wait", {"type": float, "help": "give up (exit 75, item stays queued) if the gate lock is held this many seconds"}),
            ("--force-schema-hold", {"action": "store_true", "help": "lead only: merge a schema-bump task although running agents are on an older schema (needs --reason). Without it such a task stays queued and is reported held (exit 2 when nothing else failed); later queue items still run"}),
            ("--reason", {"help": "why the schema hold is overridden"}))
