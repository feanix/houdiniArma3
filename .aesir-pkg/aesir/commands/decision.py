"""decision add/list/show/amend: decisions are append-only; amend adds a note, --supersedes links a replacement."""
from .. import service as S
from ._common import out, out_list, hint, line, add, grp, paging


def dec_add(c, a):
    print(f"decision {S.add_decision(c, a.actor, a.title, a.context, a.choice, a.alternatives, a.decided_by, a.reversible, a.task, a.supersedes)} added")


def dec_amend(c, a):
    S.amend_decision(c, a.actor, a.id, a.note)
    print(f"decision {a.id} amended")


def dec_list(c, a):
    rows = S.list_decisions(c)
    out_list(a, rows, lambda ds: [line(f"D{d['id']}", d["date"], d["title"], f"[{d['decided_by']}]" + _marks(d)) for d in ds],
             "python -m aesir decision list", f"python -m aesir decision show D{rows[-1]['id']}" if rows else "python -m aesir decision add <title>")


def _marks(d):
    m = ""
    if d.get("amendments"):
        m += f" (amended x{d['amendments']})"
    if d.get("superseded_by"):
        m += " (superseded by " + ", ".join(f"D{x}" for x in d["superseded_by"]) + ")"
    return m


def _show_dec(d):
    for k in ("date", "title", "context", "choice", "alternatives", "decided_by", "reversible"):
        print(f"{k}: {d[k]}")
    if d["tasks"]:
        print("tasks:", ", ".join("#" + t for t in d["tasks"]))
    if d["supersedes"]:
        print("supersedes:", ", ".join(f"D{x}" for x in d["supersedes"]))
    if d["superseded_by"]:
        print("superseded by:", ", ".join(f"D{x}" for x in d["superseded_by"]))
    for m in d["amendments"]:
        print(f"amendment {m['ts']} [{m['actor']}]: {m['note']}")


def dec_show(c, a):
    d = S.show_decision(c, a.id)
    out(a, d, _show_dec)
    hint(a, *[f"python -m aesir task show {t}" for t in d["tasks"][:2]], "python -m aesir decision list")


def register(sub):
        d = grp(sub, "decision")
        add(d, "add", dec_add, ("title", {}), ("--context", {"default": ""}), ("--choice", {"default": ""}),
            ("--alternatives", {"default": ""}), ("--decided-by", {"default": ""}), ("--reversible", {"default": "yes"}),
            ("--task", {"type": int, "action": "append", "default": [], "help": "linked task id (repeatable)"}),
            ("--supersedes", {"action": "append", "default": [], "help": "decision id this replaces (repeatable)"}))
        paging(add(d, "list", dec_list))
        add(d, "amend", dec_amend, ("id", {"help": "11 or D11"}), ("--note", {"required": True}))
        add(d, "show", dec_show, ("id", {"help": "11 or D11"}))
