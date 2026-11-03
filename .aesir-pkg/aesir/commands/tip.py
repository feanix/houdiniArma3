"""tip add/search/edit/rm."""
from .. import service as S, tips
from ._common import out, out_list, add, grp, paging, text_arg as _text_arg


def tip_add(c, a):
    tid = S.add_tip(c, a.actor, a.text, a.topic, a.tags)
    print(f"tip {tid} added" if tid else "tip already exists")


def tip_search(c, a):
    out_list(a, S.search_tips(c, a.words),
             lambda ts: [print(f"T{t['id']} [{t['topic']}] {t['body']}") for t in ts] or (not ts and print("no tips found")),
             "python -m aesir tip search " + " ".join(a.words), "python -m aesir tip edit <id> <text>")


def tip_edit(c, a):
    S.edit_tip(c, a.actor, a.id, _text_arg(a.text), a.topic, a.tags)
    print("tip updated")


def tip_rm(c, a):
    S.rm_tip(c, a.actor, a.id)
    print("tip removed")


def tip_promote(c, a):
    if a.actor not in ("lead", "advisor", "owner"):
        raise S.AesirError("tip promote: lead, advisor or owner only")
    tid = S.parse_id(a.id, "T")
    if a.target == "hook":
        if len(a.rest) != 1:
            raise S.AesirError("usage: tip promote <id> hook <check-name>")
        tips.promote_hook(c, a.actor, tid, a.rest[0])
        print(f"tip {tid} retired; enforced by hook check {a.rest[0]}")
        return
    if a.target != "core" or len(a.rest) != 2:
        raise S.AesirError("usage: tip promote <id> core <role> <section> [--scope owner|role|project] | <id> hook <name>")
    kind, ref = tips.promote(c, a.actor, tid, a.rest[0], a.rest[1], a.scope, a.reason, a.key)
    print(f"tip {tid} retired into core {a.rest[0]}/{a.rest[1]}/{ref}" if kind == "core" else
          f"tip {tid} proposed as core proposal {ref} (scope {a.scope}); the advisor relays owner approval "
          f"(core approve {ref} --for owner --quote ...); the tip retires on approval")


def register(sub):
        tp = grp(sub, "tip")
        add(tp, "add", tip_add, ("text", {}), ("--topic", {"default": ""}),
            ("--tags", {"default": "", "help": "where it applies: agent type, track, scope path, command (tasks, commits), words"}))
        paging(add(tp, "search", tip_search, ("words", {"nargs": "+"})))
        add(tp, "edit", tip_edit, ("id", {"help": "5 or T5"}), ("text", {"help": "new text; @file reads a file"}),
            ("--topic", {"help": "also change the topic"}), ("--tags", {"help": "replace the tags"}))
        add(tp, "promote", tip_promote, ("id", {"help": "5 or T5"}), ("target", {"choices": ["core", "hook"]}),
            ("rest", {"nargs": "+", "help": "core: <role> <section>; hook: <check-name>"}),
            ("--scope", {"default": "project", "choices": ["owner", "role", "project"]}),
            ("--reason", {"default": ""}), ("--key", {"default": ""}))
        add(tp, "rm", tip_rm, ("id", {"help": "5 or T5"}))
