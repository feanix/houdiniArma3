"""ask, reply, note, wait, inbox, thread, post, search."""
import sys

from .. import db, messages, service as S
from ._common import out, out_list, add, paging, text_arg as _text_arg


def _cfg():
    return messages.load_config(db.project_root())


def _render_msg(m):
    where = f"#{m['task_id']:03d}" if m["task_id"] else (f"D{m['decision_id']}" if m["decision_id"] else m["channel"])
    flags = "".join([" [reminded]" if m.get("reminded_ts") else "",
                     f" [handed to {m['handed_to']}]" if m.get("handed_to") else "",
                     " [answered]" if m.get("answered") else "", " [closed]" if m.get("closed") else "",
                     f" [visible to {','.join(m['visible_to'])}]" if m["visible_to"] is not None else ""])
    arrow = f"{m['sender']} -> {m['recipient']}" if m["recipient"] else f"{m['sender']} in {m['channel']}"
    re_ = f" re M{m['parent_id']}" if m["parent_id"] else ""
    print(f"M{m['id']} {m['ts']} {m['kind']} {where}{re_} {arrow}{flags}")
    print(f"    {m['body']}")


def msg_ask(c, a):
    mid = messages.ask(c, a.actor, a.target, a.to, _text_arg(a.text), a.visible_to)
    nl = messages.for_caller(messages.notify_line(c, db.project_root(), mid))
    out(a, {"id": mid, "notify": nl}, lambda d: (print(f"M{d['id']} sent; wait with: wait M{d['id']}"),
                                                 d["notify"] and print(d["notify"])))


def msg_reply(c, a):
    mid = messages.reply(c, a.actor, a.message, _text_arg(a.text))
    nl = messages.for_caller(messages.notify_line(c, db.project_root(), mid))
    out(a, {"id": mid, "notify": nl}, lambda d: (print(f"M{d['id']} sent"), d["notify"] and print(d["notify"])))


def msg_note(c, a):
    agent = S.resolve_agent(c, a.agent)  # #207: an agent id, or a task for its one running agent
    mid = messages.steer(c, a.actor, agent, _text_arg(a.text))
    out(a, {"id": mid, "agent": agent}, lambda d: print(f"M{d['id']} stored; the hook delivers it on {agent}'s next tool call"))


def msg_wait(c, a):
    r = messages.wait(c, a.actor, a.message, _cfg(), a.timeout)
    out(a, {"reply": r}, lambda d: _render_msg(d["reply"]) if d["reply"] else print(
        f"timeout: no reply to {a.message} after {a.timeout:g}s", file=sys.stderr))
    if r is None:
        raise SystemExit(3)


def msg_inbox(c, a):
    if a.close or a.close_before or a.close_task is not None:  # #190: bulk-close asks as superseded, no message sent
        done = messages.close_asks(c, a.actor, a.role or a.actor, a.close or (), a.close_before, a.close_task)
        out(a, {"closed": done}, lambda d: print("closed " + (", ".join(f"M{i}" for i in d["closed"]) or "nothing")
                                                  + " (superseded; no message delivered to the sender)"))
        return
    messages.sweep(c, a.actor, _cfg())
    ms = messages.inbox(c, a.role or a.actor, a.all, a.mark_read)
    if a.mark_read:
        messages.mark_direct_read(c, db.project_root(), a.role or a.actor)  # #203
        # marking covers every reply returned, so show them all
        a.limit, a.page = max(len(ms), 1), 1
    first = next((m for m in ms if m["kind"] == "ask" and not m.get("answered") and not m.get("closed")), None)
    out_list(a, ms, lambda ms: [_render_msg(m) for m in ms] or print("inbox empty"), "python -m aesir inbox"
             + (f" {a.role}" if a.role else "") + (" --all" if a.all else ""),
             f"python -m aesir reply M{first['id']} \"<text>\"" if first else None, "python -m aesir status")


def msg_thread(c, a):
    out_list(a, messages.thread(c, a.actor, a.target), lambda ms: [_render_msg(m) for m in ms] or print("no messages"),
             f"python -m aesir thread {a.target}", f"python -m aesir ask {a.target} --to <role> \"<text>\"")


def msg_post(c, a):
    mid = messages.post(c, a.actor, a.channel, _text_arg(a.text), _cfg(), a.visible_to)
    out(a, {"id": mid}, lambda d: print(f"M{d['id']} posted"))


def msg_search(c, a):
    out_list(a, messages.search(c, a.actor, a.words), lambda ms: [_render_msg(m) for m in ms] or print("no messages found"),
             "python -m aesir search " + " ".join(a.words), "python -m aesir inbox")


def register(sub):
        add(sub, "ask", msg_ask, ("target", {"help": "task id (7) or decision id (D3)"}),
            ("--to", {"required": True, "help": "role or agent id"}), ("text", {"help": "@file reads a file"}),
            ("--visible-to", {"help": "comma list; only these (plus you and --to) can read it"}))
        add(sub, "reply", msg_reply, ("message", {"help": "M12 or 12"}), ("text", {"help": "@file reads a file"}))
        add(sub, "note", msg_note, ("agent", {"help": "running agent id, or #N for the one running agent on task N"}), ("text", {"help": "@file reads a file"}))
        # `wait <message>` is registered by commands/wait.py, which also holds the Monitor-ready waiter (#156)
        paging(add(sub, "inbox", msg_inbox, ("role", {"nargs": "?", "help": "default: the caller (--as)"}),
            ("--all", {"action": "store_true", "help": "include answered asks and read replies"}),
            ("--mark-read", {"action": "store_true", "help": "mark the replies shown as read"}),
            ("--close", {"nargs": "+", "metavar": "M<n>", "help": "close these asks as superseded (no reply sent)"}),
            ("--close-before", {"metavar": "DATE", "help": "close open asks older than DATE (YYYY-MM-DD)"}),
            ("--close-task", {"type": int, "metavar": "N", "help": "close open asks on merged/verified task N"})))
        paging(add(sub, "thread", msg_thread, ("target", {"help": "task id, D<decision id> or channel name"})))
        add(sub, "post", msg_post, ("channel", {}), ("text", {"help": "@file reads a file"}),
            ("--visible-to", {"help": "comma list"}))
        paging(add(sub, "search", msg_search, ("words", {"nargs": "+"})))
