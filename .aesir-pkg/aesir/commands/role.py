"""role checkin/agent/note/show."""
import os

from .. import service as S
from ._common import out, add, grp, text_arg


def role_agent(c, a):
    if a.action == "start":
        S.agent_start(c, a.actor, a.role, a.agent_id, a.task, a.model, a.session)
    else:
        S.agent_end(c, a.actor, a.role, a.agent_id, a.result)
    print(f"agent {a.agent_id} " + ("started" if a.action == "start" else "ended"))


def role_checkin(c, a):
    S.checkin(c, a.actor, a.role, a.session)
    print(f"{a.role} checked in")


def role_note(c, a):
    S.role_note(c, a.actor, a.role, text_arg(a.text))
    print("noted")


def _show_role(r):
    print(f"role: {r['role']}  session: {r['session'] or '-'}  name: {r.get('session_name') or '-'}  last checkin: {r['last_checkin'] or 'never'}")
    for n in r["notes"]:
        print(f"- {n['ts']} {n['actor']}: {n['text']}")


def role_show(c, a):
    out(a, S.show_role(c, a.role), _show_role)


def register(sub):
        r = grp(sub, "role")
        add(r, "checkin", role_checkin, ("role", {}),
            ("--session", {"default": os.environ.get("CLAUDE_SESSION_ID", ""),
                           "help": "optional: your session's name (e.g. the terminal's session name), shown in role show"}))
        add(r, "agent", role_agent, ("role", {}), ("action", {"choices": ["start", "end"]}), ("agent_id", {}),
            ("--task", {"type": int, "help": "task the agent works on (start)"}),
            ("--result", {"default": "", "help": "outcome (end)"}),
            ("--model", {"default": "", "help": "model the agent runs on (start)"}),
            ("--session", {"default": "", "help": "agent's session id (start)"}))
        add(r, "note", role_note, ("role", {}), ("text", {"help": "@file reads a file"}))
        add(r, "show", role_show, ("role", {}))
