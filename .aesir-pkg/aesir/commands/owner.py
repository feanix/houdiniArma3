"""owner wait|answer|list: mark a task or decision as waiting on the owner (#86); see aesir/ownerwait.py."""
from .. import ownerwait as W
from ._common import out, add, grp


def _ref(v):
    return int(v.lstrip("#Dd"))  # accept 12, #012, D12


def wait(c, a):
    W.begin(c, a.actor, a.kind, _ref(a.ref), a.question)
    print(f"{a.kind} {a.ref} waiting on owner")


def answered(c, a):
    W.answer(c, a.actor, a.kind, _ref(a.ref), a.note)
    print(f"{a.kind} {a.ref} owner answered")


def lst(c, a):
    out(a, W.open_waits(c), lambda ws: W.render_waiting(ws) if ws else print("  (nothing waits on the owner)"))


def register(sub):
    g = grp(sub, "owner")
    add(g, "wait", wait, ("kind", {"choices": list(W.KINDS)}), ("ref", {}), ("question", {}))
    add(g, "answer", answered, ("kind", {"choices": list(W.KINDS)}), ("ref", {}), ("--note", {"default": ""}))
    add(g, "list", lst)
