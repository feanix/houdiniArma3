"""Messages (charter s5): ask/reply/wait/inbox/thread attached to tasks or decisions, kept in the db.

Visibility: `visible_to` is a JSON list of names (NULL = everyone). Sender and recipient are always in it. A reader
matches by its own name or, for an agent id, by the agent's role. A reader outside the list never sees the message
in inbox, thread or search, and never gets it handed to them. Reminders and handoff run lazily (`sweep`), on any
inbox, resume or wait call: there is no daemon.
"""
import json
import os
import re
import time
from datetime import datetime, timezone

from . import acks, gates
from .db import event, has_msg_fts, now, tx
from .rolenames import prefix_configured, role_ok, session_prefix
from .service import AesirError, get_task, parse_id

DEFAULTS = {"reminder_min": 30, "handoff_min": 60, "channels": ["announcements"]}
LEAD = "lead"
WAIT_TIMEOUT = 600
_FMT = "%Y-%m-%dT%H:%M:%SZ"
DIRECT = "direct"  # channel of a captured SendMessage (kind 'note' with a recipient); 'direct:<project>' = cross-project
STEER = "steer"  # channel of a note to a running subagent (kind 'note', recipient = its agent id), #141
EXPIRY_PREFIX = "[system] "  # starts the reply expire_notes sends; split_attention collapses on it, not on wording
_TASK_REF = re.compile(r"(?<![\w&])#(\d{1,6})\b")


def load_config(root) -> dict:
    """[messages] in aesir.toml: reminder_min, handoff_min (minutes), channels (announcement-only names)."""
    try:
        sec = gates.load_config(root).get("messages", {})
    except AesirError as e:
        if "no aesir.toml" in str(e):
            return dict(DEFAULTS)
        raise
    cfg = {**DEFAULTS, **sec}
    for k in ("reminder_min", "handoff_min"):
        v = cfg[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            raise AesirError(f"bad aesir.toml: [messages] {k} must be a number of minutes >= 0")
    if cfg["handoff_min"] < cfg["reminder_min"]:
        raise AesirError("bad aesir.toml: [messages] handoff_min must be >= reminder_min")
    ch = cfg["channels"]
    if not isinstance(ch, list) or not all(isinstance(x, str) and x for x in ch):
        raise AesirError("bad aesir.toml: [messages] channels must be a list of names")
    return cfg


def names(conn, who) -> set:
    """Everything a reader answers to: its own name, plus its role if it is an agent id."""
    out = {who}
    for r in conn.execute("SELECT role FROM agents WHERE agent_id=?", (who,)):
        out.add(r["role"])
    return out


def _msg(r):
    m = dict(r)
    m["visible_to"] = json.loads(m["visible_to"]) if m["visible_to"] is not None else None
    return m


def _visible(m, who_names) -> bool:
    return m["visible_to"] is None or bool(who_names & set(m["visible_to"]))


def _get(conn, mid, reader):
    r = conn.execute("SELECT * FROM messages WHERE id=?", (int(mid),)).fetchone()
    m = _msg(r) if r else None
    if not m or not _visible(m, names(conn, reader)):  # a hidden message looks like a missing one
        raise AesirError(f"no such message: M{mid}")
    return m


def _insert(conn, actor, kind, body, recipient="", task=None, decision=None, channel="", parent=None, vis=None):
    if not (body or "").strip():
        raise AesirError("message text is empty")
    if vis is not None:
        vis = sorted(set(vis) | names(conn, actor) | ({recipient} if recipient else set()))
    cur = conn.execute(
        "INSERT INTO messages (kind, task_id, decision_id, channel, parent_id, sender, recipient, body, visible_to, ts)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (kind, task, decision, channel, parent, actor, recipient, body,
         json.dumps(vis) if vis is not None else None, now()))
    mid = cur.lastrowid
    if has_msg_fts(conn):
        conn.execute("INSERT INTO messages_fts (rowid, body) VALUES (?,?)", (mid, body))
    event(conn, actor, "message", mid, kind, {"task": task, "decision": decision, "channel": channel,
                                              "to": recipient, "parent": parent})
    return mid


def _split(vis):
    if vis is None:
        return None
    items = vis.split(",") if isinstance(vis, str) else vis
    out = [x.strip() for x in items if x.strip()]
    return out or None


def ask(conn, actor, target, to, text, visible_to=None):
    """target: task id (7, #7) or decision id (D3). Returns the message id."""
    if not (to or "").strip():
        raise AesirError("ask needs --to <role|agent>")
    t = str(target).strip().lstrip("#")
    task = decision = None
    with tx(conn):
        if t[:1].upper() == "D":
            decision = parse_id(t, "D")
            if not conn.execute("SELECT 1 FROM decisions WHERE id=?", (decision,)).fetchone():
                raise AesirError(f"no such decision: {target}")
        else:
            task = get_task(conn, parse_id(t, "T"))["id"]
        return _insert(conn, actor, "ask", text, to.strip(), task, decision, vis=_split(visible_to))


def post(conn, actor, channel, text, cfg, visible_to=None):
    """Announcement on an untethered channel (not an ask: no recipient, never in an inbox)."""
    if channel not in cfg["channels"]:
        raise AesirError(f"unknown channel '{channel}' (have: {', '.join(cfg['channels']) or 'none'})")
    with tx(conn):
        return _insert(conn, actor, "note", text, channel=channel, vis=_split(visible_to))


def reply(conn, actor, mid, text):
    with tx(conn):
        p = _get(conn, parse_id(mid, "M"), actor)
        if p["kind"] == "note" and p["channel"] != STEER:
            raise AesirError(f"M{p['id']} is an announcement; nothing to reply to")
        if p["kind"] == "reply":  # #197: a reply needs no answer; replying to it only loops
            raise AesirError(f"M{p['id']} is a reply; replies need no answer. Mark it read: "
                             "python -m aesir inbox --mark-read")
        if p["channel"] == STEER:
            # the agent's names() include its spawner (the sender), so the generic test below cannot tell sides apart
            to = p["recipient"] if actor == p["sender"] else p["sender"]
        else:
            to = p["sender"] if p["sender"] not in names(conn, actor) else p["recipient"]
        mid = _insert(conn, actor, "reply", text, to, p["task_id"], p["decision_id"], p["channel"], p["id"],
                      p["visible_to"])
        if p["sender"] == p["recipient"] and to in names(conn, actor):  # #197: answering a message one sent to oneself: kept as the answer, born read
            conn.execute("UPDATE messages SET read_ts=? WHERE id=?", (now(), mid))
        return mid


def steer(conn, actor, agent_id, text):
    """Note to a running subagent (#141): stored now, pushed into its context by the hook on its next tool call
    (`deliver_notes`), expired when it ends (`expire_notes`). Returns the message id."""
    a = conn.execute("SELECT * FROM agents WHERE agent_id=? AND ended='' ORDER BY id DESC LIMIT 1",
                     (agent_id,)).fetchone()
    if not a:
        raise AesirError(f"no running agent {agent_id} (see: agent list --running)")
    with tx(conn):
        return _insert(conn, actor, "note", text, agent_id, a["task_id"], channel=STEER)


_UNANSWERED = ("kind='note' AND channel=? AND recipient=? AND expired_ts='' AND NOT EXISTS "
               "(SELECT 1 FROM messages r WHERE r.parent_id=messages.id AND r.kind='reply' AND r.sender<>messages.sender)")


def unanswered_notes(conn, agent_id):
    """Steering notes to the agent that it has not replied to yet (#172), oldest first: [{id, sender, body, new}]."""
    rows = conn.execute(f"SELECT id, sender, body, read_ts FROM messages WHERE {_UNANSWERED} ORDER BY id",
                        (STEER, agent_id)).fetchall()
    return [{"id": r["id"], "sender": r["sender"], "body": r["body"], "new": not r["read_ts"]} for r in rows]


def deliver_notes(conn, agent_id):
    """Every unanswered steering note for the agent, oldest first (#172: repeated on each call until it replies);
    notes not seen before are marked delivered and flagged new. Returns [{id, sender, body, new}]."""
    with tx(conn):
        notes = unanswered_notes(conn, agent_id)
        for n in notes:
            if n["new"]:
                conn.execute("UPDATE messages SET read_ts=? WHERE id=?", (now(), n["id"]))
                event(conn, agent_id, "message", n["id"], "read", {"via": "hook"})
    return notes


def expire_notes(conn, actor, agent_id):
    """The agent ended: its undelivered steering notes expire and each sender gets a reply saying so (it lands in
    their inbox). Delivered notes stay as history. Returns the expired message ids."""
    with tx(conn):
        rows = conn.execute("SELECT id, sender FROM messages WHERE kind='note' AND channel=? AND recipient=?"
                            " AND read_ts='' AND expired_ts='' ORDER BY id", (STEER, agent_id)).fetchall()
        for r in rows:
            conn.execute("UPDATE messages SET expired_ts=? WHERE id=?", (now(), r["id"]))
            conn.execute("INSERT INTO messages (kind, task_id, channel, parent_id, sender, recipient, body, ts)"
                         " SELECT 'reply', task_id, channel, id, ?, sender, ?, ? FROM messages WHERE id=?",
                         (actor, f"{EXPIRY_PREFIX}M{r['id']} to {agent_id} was never delivered: the agent ended first.",
                          now(), r["id"]))
            event(conn, actor, "message", r["id"], "expire", {"agent": agent_id})
        # delivered but unanswered notes stop repeating (#172) with the agent; they stay as history, no reply sent
        conn.execute(f"UPDATE messages SET expired_ts=? WHERE {_UNANSWERED}", (now(), STEER, agent_id))
    return [r["id"] for r in rows]


def answers(conn, mid, reader):
    rn = names(conn, reader)
    return [m for m in (_msg(r) for r in conn.execute(
        "SELECT * FROM messages WHERE parent_id=? AND kind='reply' ORDER BY id", (int(mid),))) if _visible(m, rn)]


def _dt(ts):
    return datetime.strptime(ts, _FMT).replace(tzinfo=timezone.utc)


def sweep(conn, actor, cfg, at=None):
    """Lazy reminders and handoff for unanswered asks. Returns [{id, action}] for what it did."""
    at = at or datetime.now(timezone.utc)
    rows = conn.execute(
        "SELECT * FROM messages m WHERE kind='ask' AND handed_to='' AND expired_ts='' "
        "AND NOT EXISTS (SELECT 1 FROM messages r WHERE r.parent_id=m.id AND r.kind='reply')").fetchall()
    due = []
    for r in rows:
        age = (at - _dt(r["ts"])).total_seconds() / 60
        if (not r["reminded_ts"] and age >= cfg["reminder_min"]) or age >= cfg["handoff_min"]:
            due.append((r["id"], age))
    done = []
    if not due:
        return done
    with tx(conn):
        for mid, age in due:
            m = _msg(conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone())
            if conn.execute("SELECT 1 FROM messages WHERE parent_id=? AND kind='reply'", (mid,)).fetchone():
                continue
            ts = at.strftime(_FMT)
            if not m["reminded_ts"]:
                conn.execute("UPDATE messages SET reminded_ts=? WHERE id=?", (ts, mid))
                event(conn, actor, "message", mid, "remind", {"to": m["recipient"], "age_min": round(age, 1)})
                done.append({"id": mid, "action": "remind"})
            if age >= cfg["handoff_min"]:
                # never to the asker, never to someone who may not read it, no point if lead already holds it
                if (LEAD not in names(conn, m["sender"]) and LEAD not in names(conn, m["recipient"])
                        and _visible(m, {LEAD})):
                    conn.execute("UPDATE messages SET handed_to=?, handed_ts=? WHERE id=?", (LEAD, ts, mid))
                    event(conn, actor, "message", mid, "handoff", {"to": LEAD, "age_min": round(age, 1)})
                    done.append({"id": mid, "action": "handoff"})
    return done


def acked_ask_ids(conn) -> set:
    """Ids of asks whose `status ack unanswered_ask:N` still holds (#190), by the shared rule in acks."""
    st = acks.episodes(conn, "unanswered_ask:%")
    if not st:
        return set()
    last = acks.last_event_id(conn)
    return {int(k.split(":", 1)[1]) for k, e in st.items() if acks.holds(e["ack"], k.split(":", 1)[1], last)}


STALE_REPLY_H = 48  # an unread reply older than this is history, not news (resume collapses it)


def split_attention(msgs, at=None):
    """(needs, collapsed) for resume (reports#136): asks not status-acked stay; unread replies stay unless they
    start with 'closed:', carry EXPIRY_PREFIX (the system's expiry reply) or are older than STALE_REPLY_H."""
    at = at or datetime.now(timezone.utc)
    needs, collapsed = [], []
    for m in msgs:
        if m["kind"] == "ask":
            (collapsed if m.get("acked") else needs).append(m)
            continue
        body = (m["body"] or "").lstrip().lower()
        old = (at - _dt(m["ts"])).total_seconds() > STALE_REPLY_H * 3600
        system = (m["body"] or "").startswith(EXPIRY_PREFIX)
        (collapsed if body.startswith("closed:") or system or old else needs).append(m)
    return needs, collapsed


def close_asks(conn, actor, reader, ids=(), before=None, task=None):
    """Close open asks in the reader's inbox as superseded (#190): no reply, nothing delivered to the sender; they
    leave the hook count, inbox, status and resume. Select by message ids, by `before` (a date: asks older than it)
    or by `task` (must be merged/verified). Returns the closed ids."""
    open_ = {m["id"]: m for m in inbox(conn, reader) if m["kind"] == "ask"}
    pick = []
    for x in ids:
        mid = parse_id(x, "M")
        if mid not in open_:
            raise AesirError(f"M{mid} is not an open ask in {reader}'s inbox")
        pick.append(mid)
    if before:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2}Z)?", before):
            raise AesirError(f"--close-before {before!r}: use YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ")
        try:
            datetime.strptime(before[:10], "%Y-%m-%d")
        except ValueError:
            raise AesirError(f"--close-before {before!r} is not a real date") from None
        pick += [i for i, m in open_.items() if m["ts"] < before]
    if task is not None:
        t = get_task(conn, task)
        if t["status"] not in ("merged", "verified"):
            raise AesirError(f"task {t['id']} is {t['status']}, not merged or verified; close its asks by id instead")
        pick += [i for i, m in open_.items() if m["task_id"] == t["id"]]
    pick = sorted(set(pick))
    with tx(conn):
        for mid in pick:
            conn.execute("UPDATE messages SET expired_ts=? WHERE id=?", (now(), mid))
            event(conn, actor, "message", mid, "close", {"reader": reader})
    return pick


def inbox(conn, who, all_=False, mark_read=False):
    """Unanswered asks addressed to (or handed to) the reader, plus its unread replies. all_: everything addressed.
    Closed asks (#190) are left out unless all_; each ask carries `acked` (status-acked, still listed) and `closed`."""
    wn = names(conn, who)
    ph = ",".join("?" * len(wn))
    rows = conn.execute(
        f"SELECT * FROM messages WHERE (recipient IN ({ph}) OR handed_to IN ({ph})) AND kind IN ('ask','reply') "
        "ORDER BY id", (*wn, *wn)).fetchall()
    out, unread = [], []
    acked = acked_ask_ids(conn)
    for r in rows:
        m = _msg(r)
        if not _visible(m, wn):
            continue
        if m["kind"] == "ask":
            m["answered"] = bool(conn.execute(
                "SELECT 1 FROM messages WHERE parent_id=? AND kind='reply'", (m["id"],)).fetchone())
            m["closed"] = bool(m["expired_ts"])
            m["acked"] = m["id"] in acked
            if (m["answered"] or m["closed"]) and not all_:
                continue
        else:
            if m["read_ts"] and not all_:
                continue
            if not m["read_ts"]:
                unread.append(m["id"])
        m["reminded"] = bool(m["reminded_ts"])
        m["handed"] = bool(m["handed_to"])
        out.append(m)
    if mark_read:
        _mark_read(conn, who, unread)
    return out


def _mark_read(conn, who, ids):
    """Stamp read_ts on the messages and log one 'read' event (shared by inbox and mark_direct_read)."""
    if ids:
        with tx(conn):
            conn.execute(f"UPDATE messages SET read_ts=? WHERE id IN ({','.join('?' * len(ids))})", (now(), *ids))
            event(conn, who, "message", ",".join(map(str, ids)), "read", {})


def direct_names(conn, root, role) -> set:
    """Every recipient string a captured SendMessage to this role can carry: role, its session name, '<prefix>-<role>'."""
    out = {role, f"{session_prefix(root)}-{role}"}
    row = conn.execute("SELECT session_name FROM roles WHERE role=?", (role,)).fetchone()
    if row and row["session_name"]:
        out.add(row["session_name"])
    return {n.lower() for n in out}


def direct_unread(conn, root, role, since=""):
    """Unread captured direct messages (#203) to the role (not ones it sent itself), newer than `since` (a checkin ts; '' = all). Local only:
    'direct:<project>' notes went to another project."""
    wn = sorted(direct_names(conn, root, role))
    ph = ",".join("?" * len(wn))
    rows = conn.execute(
        f"SELECT * FROM messages WHERE kind='note' AND channel=? AND read_ts='' AND ts>? "
        f"AND lower(recipient) IN ({ph}) AND lower(sender) NOT IN ({ph}) ORDER BY id",
        (DIRECT, since or "", *wn, *wn)).fetchall()
    return [_msg(r) for r in rows]


def mark_direct_read(conn, root, role):
    """inbox --mark-read also clears the role's direct messages, so resume does not list them again."""
    _mark_read(conn, role, [m["id"] for m in direct_unread(conn, root, role)])


def wait(conn, actor, mid, cfg, timeout=WAIT_TIMEOUT, sleep=time.sleep, clock=time.monotonic):
    """Block until a reply to the ask arrives. Polls the db with backoff. Returns the reply, or None on timeout."""
    mid = parse_id(mid, "M")
    m = _get(conn, mid, actor)
    if m["kind"] != "ask":
        raise AesirError(f"M{mid} is not an ask")
    end, delay = clock() + timeout, 0.2
    while True:
        sweep(conn, actor, cfg)
        got = answers(conn, mid, actor)
        if got:
            with tx(conn):
                conn.execute("UPDATE messages SET read_ts=? WHERE id=? AND read_ts=''", (now(), got[0]["id"]))
                event(conn, actor, "message", got[0]["id"], "read", {"via": "wait"})
            return got[0]
        left = end - clock()
        if left <= 0:
            return None
        sleep(min(delay, left))
        delay = min(delay * 1.5, 5.0)


def thread(conn, reader, target):
    """All visible messages on a task (7), a decision (D3) or a channel (name), oldest first."""
    t = str(target).strip().lstrip("#")
    if t[:1].upper() == "D" and t[1:].isdigit():
        col, val = "decision_id", int(t[1:])
    elif t.isdigit():
        col, val = "task_id", get_task(conn, int(t))["id"]
    else:
        col, val = "channel", t
    rn = names(conn, reader)
    msgs = [m for m in (_msg(r) for r in conn.execute(f"SELECT * FROM messages WHERE {col}=? ORDER BY id", (val,)))
            if _visible(m, rn)]
    for m in msgs:
        if m["kind"] == "ask":
            m["answered"] = any(x["parent_id"] == m["id"] and x["kind"] == "reply" for x in msgs)
            m["closed"] = bool(m["expired_ts"])  # #190: closed as superseded
    return msgs


def search(conn, reader, words, limit=50):
    words = [w for w in words if w.strip()]
    if not words:
        raise AesirError("give at least one search word")
    if has_msg_fts(conn):
        q = " ".join('"%s"*' % w.replace('"', '""') for w in words)
        rows = conn.execute("SELECT m.* FROM messages_fts f JOIN messages m ON m.id = f.rowid"
                            " WHERE messages_fts MATCH ? ORDER BY rank", (q,))
    else:
        cond = " AND ".join("body LIKE ?" for _ in words)
        rows = conn.execute(f"SELECT * FROM messages WHERE {cond} ORDER BY id", [f"%{w}%" for w in words])
    rn = names(conn, reader)
    return [m for m in (_msg(r) for r in rows) if _visible(m, rn)][:limit]


def recipient_project(conn, recipient, prefix):
    """Project prefix of a recipient session name that belongs to ANOTHER project, else ''. A name is local when it has
    no '-', starts with this project's prefix, is a known role or is a known agent id."""
    r = (recipient or "").strip().lower()
    if "-" not in r or r.startswith(prefix + "-") or role_ok(conn, r):
        return ""
    if conn.execute("SELECT 1 FROM agents WHERE agent_id=?", (recipient,)).fetchone():
        return ""
    return r.split("-")[0]


def capture_direct(conn, sender, recipient, text, prefix):
    """Record a direct session message (SendMessage) so it outlives the transcript. A '#N' cite links it to local task
    N when the recipient is in this project and N exists; a recipient in another project is never linked (#N is
    ambiguous there) and is tagged in the channel. Returns the message id, or None for an empty text."""
    text = (text or "").strip()
    if not text or not sender:
        return None
    other = recipient_project(conn, recipient, prefix)
    task = None
    if not other:
        for n in _TASK_REF.findall(text):
            if conn.execute("SELECT 1 FROM tasks WHERE id=?", (int(n),)).fetchone():
                task = int(n)
                break
    with tx(conn):
        return _insert(conn, sender, "note", text, (recipient or "").strip(), task,
                       channel=f"{DIRECT}:{other}" if other else DIRECT)


def notify_session(conn, root, recipient):
    """Session name to wake for a recipient: '<prefix>-<role>' for a persistent role (advisor, lead, checked-in), the
    agent id for a running subagent, else '' (nobody to wake)."""
    r = (recipient or "").strip()
    if not r:
        return ""
    return notify_target(conn, root, r)[0]


def notify_target(conn, root, recipient):
    """(session, how): how is 'recorded' (the role's real session name from its checkin), 'derived' (built from the
    project prefix) or '' (agent id, or nobody to wake: session '')."""
    r = (recipient or "").strip()
    if not r:
        return "", ""
    prefix = session_prefix(root)
    low = r.lower()
    role = low if role_ok(conn, low) else (low[len(prefix) + 1:] if low.startswith(prefix + "-")
                                           and role_ok(conn, low[len(prefix) + 1:]) else "")
    if role:
        row = conn.execute("SELECT session_name FROM roles WHERE role=?", (role,)).fetchone()
        if row and row["session_name"]:
            return row["session_name"], "recorded"
        return f"{prefix}-{role}", "derived"
    if conn.execute("SELECT 1 FROM agents WHERE agent_id=? AND ended=''", (r,)).fetchone():
        return r, ""
    return "", ""


SUBAGENT_NOTIFY = "notify: no SendMessage needed; the lead sees this through the note channel"


def for_caller(line):
    """The notify line as the caller can act on it. Subagents (AESIR_AGENT, exported into a bound agent's shell by
    the agent hook; the same marker cli.main and db._guard use) have no SendMessage tool (aesir-reports#130), so
    they get one line saying no message is needed. Role sessions get `line` unchanged; an empty line stays empty."""
    return SUBAGENT_NOTIFY if line and os.environ.get("AESIR_AGENT") else line


def notify_line(conn, root, mid):
    """'notify: SendMessage <session> "<task, from, first line>"' for message `mid`, or '' when no one to wake."""
    m = conn.execute("SELECT * FROM messages WHERE id=?", (int(mid),)).fetchone()
    sess, how = notify_target(conn, root, m["recipient"]) if m else ("", "")
    if not sess or m["sender"] == m["recipient"]:  # #197: never wake a session about its own message
        return ""
    first = (m["body"].strip().splitlines() or [""])[0].replace('"', "'")[:100]
    where = f"#{m['task_id']:03d} " if m["task_id"] else (f"D{m['decision_id']} " if m["decision_id"] else "")
    line = f'notify: SendMessage {sess} "{where}M{m["id"]} from {m["sender"]}: {first}"'
    if how == "derived":  # a wrong guess looks like an offline peer (aesir-reports#49): say it is a guess
        line += (" (session name derived from " + ("[project] session_prefix" if prefix_configured(root) else
                 "[project] name; set session_prefix if wrong") + "; a failed send may mean a wrong name, not offline)")
    return line
