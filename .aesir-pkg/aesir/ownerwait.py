"""Owner metrics (#86): what waits on an owner-only call, and for how long.

Why events and not a task status or table: the status machine is shared by every transition check, gate and report,
and a decision has no status at all; a new state would ripple through all of them. A wait is a flag on top of the
state, so it is two append-only events on the thing waited on ('owner-wait' with the question, 'owner-answered'),
which needs no migration and no new status. Time waiting = answered ts - wait ts (open waits count to now).
Owner prompt counting lives in agenthook (`owner-msg` events); [owner] record_text in aesir.toml opts into keeping text."""
import json

from . import db, gates
from .db import now
from .service import AesirError

KINDS = ("task", "decision")
WAIT, DONE = "owner-wait", "owner-answered"


def load_config(root):
    """[owner] in aesir.toml: record_text (default false) keeps the text of owner prompts; off, only length is stored."""
    try:
        sec = gates.load_config(root).get("owner", {})
    except (AesirError, OSError, ValueError):
        sec = {}
    if not isinstance(sec.get("record_text", False), bool):
        raise AesirError("bad aesir.toml: [owner] record_text must be true or false")
    sh = sec.get("stale_hours", 24)
    if isinstance(sh, bool) or not isinstance(sh, (int, float)) or sh < 0:
        raise AesirError("bad aesir.toml: [owner] stale_hours must be a number of hours >= 0")
    return {"record_text": sec.get("record_text", False), "stale_hours": sh}


def _sec(a, b):
    from .metrics import _t  # lazy: metrics imports this module
    return max(0, int((_t(b) - _t(a)).total_seconds()))


def _check(conn, kind, ref):
    if kind not in KINDS:
        raise AesirError(f"owner wait kind must be one of {', '.join(KINDS)}")
    tbl = "tasks" if kind == "task" else "decisions"
    r = conn.execute(f"SELECT * FROM {tbl} WHERE id=?", (int(ref),)).fetchone()
    if not r:
        raise AesirError(f"no such {kind}: {ref}")
    return r


def _is_open(conn, kind, ref):
    last = conn.execute("SELECT action FROM events WHERE entity=? AND entity_id=? AND action IN (?,?) ORDER BY id DESC",
                        (kind, str(ref), WAIT, DONE)).fetchone()
    return bool(last and last["action"] == WAIT)


def _log(conn, actor, kind, ref, text):
    if kind == "task":
        conn.execute("INSERT INTO task_log (task_id, ts, actor, text) VALUES (?,?,?,?)", (int(ref), now(), actor, text))


def begin(conn, actor, kind, ref, question):
    """Mark a task/decision as waiting on the owner. stage = the task's status at that moment ('decision' for one)."""
    if not question.strip():
        raise AesirError("a wait needs the question for the owner")
    row = _check(conn, kind, ref)
    if _is_open(conn, kind, ref):
        raise AesirError(f"{kind} {ref} is already waiting on the owner")
    with db.tx(conn):
        db.event(conn, actor, kind, ref, WAIT,
                 {"question": question, "stage": row["status"] if kind == "task" else "decision"})
        _log(conn, actor, kind, ref, f"waiting_owner: {question}")


def answer(conn, actor, kind, ref, note=""):
    _check(conn, kind, ref)
    if not _is_open(conn, kind, ref):
        raise AesirError(f"{kind} {ref} is not waiting on the owner")
    with db.tx(conn):
        db.event(conn, actor, kind, ref, DONE, {"note": note})
        _log(conn, actor, kind, ref, "owner answered" + (f": {note}" if note else ""))


def waits(conn, since=""):
    """Every wait that started at/after `since`: {kind, ref, question, stage, started, ended ('' if open), seconds}."""
    end, open_, out = now(), {}, []
    for e in conn.execute("SELECT ts, entity, entity_id, action, payload FROM events WHERE action IN (?,?) ORDER BY id",
                          (WAIT, DONE)):
        key = (e["entity"], e["entity_id"])
        if e["action"] == WAIT:
            p = json.loads(e["payload"] or "{}")
            w = {"kind": e["entity"], "ref": int(e["entity_id"]), "question": p.get("question", ""),
                 "stage": p.get("stage", ""), "started": e["ts"], "ended": "", "seconds": _sec(e["ts"], end)}
            open_[key] = w
            out.append(w)
        elif key in open_:
            w = open_.pop(key)
            w["ended"], w["seconds"] = e["ts"], _sec(w["started"], e["ts"])
    return [w for w in out if w["started"] >= since]


def open_waits(conn):
    return [w for w in waits(conn) if not w["ended"]]


def stale(conn, stale_hours):
    """Open waits that are stale: open longer than stale_hours, or on a task already merged/verified/closed.
    Each is the wait plus 'why' ('old' or the task status) and 'role' (task requester, 'advisor' for decisions)."""
    out = []
    for w in open_waits(conn):
        role, why = "advisor", ""
        if w["kind"] == "task":
            t = conn.execute("SELECT status, requester FROM tasks WHERE id=?", (w["ref"],)).fetchone()
            role = t["requester"] if t and t["requester"] in ("advisor", "lead") else "advisor"
            if t and t["status"] in ("merged", "verified", "closed"):
                why = t["status"]
        if not why and w["seconds"] > stale_hours * 3600:
            why = "old"
        if why:
            out.append({**w, "why": why, "role": role})
    return out


def summary(conn, since="", task_ids=None):
    """Owner-wait totals: overall, per stage (task status when the wait began), per task. task_ids limits to those tasks."""
    ws = [w for w in waits(conn, since) if task_ids is None or (w["kind"] == "task" and w["ref"] in task_ids)]
    by_stage, by_task = {}, {}
    for w in ws:
        by_stage[w["stage"]] = by_stage.get(w["stage"], 0) + w["seconds"]
        if w["kind"] == "task":
            by_task[w["ref"]] = by_task.get(w["ref"], 0) + w["seconds"]
    return {"waits": len(ws), "open": sum(1 for w in ws if not w["ended"]), "seconds": sum(w["seconds"] for w in ws),
            "seconds_by_stage": by_stage, "seconds_by_task": by_task}


def render_waiting(ws, indent="  "):
    for w in ws:
        ref = f"#{w['ref']:03d}" if w["kind"] == "task" else f"D{w['ref']}"
        age = w["seconds"]  # open waits count to now
        print(f"{indent}{ref} waiting {age // 3600}h{age % 3600 // 60:02d}m, since {w['started']} ({w['stage']}): {w['question']}")
