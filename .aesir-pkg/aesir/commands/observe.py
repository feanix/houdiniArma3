"""observe check; event owner-sensor."""
from .. import db, observe
from ..service import AesirError
from ._common import add, grp, out


def observe_check(c, a):
    res = observe.check(c, db.project_root())
    out(a, [{"entry": k, "result": r, "detail": m} for k, r, m in res],
        lambda rows: [print(f"{r['result']:<4} {r['entry']}: {r['detail']}") for r in rows])
    if any(r == "fail" for _, r, _ in res):
        raise AesirError("observe check failed")


def event_owner_sensor(c, a):
    observe.owner_sensor(c, a.actor, a.task, a.note)
    print("recorded")


def register(sub):
    add(grp(sub, "observe"), "check", observe_check)
    add(grp(sub, "event"), "owner-sensor", event_owner_sensor, ("--task", {"type": int, "required": True}),
        ("--note", {"default": "", "help": "what the owner had to describe about observed app behaviour"}))
