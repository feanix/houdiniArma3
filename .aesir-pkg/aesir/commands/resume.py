"""resume: a role's handoff notes, inbox, identity core and work; warns when the role's session context passed
[sessions] restart_tokens (#92); lists stalled tasks the role acts on (#80), old ones as one count (#246).
Section order (#190): release and session warnings, stalled tasks, handoff notes, inbox (unanswered, bodies cut to 2 lines, the rest one count line),
direct messages since the last checkin (#203), identity core, core proposals, owner-pending, work."""
import json

from .. import db, messages, metrics, ownerwait, releases, service as S
from ..service import AesirError
from ._common import out, add, steer_line, client_cmd
from .core import _render_core
from .role import _show_role
from .status import active_agents, idle_agents, idle_note, load_config as status_cfg


def _render_inbox_item(m):
    """One inbox item, body cut to 2 lines (#190); the full text is a thread/inbox command away."""
    where = f"#{m['task_id']:03d}" if m["task_id"] else (f"D{m['decision_id']}" if m["decision_id"] else m["channel"])
    print(f"M{m['id']} {m['ts']} {m['kind']} {where} {m['sender']} -> {m['recipient']}")
    lines = (m["body"] or "").strip().splitlines()
    for ln in lines[:2]:
        print(f"    {ln}")
    if len(lines) > 2:
        ref = str(m["task_id"]) if m["task_id"] else (f"D{m['decision_id']}" if m["decision_id"] else m["channel"])
        print("    ... " + client_cmd(f"python -m aesir thread {ref}") + " or " + client_cmd("python -m aesir inbox"))


def _render_old_stalls(r):
    """One count line for old stalls (#246): October-style leftovers would otherwise bury the live lines."""
    if r.get("stalled_old"):
        days = r["stalled_old_days"]
        print(f"{len({x['task'] for x in r['stalled_old']})} older stalled tasks (idle {days:g}+ days) not listed: "
              + client_cmd(f"python -m aesir status ack --older-than {days:g}") + " pauses them all\n")


def _render_resume(r):
    if r.get("release"):
        print(r["release"] + "\n")
    for w in r.get("session_warnings", []):
        print(f"WARNING: {w['text']}\n")
    for x in r.get("stalled", []):
        print(f"STALLED [{x['role']}] {x['text']}  -> {x['cmd']}\n")
    _render_old_stalls(r)
    print("== Handoff notes ==")  # before the inbox (#190): the note says what the inbox items mean
    _show_role(r)
    print()
    col = r.get("inbox_collapsed") or []
    if r.get("inbox") or col:
        print("== Inbox (unanswered) ==")
        for m in r["inbox"]:
            _render_inbox_item(m)
        if col:
            print(f"  {len(col)} more need no answer (acked, 'closed:' or stale replies): "
                  + client_cmd("python -m aesir inbox") + "; close asks with inbox --close M<n>...")
        print()
    if r.get("unread_replies"):  # #197: replies are not asks; nothing to answer, only to read and mark
        print("== Unread replies ==")
        for m in r["unread_replies"]:
            _render_inbox_item(m)
        print("  Read, then clear with: " + client_cmd("python -m aesir inbox --mark-read") + " (do not reply to a reply)")
        print()
    if r.get("direct_messages"):  # #203: SendMessage notes between checkins; a startup handshake message is optional
        print("== Direct messages since your last checkin ==")
        for m in r["direct_messages"]:
            first = ((m["body"] or "").strip().splitlines() or [""])[0][:100]
            ref = str(m["task_id"]) if m["task_id"] else "direct"
            print(f"M{m['id']} {m['ts']} {m['sender']}: {first}  -> "
                  + client_cmd(f"python -m aesir thread {ref}") + "; clear: " + client_cmd("python -m aesir inbox --mark-read"))
        print()
    print("== Identity core ==", end="")
    _render_core(r["core"])
    if not r["core"]:
        print("\n  (empty; run: core seed <role>)")
    if r["core_proposals"]:
        print("\n== Core proposals ==")
        for p in r["core_proposals"]:
            note = f" [{p['decided_by']}: {p['decision_note']}]" if p["status"] != "pending" else ""
            print(f"  P{p['id']} {p['status']} {p['role']}/{p['section']}/{p['key']}: {p['text']}"
                  f" (by {p['proposed_by']}: {p['reason']}){note}")
    op = r.get("owner_pending")
    if op:
        print("\n== Owner-pending ==")
        for p in op["proposals"]:
            print(f"  proposal P{p['id']} {p['role']}/{p['section']}/{p['key']}: {p['text']}")
        for t in op["escalated"]:
            print(f"  escalated #{t['id']:03d} {t['title']}")
        for t in op["to_verify"]:
            print(f"  to verify #{t['id']:03d} {t['title']}")
        if not any(op.values()):
            print("  (none)")
    if r.get("owner_waiting"):
        print("\n== Waiting on the owner ==")
        ownerwait.render_waiting(r["owner_waiting"])
    print("\n== Priorities ==")
    for p in r["priorities"] or [None]:
        print("  (none)" if p is None else f"  {p['key']}: {json.dumps(p['body'], ensure_ascii=False)}")
    print("\n== Work ==")
    for title, key in (("Your tasks", "tasks"), ("Merged, awaiting your verification", "awaiting_verification")):
        print(f"\n{title}:")
        for t in r[key] or [None]:
            if t is None:
                print("  (none)")
                continue
            print(f"  #{t['id']:03d} [{t['status']}] {t['title']}")
            if t.get("waits"):
                print(f"      {t['waits']}")
            for e in t["latest_log"]:
                print(f"      {e['ts']} {e['actor']}: {e['text']}")
    print("\nRunning agents:")
    for g in r["agents"] or [None]:
        print("  (none)" if g is None else
              f"  {g['agent_id']}  task {('#%03d' % g['task_id']) if g['task_id'] else '-'}  started {g['started']}"
              + (f"  [{g['idle']}]" if g.get("idle") else ""))
    print("  " + steer_line())  # #172
    print("\nDecisions since previous checkin:" if r["previous_checkin"] else "\nRecent decisions:")
    for d in r["new_decisions"] or [None]:
        print("  (none)" if d is None else f"  D{d['id']} {d['date']} {d['title']} [{d['decided_by']}]")


def resume_data(c, role, actor=None, session=None):
    """S.resume plus the role's unanswered inbox (runs the lazy reminder/handoff sweep first). session: the calling
    session's id (SessionStart); the size warning then measures only that transcript (reports#100)."""
    messages.sweep(c, actor or role, messages.load_config(db.project_root()))
    try:
        scfg = status_cfg(db.project_root())
        S.end_finished_agents(c, actor or role, scfg["agent_idle_min"], orphan_min=scfg["no_agent_min"])  # reports#136: a finished Workflow's row
    except AesirError:  # a broken aesir.toml must not block resume
        scfg = None
    r = S.resume(c, role)
    idle = {x["agent_id"]: idle_note(x) for x in idle_agents(c, scfg)} if scfg else {}  # once, not per agent
    for g in r["agents"]:  # #195: label agents whose transcript went quiet
        g["idle"] = idle.get(g["agent_id"], "")
    needs, r["inbox_collapsed"] = messages.split_attention(messages.inbox(c, role))  # reports#136
    r["inbox"] = [m for m in needs if m["kind"] == "ask"]
    r["unread_replies"] = [m for m in needs if m["kind"] != "ask"]  # #197: news, not work; cleared by --mark-read
    r["direct_messages"] = messages.direct_unread(c, db.project_root(), role, r["previous_checkin"])
    r["owner_waiting"] = ownerwait.open_waits(c)
    note = releases.notice(db.project_root())
    if note:
        r["release"] = note
    try:
        r["session_warnings"] = metrics.session_warnings(metrics.load_config(db.project_root()), role=role, session=session,
                                                         active=active_agents(c, scfg) if scfg else ())
    except AesirError:  # a broken aesir.toml must not block resume
        r["session_warnings"] = []
    try:
        from .status import stale_def_flags  # lazy: status imports a lot
        r["session_warnings"] += [{"text": x["text"]} for x in stale_def_flags(c, db.project_root(), role)]
    except AesirError:
        pass
    try:
        from .status import ack_done_shown, is_old_stall, load_config, unacked_stalls  # lazy: status imports a lot
        cfg = load_config(db.project_root())
        every = unacked_stalls(c, db.project_root(), cfg, role)
        r["stalled_old"] = [x for x in every if is_old_stall(x, cfg["stall_old_days"])]  # #246: counted, not listed
        r["stalled"] = [x for x in every if x not in r["stalled_old"]]
        r["stalled_old_days"] = cfg["stall_old_days"]
        ack_done_shown(c, actor or role, [x for x in r["stalled"] if x["kind"] == "done_task"])  # shown once
    except AesirError:
        r["stalled"], r["stalled_old"] = [], []
    return r


def resume(c, a):
    out(a, resume_data(c, a.role, a.actor), _render_resume)


def register(sub):
        add(sub, "resume", resume, ("role", {}))
