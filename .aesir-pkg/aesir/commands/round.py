"""round arm|disarm|check|watch: supervise a running round (#156). See aesir/rounds.py and docs/supervision.md."""
import time

from .. import db, rounds
from ..service import AesirError
from ._common import out, add, grp


def r_arm(c, a):
    rid = rounds.arm(c, a.actor, db.project_root(), a.task, a.branch or "", a.workflow or "", a.label or "", a.limit,
                     a.agents or "")
    out(a, {"round": rid}, lambda d: print(d["round"]))


def r_disarm(c, a):
    done = rounds.disarm(c, a.actor, a.id, a.task)
    out(a, {"disarmed": done}, lambda d: print("disarmed: " + (", ".join(map(str, d["disarmed"])) or "none")))


def r_check(c, a):
    if a.all and a.id is not None:
        raise AesirError("round check: give a round id or --all, not both")
    rows = rounds.check(c, a.actor, db.project_root(), a.id)
    out(a, rows, lambda rs: [print(rounds.summary(r)) for r in rs] or print("round check: no armed rounds"))


def r_show(c, a):
    root = db.project_root()
    cfg, at = rounds.load_config(root), time.time()
    rows = rounds.live(c, a.id)
    out(a, rows, lambda rs: [print(rounds.view(c, root, r, cfg, at)) for r in rs] or print("round show: no live rounds"))


def r_watch(c, a):
    return rounds.watch(c, a.actor, db.project_root(), a.interval, a.max)


def register(sub):
    g = grp(sub, "round")
    add(g, "arm", r_arm, ("--task", {"type": int, "required": True}), ("--branch", {}), ("--workflow", {}), ("--label", {}),
        ("--limit", {"help": "wall-clock limit, e.g. 45m (default: [rounds] wall_limit)"}),
        ("--agents", {"help": "comma-separated agent ids whose tokens count (default: the task's agents since arming)"}))
    add(g, "disarm", r_disarm, ("id", {"nargs": "?", "type": int}), ("--task", {"type": int}))
    add(g, "check", r_check, ("id", {"nargs": "?", "type": int}), ("--all", {"action": "store_true"})).description = (
        "Record a check of live rounds: updates their state and may ask the lead. For a look without recording, use "
        "'round show' / 'round list'.")
    for name in ("show", "list"):
        add(g, name, r_show, ("id", {"nargs": "?", "type": int})).description = (
            "Read-only view of live rounds (state, last progress, idle time, live tokens, open command); records nothing, asks no one.")
    add(g, "watch", r_watch, ("--interval", {"type": float, "help": "seconds between passes (default: [rounds] interval, 180)"}),
        ("--max", {"type": float, "default": rounds.MAX_DEFAULT, "help": "seconds before 'watch: restart' (Monitor 30-min cap)"}))
