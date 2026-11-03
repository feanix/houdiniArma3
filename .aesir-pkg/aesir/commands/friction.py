"""friction record/list/link and deviation record."""
from .. import db, friction, service as S
from ._common import out, add, grp


def friction_record(c, a):
    friction.record(c, a.actor, a.type, a.detail, a.agent or "", a.task, source=a.actor)
    print("recorded")


def friction_list(c, a):
    out(a, friction.report(c, a.since or "", friction.load_config(db.project_root())), friction.render)


def friction_triage(c, a):
    out(a, friction.triage(c, a.since or "", a.top), friction.render_triage)


def friction_link(c, a):
    friction.link(c, a.actor, a.key, a.target)
    print("linked")


def deviation_record(c, a):
    S.get_task(c, a.task)
    friction.record_deviation(c, a.actor, a.task, a.cause, a.criterion, a.detail)
    print("recorded")


def register(sub):
    f = grp(sub, "friction")
    add(f, "record", friction_record, ("type", {"choices": list(friction.TYPES)}), ("detail", {}),
        ("--agent", {}), ("--task", {"type": int}))
    add(f, "list", friction_list, ("--since", {"help": "YYYY-MM-DD or full timestamp (UTC)"}))
    add(f, "triage", friction_triage, ("--since", {"help": "YYYY-MM-DD or full timestamp (UTC)"}),
        ("--top", {"type": int, "default": 10, "help": "show the N most frequent causes (0: all)"}))
    add(f, "link", friction_link, ("key", {"help": "a friction key or a `cause:...` key from `friction triage`"}),
        ("target", {"help": "task:<id> or tip:<id>"}))
    d = grp(sub, "deviation")
    add(d, "record", deviation_record, ("task", {"type": int}),
        ("--cause", {"required": True, "choices": list(friction.CAUSES)}),
        ("--criterion", {"default": ""}), ("--detail", {"default": ""}))
