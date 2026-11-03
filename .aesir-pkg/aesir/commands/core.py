"""core show/set/rm/propose/approve/reject/seed."""
from .. import service as S
from ._common import out, add, grp, proposal_id


def _render_core(rows, pending=()):
    section = None
    for r in rows:
        if r["section"] != section:
            section = r["section"]
            print(f"\n{section}:")
        print(f"  - [{r['rule']}/{r['scope']}] {r['key']}: {r['text']}")
    for p in pending:
        print(f"  pending P{p['id']} {p['section']}/{p['key']}: {p['text']} ({p['reason']})")


def core_show(c, a):
    out(a, S.show_core(c, a.role),
        lambda d: (print(f"core: {d['role']}", end=""), _render_core(d["rows"], d["pending"])))


def core_set(c, a):
    print(f"core {a.section}/{S.set_core(c, a.actor, a.role, a.section, a.text, a.reason, a.key)} updated; decision logged")


def core_rm(c, a):
    S.rm_core(c, a.actor, a.role, a.section, a.key, a.reason)
    print(f"core {a.section}/{a.key} removed; decision logged")


def core_propose(c, a):
    print(f"proposal {S.propose_core(c, a.actor, a.role, a.section, a.text, a.reason, a.key)} pending owner approval")


def core_approve(c, a):
    S.approve_core(c, a.actor, a.id, a.for_, a.quote)
    print(f"proposal {a.id} approved and applied")


def core_reject(c, a):
    S.reject_core(c, a.actor, a.id, a.reason, a.for_, a.quote)
    print(f"proposal {a.id} rejected")


def core_seed(c, a):
    out(a, {"added": S.seed_core(c, a.actor, a.role)}, lambda d: print(f"seeded {d['added']} rows"))


def register(sub):
        c = grp(sub, "core")
        add(c, "show", core_show, ("role", {}))
        add(c, "set", core_set, ("role", {}), ("section", {}), ("text", {}), ("--reason", {}), ("--key", {"default": ""}))
        add(c, "rm", core_rm, ("role", {}), ("section", {}), ("key", {}), ("--reason", {}))
        add(c, "propose", core_propose, ("role", {}), ("section", {}), ("text", {}), ("--reason", {}),
            ("--key", {"default": ""}))
        add(c, "approve", core_approve, ("id", {"type": proposal_id}), ("--for", {"dest": "for_"}), ("--quote", {"default": ""}))
        add(c, "reject", core_reject, ("id", {"type": proposal_id}), ("--reason", {}), ("--for", {"dest": "for_"}),
            ("--quote", {"default": ""}))
        add(c, "seed", core_seed, ("role", {}))
