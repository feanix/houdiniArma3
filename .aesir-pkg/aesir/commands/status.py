"""status: one screen of judgements, anomalies first. Each anomaly names the role that acts and the command that
resolves it; `status ack <key>` hides one until its state changes; one open too long escalates once to its role.
A claimed/in_progress task with no activity for stall_min is a `stalled_task` anomaly (#80); acking it pauses it on
purpose, because its sig is the last-activity time and so the ack lapses at the next activity.
#195: a running agent whose transcript has been silent for agent_idle_min is an `idle_agent` ("owner session may be
gone"); one whose transcript finished is ended on status/resume (tokens kept); a claimed/in_progress task with no
running agent and no task-log line for no_agent_min is a `no_live_agent` anomaly (ackable like the others).
#246 (reports#136): a claimed/in_progress task whose agent ended and whose branch is merged is one `done_task` anomaly
(it replaces that task's stalled/no-agent lines) naming the command that closes it; resume shows it once. Stalls older
than stall_old_days are counted, not listed, in resume; `status ack --older-than <days>` acks them in one go."""
import json
from datetime import datetime, timezone

from .. import proc, queue, worktrees
from .. import acks, backup, db, evidence, install, friction, gates, longrun, messages, metrics, ownerwait, rank, releases, rounds, sched, skew, service as S, taskorder, tips, upstream, upstream_pickup
from ..rolenames import prefix_configured, session_prefix
from ..service import AesirError
from ._common import out, add, hint, client_cmd

FMT = "%Y-%m-%dT%H:%M:%SZ"
DEFAULTS = {"in_review_min": 240, "approved_min": 120, "changes_requested_min": 240,
            "agent_min": 120, "unbound_min": 10, "unbound_end_min": 60, "escalate_min": 480, "stall_min": 60,
            "agent_idle_min": 10, "no_agent_min": 1440, "stall_old_days": 7}
STUCK = ("in_review", "approved", "changes_requested")


def load_config(root):
    """[observe.aesir] in aesir.toml: minutes before a state counts as stuck."""
    try:
        sec = gates.load_config(root).get("observe", {}).get("aesir", {})
    except AesirError as e:
        if "no aesir.toml" in str(e):
            return dict(DEFAULTS)
        raise
    cfg = {**DEFAULTS, **sec}
    for k in DEFAULTS:
        v = cfg[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            raise AesirError(f"bad aesir.toml: [observe.aesir] {k} must be a number >= 0 (minutes; stall_old_days: days)")
    return cfg


def _age(ts, at):
    try:
        return (at - datetime.strptime(ts, FMT).replace(tzinfo=timezone.utc)).total_seconds() / 60
    except (ValueError, TypeError):
        return 0.0


def _dur(m):
    m = int(m)
    return f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}m"


def _unpushed(root):
    try:
        r = proc.run(["git", "rev-list", "--count", "origin/main..main"], cwd=str(root), capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
    except OSError:
        return 0
    return int(r.stdout.strip() or 0) if r.returncode == 0 else 0


def _export_lag(c, root):
    """(events the newest aesir-state export lacks, ts of the first of them); None: no export to compare with."""
    f = root / backup.STATE_DIR / "events.jsonl"
    if not f.exists():
        return None
    last = 0
    for ln in f.read_text(encoding="utf-8").splitlines()[-1:]:
        try:
            last = json.loads(ln)["id"]
        except (ValueError, KeyError):
            pass
    r = c.execute("SELECT COUNT(*), MIN(ts) FROM events WHERE id>?", (last,)).fetchone()
    return r[0], r[1]


def _practices(c, root):
    """{key: 'default'|'adopted'|'unadopted'} (#082): [practices] flags in aesir.toml plus lead core row keys."""
    try:
        flags = gates.load_config(root).get("practices", {})
    except AesirError:
        flags = {}
    keys = {r[0] for r in c.execute("SELECT key FROM core WHERE role='lead'")}
    return install.practice_state(flags, keys)


def _branch_commit(root, branch):
    """ts of the newest commit on the task branch (an implementer may commit without logging); '' if unknown."""
    if not branch:
        return ""
    try:
        r = proc.run(["git", "log", "-1", "--format=%ct", branch, "--"], cwd=str(root), capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if r.returncode != 0 or not r.stdout.strip().isdigit():
        return ""
    return datetime.fromtimestamp(int(r.stdout.strip()), timezone.utc).strftime(FMT)


def last_activity(c, root, t):
    """Newest of: task status change, task log, evidence, gate run, the task's agents (start/end and the events they
    made), and a commit on the task branch. Fixed-format UTC strings, so max() is chronological."""
    tid = t["id"]
    q = ("SELECT MAX(ts) FROM task_log WHERE task_id=?", "SELECT MAX(ts) FROM evidence WHERE task_id=?",
         "SELECT MAX(ts) FROM gate_runs WHERE task_id=?",
         "SELECT MAX(started) FROM agents WHERE task_id=?", "SELECT MAX(ended) FROM agents WHERE task_id=?",
         "SELECT MAX(ts) FROM events WHERE agent_id IN (SELECT agent_id FROM agents WHERE task_id=?)")
    seen = [t["updated"], _branch_commit(root, t["branch"])] + [c.execute(x, (tid,)).fetchone()[0] for x in q]
    return max(x for x in seen if x)


def stall_flags(c, root, cfg, at=None):
    """stalled_task anomaly dicts (key stalled_task:<id>); sig = last activity, so an ack lasts until the next activity."""
    at = at or datetime.now(timezone.utc)
    res = []
    for t in c.execute("SELECT id, title, status, updated, branch FROM tasks WHERE status IN ('claimed','in_progress') "
                       "ORDER BY id"):
        last = last_activity(c, root, t)
        age = _age(last, at)
        if age >= cfg["stall_min"]:
            res.append({"kind": "stalled_task", "key": f"stalled_task:{t['id']}", "role": "lead",
                        "text": f"#{t['id']:03d} {t['status']} with no activity since {last} ({_dur(age)}): {t['title']}"
                                f" (paused on purpose? ack it)",
                        "cmd": client_cmd(f"python -m aesir status ack stalled_task:{t['id']}"), "sig": last,
                        "task": t["id"], "status": t["status"], "last_activity": last, "minutes": int(age)})
    return res


def _agent_states(c, cfg, at):
    """[(row, state)] for every running agent, read once: 'idle' (transcript silent for agent_idle_min and no open tool
    call younger than the rounds step limit; #165: an open call excuses silence), 'active', or 'unknown' (no
    readable transcript), #195."""
    try:
        limit = rounds.step_limit(rounds.load_config(db.project_root()))
    except AesirError:  # a broken aesir.toml must not block resume
        limit = S.parse_duration(rounds.STEP_DEFAULT)
    res = []
    for g in c.execute("SELECT * FROM agents WHERE ended='' ORDER BY id").fetchall():
        g = dict(g)
        idle = S.transcript_idle_min(g, at)
        if idle is None:
            res.append((g, "unknown"))
        elif idle >= cfg["agent_idle_min"] and not any(
                at.timestamp() - ts <= limit for ts, _ in rounds.open_calls(g["transcript_path"])):
            res.append((g, "idle"))
        else:
            res.append((g, "active"))
    return res


def idle_agents(c, cfg, at=None):
    """Running agents whose transcript has had no write for agent_idle_min (and no open tool call within the step
    limit): [{agent_id, task_id, minutes}] (#195)."""
    at = at or datetime.now(timezone.utc)
    return [{"agent_id": g["agent_id"], "task_id": g["task_id"], "minutes": int(S.transcript_idle_min(g, at))}
            for g, st in _agent_states(c, cfg, at) if st == "idle"]


def idle_note(x):
    """The label for an idle agent, shared by status and resume."""
    return f"no activity {_dur(x['minutes'])}; owner session may be gone"


def active_agents(c, cfg, at=None):
    """Running agents a session restart would orphan, for the restart warning (#195): those with recent transcript
    activity or an open tool call within the step limit by id, those without a readable transcript as
    'id (unknown activity)'."""
    at = at or datetime.now(timezone.utc)
    return [g["agent_id"] + (" (unknown activity)" if st == "unknown" else "")
            for g, st in _agent_states(c, cfg, at) if st != "idle"]


def orphan_task_flags(c, cfg, at=None):
    """no_live_agent anomaly dicts (reports#136): a claimed/in_progress task with no running agent whose newest
    task-log line is older than no_agent_min. sig = that line's time, so an ack lapses at the next log line."""
    at = at or datetime.now(timezone.utc)
    res = []
    for t in c.execute("SELECT id, title, status, updated FROM tasks WHERE status IN ('claimed','in_progress') "
                       "AND NOT EXISTS (SELECT 1 FROM agents a WHERE a.task_id=tasks.id AND a.ended='') ORDER BY id"):
        last = c.execute("SELECT MAX(ts) FROM task_log WHERE task_id=?", (t["id"],)).fetchone()[0] or t["updated"]
        age = _age(last, at)
        if age >= cfg["no_agent_min"]:
            res.append({"kind": "no_live_agent", "key": f"no_live_agent:{t['id']}", "role": "lead",
                        "text": f"#{t['id']:03d} {t['status']}, no live agent, last log {last} ({_dur(age)}): "
                                f"{t['title']} (no live agent; set status or unclaim)",
                        "cmd": client_cmd(f"python -m aesir status ack no_live_agent:{t['id']}"), "sig": last,
                        "task": t["id"], "status": t["status"], "last_log": last, "minutes": int(age)})
    return res


def _merged_by_merge_commit(root, branch):
    """True when the branch tip is merged into main and is not on main's own first-parent line: the work came in as a
    side of a merge. A branch cut from a main that moved on, never committed to, has its tip on that line (and a
    fast-forward merge leaves it there too, which is why such a task is not auto-reported)."""
    try:
        if not worktrees.merged(root, branch):
            return False
        tip = queue._git(root, "rev-parse", branch, check=False).stdout.strip()
        line = queue._git(root, "rev-list", "--first-parent", "main", check=False).stdout.split()
    except (OSError, AesirError):
        return False
    return bool(tip) and tip not in line


def done_task_flags(c, root, cfg, at=None):
    """done_task anomaly dicts (#246, reports#136 case 3): a claimed/in_progress task with no running agent, at least one
    ended agent, and a branch merged into main by a merge commit (_merged_by_merge_commit: a branch cut from main and
    never committed to is also an ancestor of main) whose newest commit is no older than its first agent's start. Not
    moved automatically: only the requester may verify, so the line names the requester. sig = the branch commit time."""
    res = []
    for t in c.execute("SELECT id, title, status, branch, requester FROM tasks WHERE status IN ('claimed','in_progress') AND branch!='' "
                       "AND NOT EXISTS (SELECT 1 FROM agents a WHERE a.task_id=tasks.id AND a.ended='') "
                       "AND EXISTS (SELECT 1 FROM agents a WHERE a.task_id=tasks.id) ORDER BY id"):
        first = c.execute("SELECT MIN(started) FROM agents WHERE task_id=?", (t["id"],)).fetchone()[0]
        commit = _branch_commit(root, t["branch"])
        if not commit or commit < first or not _merged_by_merge_commit(root, t["branch"]):
            continue
        res.append({"kind": "done_task", "key": f"done_task:{t['id']}", "role": "lead",
                    "text": f"#{t['id']:03d} still {t['status']} but its agent ended and branch {t['branch']} is merged "
                            f"into main: {t['title']}",
                    "cmd": f"requester {t['requester']} closes it: " + client_cmd(
                        f"python -m aesir task set {t['id']} --status verified --reason \"branch {t['branch']} merged into main\""),
                    "sig": commit, "task": t["id"], "status": t["status"], "last_activity": commit, "minutes": 0})
    return res


def task_flags(c, root, cfg, at=None):
    """stalled_task + no_live_agent + done_task flags in one list; a done task's other lines are dropped (one line, one fix)."""
    done = done_task_flags(c, root, cfg, at)
    skip = {x["task"] for x in done}
    return done + [x for x in stall_flags(c, root, cfg, at) + orphan_task_flags(c, cfg, at) if x["task"] not in skip]


def stale_def_flags(c, root, role=None, written=None):
    """stale_agent_defs anomaly dicts (#125): a persistent role whose session started (its recorded checkin, taken at
    session start) before the agent defs on disk were last written still runs the old defs; Claude Code reads them at
    session start. `written` overrides the defs' mtime (tests); sig = that time, so an ack lapses at the next re-render."""
    written = written if written is not None else install.defs_written(root)
    res = []
    if not written:
        return res
    for r in c.execute("SELECT role, last_checkin FROM roles WHERE last_checkin!='' ORDER BY role"):
        if r["role"] in install.PERSISTENT_ROLES and role in (None, r["role"]) and written > r["last_checkin"]:
            res.append({"kind": "stale_agent_defs", "key": f"stale_agent_defs:{r['role']}", "role": r["role"],
                        "text": f"{r['role']} session started {r['last_checkin']}, before the agent defs were last written "
                                f"({written}); it runs the old defs. Write a handoff (`python -m aesir --as {r['role']} "
                                f"role note {r['role']} \"<handoff>\"`), then restart the session and run `resume`",
                        "cmd": client_cmd(f"python -m aesir status ack stale_agent_defs:{r['role']}"), "sig": written})
    return res


def skew_flags(c):
    """schema_skew anomaly dicts (#178): hook code older than the db. Two sources: skew the old code's hook recorded
    (names the agent or session and the code path), and running agents whose worktree code has fewer migrations than
    the db (seen from here, so it works even when the old hook predates the recording). Fix: re-spin the worktree."""
    res = []
    live = {a for a, _, _, _ in skew.worktree_flags(c)}
    for who, p in skew.flags(c):
        res.append({"kind": "schema_skew", "key": f"schema_skew:{who}", "role": "lead", "sig": p.get("db_schema"),
                    "text": f"{who} ran hook code {p.get('code_path')} at schema {p.get('code_schema')} against a db at "
                            f"schema {p.get('db_schema')}: its guards did not run. Re-spin its worktree from the current "
                            f"main head",
                    "cmd": client_cmd(f"python -m aesir status ack schema_skew:{who}"), "agent": who,
                    "code_path": p.get("code_path"), "code_schema": p.get("code_schema"), "db_schema": p.get("db_schema")})
    for a, wt, n, v in skew.worktree_flags(c):
        if any(x["key"] == f"schema_skew:{a}" for x in res):
            continue
        res.append({"kind": "schema_skew", "key": f"schema_skew:{a}", "role": "lead", "sig": v,
                    "text": f"agent {a}: worktree {wt} has Aesir code with {n} migrations, the db is at schema {v}; its "
                            f"hooks run old code and do nothing. Re-spin the worktree from the current main head",
                    "cmd": client_cmd(f"python -m aesir agent end {a}"), "agent": a, "code_path": wt,
                    "code_schema": n, "db_schema": v})
    return res


def worktree_rows(c, root):
    """worktrees.scan rows for the worktrees of merged/verified/wontfix tasks, the only ones the report can show (#272);
    [] on a git failure. Taken once per collect and shared by stale_worktrees and wontfix_worktrees."""
    from .. import worktrees
    names = {r["branch"] for r in c.execute(
        "SELECT branch FROM tasks WHERE status IN ('merged','verified','wontfix') AND branch!=''")}
    try:
        return worktrees.scan(c, root, branches=names) if names else []
    except (AesirError, OSError):
        return []


def stale_worktrees(c, root, rows=None):
    """[(task id, path)] of worktrees `worktrees prune` would remove whose task is merged, verified or wontfix (#164).
    Clean, unlocked and agent-free is already decided by worktrees.scan; a git failure shows nothing, not a crash.
    rows: worktree_rows already taken (None: scan here)."""
    rows = [r for r in (worktree_rows(c, root) if rows is None else rows) if r["action"] == "remove" and r["branch"]]
    done = {r["branch"]: r["id"] for r in c.execute(
        "SELECT id, branch FROM tasks WHERE status IN ('merged','verified','wontfix') AND branch!=''")}
    return [(done[r["branch"]], r["path"]) for r in rows if r["branch"] in done]


def wontfix_worktrees(c, root, rows=None):
    """[(task id, path)] of clean, unlocked, agent-free worktrees of wontfix tasks whose branch was never merged:
    `worktrees prune` keeps them ("branch not merged"), so they need `git worktree remove` (#164). rows: as above."""
    rows = [r for r in (worktree_rows(c, root) if rows is None else rows)
            if r["why"] == "branch not merged" and r["branch"]]
    wf = {r["branch"]: r["id"] for r in c.execute("SELECT id, branch FROM tasks WHERE status='wontfix' AND branch!=''")}
    return [(wf[r["branch"]], r["path"]) for r in rows if r["branch"] in wf]


def low_disk_flags(root, free=None):
    """low_disk anomaly dicts (#213): free space on the worktrees' drive under [worktrees] min_free_gb (default 10).
    `free` overrides the measured bytes (tests). sig is constant so an ack holds while the disk stays low."""
    from .. import worktrees
    try:
        limit = worktrees.load_config(root)["min_free_gb"] * worktrees.GB
        free = worktrees.free_bytes(root) if free is None else free
    except (AesirError, OSError):
        return []
    if free >= limit:
        return []
    return [{"kind": "low_disk", "key": "low_disk", "role": "lead", "sig": "low",
             "text": client_cmd(f"only {worktrees.human(free)} free on the worktrees' drive (limit "
                                f"{worktrees.human(int(limit))}); free space with `python -m aesir worktrees prune` (merged "
                                f"or pushed worktrees) and `python -m aesir worktrees clean-cache` (build caches of the rest)"),
             "cmd": client_cmd("python -m aesir worktrees prune --dry-run"), "free": free, "limit": int(limit)}]


def collect(c, root, cfg, at=None, db_arg=None):
    at = at or datetime.now(timezone.utc)
    an = []

    def flag(kind, text, cmd, role, ident, sig, **kw):
        """key (kind:ident) names the anomaly for `status ack`; role acts; cmd resolves; sig = the state an ack covers."""
        an.append({"kind": kind, "key": f"{kind}:{ident}" if ident != "" else kind, "role": role, "text": text,
                   "cmd": client_cmd(cmd), "sig": str(sig), **kw})

    an.extend(task_flags(c, root, cfg, at))
    an.extend(stale_def_flags(c, root))
    an.extend(rounds.status_flags(c))  # #156: stuck supervised rounds
    an.extend(skew_flags(c))
    an.extend(low_disk_flags(root))
    queued = {r[0] for r in c.execute("SELECT task_id FROM merge_queue")}
    limit = {"in_review": cfg["in_review_min"], "approved": cfg["approved_min"],
             "changes_requested": cfg["changes_requested_min"]}
    for t in c.execute("SELECT id, title, status, updated FROM tasks WHERE status IN (?,?,?) ORDER BY id", STUCK):
        age = _age(t["updated"], at)
        if age < limit[t["status"]] or (t["status"] == "approved" and t["id"] in queued):
            continue
        what = "approved, not queued" if t["status"] == "approved" else t["status"]
        flag("stuck_task", f"#{t['id']:03d} {what} for {_dur(age)}: {t['title']}", f"python -m aesir task show {t['id']}",
             "lead", t["id"], t["status"], task=t["id"], status=t["status"], minutes=int(age))
    idle = {x["agent_id"]: x for x in idle_agents(c, cfg, at)}
    for g in c.execute("SELECT agent_id, task_id, started, bound FROM agents WHERE ended='' ORDER BY id"):
        age = _age(g["started"], at)
        tid = f" (task #{g['task_id']:03d})" if g["task_id"] else ""
        if g["agent_id"] in idle:
            x = idle[g["agent_id"]]
            flag("idle_agent", f"agent {g['agent_id']}{tid} {idle_note(x)}", f"python -m aesir agent end {g['agent_id']}",
                 "lead", g["agent_id"], "idle", agent=g["agent_id"], minutes=x["minutes"])
        elif age >= cfg["agent_min"]:
            flag("long_agent", f"agent {g['agent_id']}{tid} running {_dur(age)}", f"python -m aesir agent end {g['agent_id']}",
                 "lead", g["agent_id"], "long", agent=g["agent_id"], minutes=int(age))
        elif not g["bound"] and age >= cfg["unbound_min"]:
            flag("unbound_agent", f"agent {g['agent_id']}{tid} unbound after {_dur(age)}",
                 f"python -m aesir agent end {g['agent_id']}", "lead", g["agent_id"], "unbound",
                 agent=g["agent_id"], minutes=int(age))
    skipped = {json.loads(r[0]).get("task") for r in c.execute(
        "SELECT payload FROM events WHERE entity='friction' AND entity_id='evidence_skip' AND action='record'")}
    for t in c.execute("SELECT * FROM tasks WHERE status IN ('in_review','merged') ORDER BY id"):
        miss = [] if t["id"] in skipped or evidence.predates(c, t) else evidence.lacking(c, dict(t))
        if miss:
            flag("missing_evidence", f"#{t['id']:03d} {t['status']} without evidence for criteria "
                 + ", ".join(map(str, miss)), f"python -m aesir task show {t['id']}", "lead", t["id"], miss,
                 task=t["id"], criteria=miss)
    prefix = session_prefix(root)
    for r in c.execute("SELECT role, session_name FROM roles WHERE session_name!='' ORDER BY role"):
        live = r["session_name"]
        if live in (r["role"], f"{prefix}-{r['role']}"):
            continue
        sug = live[:-len(r["role"]) - 1] if live.endswith("-" + r["role"]) else "<prefix>"
        flag("session_name_mismatch", f"{r['role']} runs as session '{live}' but hints derive '{prefix}-{r['role']}' "
             f"(session_prefix {'set' if prefix_configured(root) else 'unset; derived from [project] name'}); "
             f'set session_prefix = "{sug}" under [project] in aesir.toml', "python -m aesir status ack "
             f"session_name_mismatch:{r['role']}", "lead", r["role"], live, session=live, derived=f"{prefix}-{r['role']}")
    lock = c.execute("SELECT pid, taken FROM queue_lock WHERE id=1").fetchone()
    if lock:
        age = max((at.timestamp() - lock["taken"]) / 60, 0)
        flag("queue_lock", f"queue lock held by pid {lock['pid']} for {_dur(age)}", "python -m aesir queue list",
             "lead", "", lock["pid"], pid=lock["pid"], minutes=int(age))
    try:
        mcfg = messages.load_config(root)
    except AesirError:
        mcfg = messages.DEFAULTS
    for m in c.execute("SELECT * FROM messages m WHERE kind='ask' AND channel!='escalation' AND expired_ts='' AND NOT EXISTS "
                       "(SELECT 1 FROM messages r WHERE r.parent_id=m.id AND r.kind='reply') ORDER BY id"):
        age = _age(m["ts"], at)
        if age >= mcfg["reminder_min"]:
            where = m["task_id"] or ("D%s" % m["decision_id"] if m["decision_id"] else m["channel"])
            flag("unanswered_ask", f"M{m['id']} to {m['recipient']} unanswered for {_dur(age)}",
                 f"python -m aesir thread {where}", m["recipient"], m["id"], m["id"], message=m["id"],
                 to=m["recipient"], minutes=int(age))
    lag = _export_lag(c, root)
    if lag and lag[0]:
        flag("export_drift", f"db has {lag[0]} event(s) newer than aesir-state (the next merge exports them)",
             "python -m aesir export", "lead", "", "", events=lag[0], minutes=int(_age(lag[1], at)))
    try:
        mcfg = metrics.load_config(root)
        rows = metrics.persistent_sessions(mcfg)  # one transcript scan for the warnings and the sessions list (#272)
        warns = metrics.session_warnings(mcfg, at=at.strftime(FMT), active=active_agents(c, cfg, at), rows=rows)
        sessions = [{k: r[k] for k in ("role", "startup", "current")}
                    for r in metrics.current_sessions(mcfg, at.strftime(FMT), rows=rows)]
    except AesirError:
        warns, sessions = [], []
    for w in warns:
        flag("session_large", w["text"], f"python -m aesir --as {w['role']} role note {w['role']} \"<handoff>\"",
             w["role"], w["role"], w["session"], current=w["current"], limit=w["limit"])
    try:
        fcfg = friction.load_config(root)
    except AesirError:
        fcfg = friction.DEFAULTS
    nc = [p.get("agent") for _, p in friction._events(c, "friction", "record", "") if p.get("type") == "no_context"]
    if nc:  # #128: agents that started without their spawn context; an ack lapses when another one happens
        flag("no_context_start", f"{len(nc)} agent(s) started without spawn context (latest: "
             + ", ".join(dict.fromkeys(reversed(nc[-5:]))) + "): paste `spawn-context` output into the spawn prompt",
             "python -m aesir status ack no_context_start", "lead", "", len(nc), count=len(nc), agents=nc[-5:])
    for x in friction.status_flags(c, fcfg):
        flag("recurring_friction", f"friction x{x['count']} {x['key']} has no linked task or tip",
             f"python -m aesir friction link '{x['key']}' task:<id>", "lead", x["key"], x["count"],
             friction=x["key"], count=x["count"])
    for x in friction.triage_flags(c, fcfg):
        flag("recurring_cause", f"cause x{x['count']} {x['type']} | {x['command'] or '-'} | {x['class']} has no linked task or tip",
             f"python -m aesir friction link '{x['key']}' task:<id>", "lead", x["key"], "recurring",
             friction=x["key"], count=x["count"])
    try:
        tcfg = tips.load_config(root)
    except AesirError:
        tcfg = tips.DEFAULTS
    for x in tips.report(c, tcfg, fcfg)["candidates"]:
        flag("promotion_candidate", f"tip T{x['tip']} surfaced x{x['impressions']} but friction {x['key']} still recurs",
             f"python -m aesir tip promote {x['tip']} core <role> <section>", "lead", x["tip"], x["key"],
             tip=x["tip"], friction=x["key"])
    for n, title in upstream_pickup.unlinked_open(c, root, db_arg):
        flag("unlinked_upstream", f"upstream issue #{n} not linked to a task: {title}",
             f"python -m aesir upstream link {n} <task>", "lead", n, "", issue=n)
    pend = upstream.pending_count(db.db_path(db_arg, root))
    if pend:
        flag("pending_upstream", f"{pend} upstream report(s) queued locally (gh failed)", "python -m aesir upstream retry",
             "lead", "", pend, count=pend)
    ahead = _unpushed(root)
    if ahead:
        flag("unpushed_main", f"main is {ahead} commit(s) ahead of origin", "python -m aesir queue run", "lead", "", ahead,
             commits=ahead)
    wt_rows = worktree_rows(c, root)  # one scan for both worktree sections (#272)
    stale = stale_worktrees(c, root, wt_rows)
    if stale:  # #164: finished tasks whose clean, unlocked worktree is still on disk
        flag("stale_worktrees", f"{len(stale)} clean worktree(s) of finished tasks: "
             + ", ".join(f"#{i:03d}" for i in sorted({x[0] for x in stale})), "python -m aesir worktrees prune", "lead", "",
             len(stale), count=len(stale), paths=[x[1] for x in stale])
    wont = wontfix_worktrees(c, root, wt_rows)
    if wont:  # #164: unmerged, so prune keeps them; each needs an explicit remove
        flag("wontfix_worktrees", f"{len(wont)} clean worktree(s) of wontfix tasks: "
             + ", ".join(f"#{i:03d}" for i in sorted({x[0] for x in wont})), f"git worktree remove {wont[0][1]}", "lead", "",
             len(wont), count=len(wont), paths=[x[1] for x in wont])
    by_req = {}
    for t in c.execute("SELECT id, requester FROM tasks WHERE status='merged' ORDER BY id"):
        by_req.setdefault(t["requester"] or "-", []).append(t["id"])
    for req, ids in by_req.items():
        flag("awaiting_verification", f"merged, awaiting verification by {req}: " + ", ".join(f"#{i:03d}" for i in ids),
             f"python -m aesir task show {ids[0]}", req if req in ("advisor", "lead") else "advisor", req, ids,
             requester=req, tasks=ids)
    try:
        stale_h = ownerwait.load_config(root)["stale_hours"]
    except AesirError:
        stale_h = 24
    for w in ownerwait.stale(c, stale_h):
        ref = f"#{w['ref']:03d}" if w["kind"] == "task" else f"D{w['ref']}"
        why = f"open {_dur(w['seconds'] // 60)}" if w["why"] == "old" else f"task already {w['why']}"
        flag("stale_owner_wait", f"owner wait on {ref} is stale ({why}): {w['question']}",
             f"python -m aesir owner answer {w['kind']} {w['ref']}", w["role"], f"{w['kind']}{w['ref']}",
             w["started"], waiting_on=w["kind"], ref=w["ref"], minutes=w["seconds"] // 60)
    counts = {
        "tasks": {r[0]: r[1] for r in c.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status ORDER BY status")},
        "agents_running": c.execute("SELECT COUNT(*) FROM agents WHERE ended=''").fetchone()[0],
        "queued": len(queued),
        "decisions": c.execute("SELECT COUNT(*) FROM decisions").fetchone()[0],
        "tips": c.execute("SELECT COUNT(*) FROM tips").fetchone()[0],
        "practices": _practices(c, root),
        "baselined": c.execute("SELECT COUNT(*) FROM findings WHERE status='baselined'").fetchone()[0],
        "rank": rank.status_line(c, root),
        "long_runs": [{**{k: r[k] for k in ("id", "profile", "status", "task_id", "started", "ended", "sha")},
                       **({"last_line": longrun.last_line(c, r)} if r["status"] == "running" else {})}
                      for r in longrun.listing(c, 3)],  # running first, then the latest finished
        "waiting": taskorder.waiting(c),  # open/claimed/in_progress tasks behind an unfinished predecessor (#176)
        "scheduled": [{k: r[k] for k in ("id", "fire_at", "command", "actor")} for r in sched.armed(c)],  # armed `at` runs (#139)
        "sessions": sessions,  # always shown, not only past the warning: true startup vs current context
    }
    d = {"anomalies": an, "counts": counts}
    note = releases.notice(root, db_arg)
    if note:
        d["release"] = note
    return d


def sweep_unbound(c, actor, cfg, at=None):
    """Auto-resolve: end agents still unbound after unbound_end_min (their bind never happened). Returns the ids."""
    at = at or datetime.now(timezone.utc)
    done = []
    for g in c.execute("SELECT role, agent_id, started FROM agents WHERE ended='' AND bound='' ORDER BY id").fetchall():
        if _age(g["started"], at) >= cfg["unbound_end_min"]:
            S.agent_end(c, actor, g["role"], g["agent_id"], "stale: never bound, ended by status sweep")
            done.append(g["agent_id"])
    return done


def _episodes(c):
    """Per anomaly key: the open episode (see acks.episodes)."""
    return acks.episodes(c)


def unacked_stalls(c, root, cfg, role):
    """Stalled tasks for `role` not covered by an ack (resume's read-only view; no episode bookkeeping)."""
    st = _episodes(c)
    last = acks.last_event_id(c)
    res = []
    for x in task_flags(c, root, cfg):
        a = (st.get(x["key"]) or {}).get("ack")
        if x["role"] == role and not acks.holds(a, x["sig"], last):
            res.append(x)
    return res


OLD_KINDS = ("stalled_task", "no_live_agent")  # the stall lines an age cut-off applies to; done_task never ages out


def is_old_stall(x, days):
    """True for a stalled_task / no_live_agent anomaly idle `days` or more (minutes >= days * 1440); done_task never is."""
    return x["kind"] in OLD_KINDS and x["minutes"] >= days * 1440


def ack_older(c, actor, root, cfg, days, db_arg=None):
    """One-time `status ack --older-than <days>` (#246): ack every stalled_task / no_live_agent anomaly idle that long.
    Each ack lapses at the task's next activity, like a single ack. Returns the acked keys."""
    d = collect(c, root, cfg, db_arg=db_arg)
    old = [x for x in d["anomalies"] if is_old_stall(x, days)]
    reconcile(c, actor, d, cfg, escalate=False)  # opens the episodes status never saw
    for x in old:
        _note(c, actor, x["key"], "ack", {"sig": x["sig"], "until": None})
    return [x["key"] for x in old]


def ack_done_shown(c, actor, flags):
    """Resume lists a done_task once (#246): ack it on showing, so it returns only if the branch gets a new commit."""
    for x in flags:
        _note(c, actor, x["key"], "ack", {"sig": x["sig"], "until": None})


def _note(c, actor, key, action, payload=None):
    with db.tx(c):
        db.event(c, actor, "anomaly", key, action, payload)


def reconcile(c, actor, d, cfg, at=None, escalate=True):
    """Track first-seen, close episodes of anomalies that ended (dropping their acks), escalate once an anomaly has
    been shown for escalate_min (since status first saw it), and split d['anomalies'] into visible and d['hidden'] (acked or escalated)."""
    at = at or datetime.now(timezone.utc)
    st = _episodes(c)
    last = acks.last_event_id(c)
    for k in set(st) - {x["key"] for x in d["anomalies"]}:
        _note(c, actor, k, "clear")
        st.pop(k)
    shown, hidden = [], []
    for x in d["anomalies"]:
        e = st.get(x["key"])
        if e is None:
            _note(c, actor, x["key"], "seen", {"sig": x["sig"]})
            e = st[x["key"]] = {"seen": at.strftime(FMT), "escalated": False, "ack": None}
        ack = e["ack"]
        if acks.holds(ack, x["sig"], last):
            hidden.append({**x, "hidden": "acked", "ack": ack})
        elif e["escalated"]:
            hidden.append({**x, "hidden": "escalated"})
        elif escalate and _age(e["seen"], at) >= cfg["escalate_min"]:
            _escalate(c, actor, x)
            hidden.append({**x, "hidden": "escalated"})
        else:
            shown.append(x)
    d["anomalies"], d["hidden"] = shown, hidden
    return d


def _escalate(c, actor, x):
    """Once per episode: an ask to the named role (lands in its inbox); the anomaly then stops repeating in status."""
    to = x["role"]
    if to in messages.names(c, actor):  # #197: never a self-addressed ask (lead->lead); the other persistent role gets it
        to = "lead" if to == "advisor" else "advisor"
    with db.tx(c):
        messages._insert(c, actor, "ask", f"Escalation: {x['text']}. Resolve: {x['cmd']} -- or `status ack {x['key']}` "
                         "and reply.", to, channel="escalation")
        db.event(c, actor, "anomaly", x["key"], "escalate", {"role": x["role"]})


def render(d):
    an = d["anomalies"]
    print(f"anomalies ({len(an)}):" if an else "anomalies: none")
    for x in an:
        print(f"  ! [{x['role']}] {x['text']}  -> {x['cmd']}  (ack key: {x['key']})")
    h = d.get("hidden", [])
    if h:
        n = sum(1 for x in h if x["hidden"] == "acked")
        print(client_cmd(f"hidden: {n} acked, {len(h) - n} escalated (python -m aesir status --all)"))
    k = d["counts"]
    print("tasks: " + (", ".join(f"{n} {s}" for s, n in k["tasks"].items()) or "none"))
    print(f"agents running: {k['agents_running']}  queued: {k['queued']}  decisions: {k['decisions']}  tips: {k['tips']}")
    for tid, w in (k.get("waiting") or {}).items():
        print(f"  #{int(tid):03d} {w}")
    pr = k.get("practices", {})
    if pr:
        names = lambda st: ", ".join(x for x, v in pr.items() if v == st) or "none"
        print(f"practices: default-on {names('default')}; adopted {names('adopted')}; "
              f"{sum(v == 'unadopted' for v in pr.values())} not adopted (docs/practices.md)")
    if k.get("baselined"):
        print(client_cmd(f"baselined findings: {k['baselined']} (python -m aesir finding digest --status baselined --by rule)"))
    if k.get("rank"):
        print(k["rank"])
    if d.get("owner_waiting"):
        print(f"waiting on the owner ({len(d['owner_waiting'])}):")
        ownerwait.render_waiting(d["owner_waiting"])
    lr = k.get("long_runs", [])
    if lr:
        print("long runs: " + "; ".join(
            f"L{x['id']} {x['profile']} {x['status']}" + (f" {_dur(_age(x['started'], datetime.now(timezone.utc)))}"
                                                           if x["status"] == "running" else "")
            + (f" (task #{x['task_id']:03d})" if x["task_id"] else "")
            + (f" last: {x['last_line'][:80]}" if x.get("last_line") else "") for x in lr))
    if k.get("scheduled"):
        print(f"scheduled runs armed ({len(k['scheduled'])}): " + "; ".join(
            f"S{x['id']} {x['fire_at']} {x['command'][:60]} (by {x['actor']})" for x in k["scheduled"])
            + client_cmd(" -- cancel: python -m aesir at cancel <id>"))
    if k.get("sessions"):
        print("sessions: " + "; ".join(f"{x['role']} startup {x['startup']} current {x['current']}"
                                       for x in k["sessions"]))
    if d.get("release"):
        print(d["release"])


def ack(c, actor, root, cfg, key, until=None, db_arg=None):
    d = collect(c, root, cfg, db_arg=db_arg)
    x = next((x for x in d["anomalies"] if x["key"] == key), None)
    if not x:
        raise AesirError(f"no such anomaly: {key}; current keys: " + (", ".join(y["key"] for y in d["anomalies"]) or "none"))
    if until is not None and until <= c.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]:
        raise AesirError(f"--until event {until} already happened")
    reconcile(c, actor, d, cfg, escalate=False)  # opens the episode if status never saw it
    _note(c, actor, key, "ack", {"sig": x["sig"], "until": until})


def status_cmd(c, a):
    root = db.project_root()
    cfg = load_config(root)
    actor = a.actor or "system"
    if a.words:
        if a.words[0] == "ack" and len(a.words) == 1 and a.older_than is not None:
            keys = ack_older(c, actor, root, cfg, a.older_than, a.db)
            print(f"acked {len({k.split(':')[1] for k in keys})} stalled tasks idle {a.older_than:g}+ days (each lapses at the task's next activity)")
            return
        if a.words[0] != "ack" or len(a.words) != 2 or a.older_than is not None:
            raise AesirError("usage: status [--all] | status ack <anomaly key> [--until <event id>] | status ack --older-than <days>")
        ack(c, actor, root, cfg, a.words[1], a.until, a.db)
        print(f"acked {a.words[1]}" + (f" until event {a.until}" if a.until else " until its state changes"))
        return
    sweep_unbound(c, actor, cfg)
    S.end_finished_agents(c, actor, cfg["agent_idle_min"], orphan_min=cfg["no_agent_min"])
    d = reconcile(c, actor, collect(c, root, cfg, db_arg=a.db), cfg)
    if not d["hidden"]:
        del d["hidden"]
    elif a.all:
        d["anomalies"] = d["anomalies"] + d["hidden"]
    d["owner_waiting"] = ownerwait.open_waits(c)
    out(a, d, render)
    hint(a, d["anomalies"][0]["cmd"] if d["anomalies"] else None, "python -m aesir task list", "python -m aesir inbox")


def register(sub):
    add(sub, "status", status_cmd, ("words", {"nargs": "*", "help": "ack <anomaly key>"}),
        ("--until", {"type": int, "help": "ack: event id after which the ack lapses"}),
        ("--older-than", {"type": float, "help": "ack (no key): all stalled tasks idle at least this many days"}),
        ("--all", {"action": "store_true", "help": "also list acked and escalated anomalies"}))
