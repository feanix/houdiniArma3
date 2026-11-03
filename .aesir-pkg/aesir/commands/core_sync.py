"""core scope / core sync: classify core rows and pull owner+role knowledge from an Aesir checkout."""
from .. import coresync as CS
from ._common import out, add, grp


def core_scope(c, a):
    CS.set_scope(c, a.actor, a.role, a.section, a.key, a.scope)
    print(f"core {a.role}/{a.section}/{a.key} scope -> {a.scope}")


def _render(d):
    for i in d["items"]:
        if i["action"] == "same":
            continue
        why = f" ({i['why']})" if i.get("why") else ""
        print(f"{i['action']:8} {i['role']}/{i['section']}/{i['key']} [{i['scope']}]{why}")
        if i["action"] in ("update", "propose") and i["old"]:
            print(f"           - {i['old']}")
        if i["action"] in ("add", "update", "propose"):
            print(f"           + {i['text']}")
    if d["applied"] is None:
        print("\n(diff only; pass --apply to write)")
    else:
        print(f"\napplied: {d['applied']['add']} added, {d['applied']['update']} updated, "
              f"{d['applied']['propose']} proposed")


def core_sync(c, a):
    items = CS.plan(c, CS.source_rows(a.source), add_only=a.add_only)
    applied = CS.apply(c, a.actor, items, a.source) if a.apply else None
    out(a, {"items": items, "applied": applied}, _render)


def register(sub):
        c = grp(sub, "core")
        add(c, "scope", core_scope, ("role", {}), ("section", {}), ("key", {}),
            ("scope", {"choices": ["owner", "role", "project"]}))
        add(c, "sync", core_sync, ("--from", {"dest": "source", "required": True,
                                              "help": "Aesir checkout (or db file) to read, read-only"}),
            ("--apply", {"action": "store_true"}), ("--add-only", {"action": "store_true"}))
