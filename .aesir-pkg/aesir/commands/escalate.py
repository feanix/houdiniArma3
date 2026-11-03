"""escalate and dossier."""
from .. import db, escalation
from ._common import out, add, text_arg as _text_arg


def escalate_cmd(c, a):
    r = escalation.escalate(c, a.actor, a.task, db.project_root(), a.dry_run, a.klass, a.reason)
    out(a, r, lambda r: print(f"#{r['task']:03d} {r['from']} -> {r['to']} ({r['failures']}/{r['needed']} failures)"
                              + ("" if r["recorded"] else " [dry run]")))


def dossier_cmd(c, a):
    md = escalation.dossier(c, a.actor, a.task, db.project_root(), _text_arg(a.append) or "")
    out(a, {"task": a.task, "dossier": md}, lambda d: print(d["dossier"]))


def register(sub):
        add(sub, "escalate", escalate_cmd, ("task", {"type": int}),
            ("--dry-run", {"action": "store_true", "help": "show the suggestion without recording it"}),
            ("--class", {"dest": "klass", "action": "store_true", "help": "long-horizon class trigger (needs --reason)"}),
            ("--reason", {"default": ""}))
        add(sub, "dossier", dossier_cmd, ("task", {"type": int}),
            ("--append", {"help": "lead: add a note (repro, ruled-out, contracts, done-definition); @file reads a file"}))
