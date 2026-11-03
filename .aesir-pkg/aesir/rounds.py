"""Supervised rounds (#156, D163): Aesir holds the state of a running round (task, branch, workflow/agents, started, wall-clock
limit, last progress seen) and `round check` reads and updates it, so nothing lives only in a cron prompt or session memory.

Progress = any configured signal changed since the previous check ([rounds] progress in aesir.toml; default commit +
evidence + agent; `testlog:<glob>` adds the newest mtime among files matching the glob in the branch's worktree; `agent`
is the newest mtime among the transcripts of the round's agents, so it moves whenever any of them writes). A round is stuck
when no progress has been seen for IDLE_LIMIT x [rounds] interval of wall-clock time (from last progress, or from arming;
how often `check` ran does not matter, #167) or when past its wall-clock limit, except that while one of its running agents has an open
tool call (a tool_use in its transcript with no tool_result yet: a test suite or gate is running and writes nothing) younger
than the step limit ([rounds] step_limit, a duration, default 30m) the idle rule does not fire; an open
call older than the limit counts as idle and is named in the stuck message. Entering stuck sets a `round_stuck` status
anomaly and asks the lead. Aesir never stops a round. `watch` is the Monitor-ready loop: one line per round per pass, never blocks beyond
its own loop. Every check reads the clock first (`at` is injectable for tests)."""
import glob as _glob
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import gates, messages, proc
from . import service as S
from .db import event, now, tx
from .queue import _worktree_of
from .service import AesirError

DEFAULTS = {"progress": ["commit", "evidence", "agent"], "interval": 180, "wall_limit": "45m", "longest_step": "", "step_limit": "",
            "end_markers": [r"^OK", r"^Ran \d+ tests", r"gate \w+ PASS"],
            "failure_markers": [r"^FAILED", "Traceback", r"gate \w+ FAIL"]}
STEP_DEFAULT = "30m"  # an open tool call may run this long before it stops excusing idleness
IDLE_LIMIT = 2  # [rounds] intervals without progress before a round is stuck
ENDED_TASK = ("merged", "verified", "wontfix")
FMT = "%Y-%m-%dT%H:%M:%SZ"
MAX_DEFAULT = 1740  # seconds; just under the Monitor 30-minute cap


def load_config(root):
    """[rounds] in aesir.toml merged over DEFAULTS."""
    try:
        sec = gates.load_config(root).get("rounds", {})
    except AesirError as e:
        if "no aesir.toml" not in str(e):
            raise
        sec = {}
    cfg = {**DEFAULTS, **sec}
    p = cfg["progress"]
    if not isinstance(p, list) or not p or not all(isinstance(x, str) and (x in ("commit", "evidence", "agent") or (
            x.startswith("testlog:") and len(x) > 8)) for x in p):
        raise AesirError('bad aesir.toml: [rounds] progress must be a list of "commit", "evidence", "agent", "testlog:<glob>"')
    if isinstance(cfg["interval"], bool) or not isinstance(cfg["interval"], (int, float)) or cfg["interval"] <= 0:
        raise AesirError("bad aesir.toml: [rounds] interval must be seconds > 0")
    for k in ("end_markers", "failure_markers"):
        if isinstance(cfg[k], str):
            cfg[k] = [cfg[k]]
        if not isinstance(cfg[k], list) or not all(isinstance(x, str) for x in cfg[k]):
            raise AesirError(f"bad aesir.toml: [rounds] {k} must be a list of regex strings")
    S.parse_duration(cfg["wall_limit"])
    S.parse_duration(cfg["step_limit"] or STEP_DEFAULT)
    return cfg


def _ts(s):
    return datetime.strptime(s, FMT).replace(tzinfo=timezone.utc).timestamp() if s else 0.0


def _fmt(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime(FMT)


def _age(secs):
    secs = int(max(secs, 0))
    return f"{secs // 3600}h{secs % 3600 // 60:02d}m" if secs >= 3600 else f"{secs // 60}m{secs % 60:02d}s"


def _git(cwd, *args):
    r = proc.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.stdout.strip() if r.returncode == 0 else ""


def get(conn, rid):
    r = conn.execute("SELECT * FROM rounds WHERE id=?", (int(rid),)).fetchone()
    if not r:
        raise AesirError(f"no such round: {rid}")
    return dict(r)


def live(conn, rid=None):
    """Armed and stuck rounds (or the one given)."""
    if rid is not None:
        return [get(conn, rid)]
    return [dict(r) for r in conn.execute("SELECT * FROM rounds WHERE state!='disarmed' ORDER BY id")]


def label(r):
    return r["label"] or f"#{r['task_id']:03d}"


def arm(conn, actor, root, task, branch="", workflow="", label_="", limit=None, agents=""):
    """Arm supervision for a task's round; an already live round of the task is updated instead. Returns the round id."""
    t = S.get_task(conn, task)
    cfg = load_config(root)
    secs = int(S.parse_duration(limit if limit else cfg["wall_limit"]))
    ids = json.dumps([x.strip() for x in agents.split(",") if x.strip()]) if agents else ""
    with tx(conn):
        cur = conn.execute("SELECT * FROM rounds WHERE task_id=? AND state!='disarmed' ORDER BY id DESC LIMIT 1", (t["id"],)).fetchone()
        if cur:
            # a new branch or workflow is a new run: the old baseline and stuck state no longer describe it
            fresh = (branch and branch != cur["branch"]) or (workflow and workflow != cur["workflow_id"])
            sets = {"branch": branch or cur["branch"] or t["branch"], "workflow_id": workflow or cur["workflow_id"],
                    "label": label_ or cur["label"], "agent_ids": ids or cur["agent_ids"],
                    "wall_limit_s": secs if limit else cur["wall_limit_s"]}
            if fresh:
                sets.update(progress_sig="", idle_checks=0, state="armed", last_progress="")
            conn.execute("UPDATE rounds SET " + ", ".join(f"{k}=?" for k in sets) + " WHERE id=?", [*sets.values(), cur["id"]])
            event(conn, actor, "round", cur["id"], "update", sets)
            return cur["id"]
        rid = conn.execute("INSERT INTO rounds (task_id, label, workflow_id, agent_ids, branch, started, wall_limit_s, actor)"
                           " VALUES (?,?,?,?,?,?,?,?)", (t["id"], label_, workflow, ids, branch or t["branch"], now(), secs,
                                                         actor)).lastrowid
        event(conn, actor, "round", rid, "arm", {"task": t["id"], "branch": branch or t["branch"], "limit_s": secs})
        return rid


def disarm(conn, actor, rid=None, task=None, why="disarm"):
    """Disarm one round (by id) or every live round of a task. Returns the ids disarmed."""
    if (rid is None) == (task is None):
        raise AesirError("usage: round disarm <id> | round disarm --task N")
    rows = [get(conn, rid)] if rid is not None else live_of_task(conn, task)
    done = []
    with tx(conn):
        for r in rows:
            if r["state"] == "disarmed":
                continue
            conn.execute("UPDATE rounds SET state='disarmed', ended=? WHERE id=?", (now(), r["id"]))
            event(conn, actor, "round", r["id"], "disarm", {"why": why})
            done.append(r["id"])
    return done


def live_of_task(conn, task):
    return [dict(r) for r in conn.execute("SELECT * FROM rounds WHERE task_id=? AND state!='disarmed' ORDER BY id",
                                          (int(str(task).lstrip("#")),))]


def worktree(root, r):
    return _worktree_of(root, r["branch"]) if r["branch"] else None


def round_agents(conn, r):
    """Agent rows of the round: its agent_ids, else the task's agents running now plus those started since arming (so an
    agent spawned just before the round was armed still counts)."""
    ids = json.loads(r["agent_ids"]) if r["agent_ids"] else []
    if ids:
        q, args = f"agent_id IN ({','.join('?' * len(ids))})", ids
    else:
        q, args = "task_id=? AND (started>=? OR ended='' OR ended IS NULL)", (r["task_id"], r["started"])
    return [dict(a) for a in conn.execute(f"SELECT * FROM agents WHERE {q} ORDER BY id", args)]


def transcripts(conn, r):
    """Distinct transcript paths of the round's agents that have one."""
    return sorted({a["transcript_path"] for a in round_agents(conn, r) if a["transcript_path"]})


def _record_ts(rec, fallback):
    """Epoch of a transcript record's timestamp, or `fallback` when it has none."""
    try:
        return datetime.fromisoformat(str(rec["timestamp"]).replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError):
        return fallback


def open_calls(path):
    """[(epoch started, 'Tool: what')] for tool calls in a transcript that have no result yet. Reads the harness JSONL:
    an assistant content item type tool_use (id) is open until a user item type tool_result carries its tool_use_id."""
    from .agenthook import _records  # lazy: agenthook is heavy and rounds loads on every status
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return []
    opened, closed = {}, set()
    for rec in _records(path):
        if rec.get("type") not in ("assistant", "user"):
            continue  # api-request-blob records replay earlier tool_use items without a timestamp (#169)
        m = rec.get("message")
        if not isinstance(m, dict) or not isinstance(m.get("content"), list):
            continue
        for item in m["content"]:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "tool_use" and item.get("id") and item["id"] not in closed:
                inp = item.get("input") if isinstance(item.get("input"), dict) else {}
                what = str(inp.get("command") or inp.get("description") or inp.get("prompt") or "").strip().replace("\n", " ")
                opened[item["id"]] = (_record_ts(rec, mtime), f"{item.get('name') or 'tool'}: {what[:80]}".rstrip(": "))
            elif item.get("type") == "tool_result":
                opened.pop(item.get("tool_use_id"), None)
                closed.add(item.get("tool_use_id"))
    return list(opened.values())


def step_limit(cfg):
    """Seconds an open tool call may run before it no longer excuses a silent agent."""
    return S.parse_duration(cfg.get("step_limit") or STEP_DEFAULT)


def running_steps(conn, r, at):
    """[(agent id, command text, age seconds)] of open tool calls in the round's running agents, oldest first."""
    out = []
    for a in round_agents(conn, r):
        if not a["ended"] and a["transcript_path"]:
            out += [(a["agent_id"], what, max(at - ts, 0)) for ts, what in open_calls(a["transcript_path"])]
    return sorted(out, key=lambda x: -x[2])


def signals(conn, root, r, cfg):
    """{signal name: value} for the round: commit sha, newest evidence id, newest mtime per testlog glob."""
    sig = {}
    wt = worktree(root, r)
    for name in cfg["progress"]:
        if name == "commit":
            sig[name] = _git(root, "rev-parse", "--verify", "--quiet", r["branch"] + "^{commit}") if r["branch"] else ""
        elif name == "agent":
            sig[name] = max((os.path.getmtime(p) for p in transcripts(conn, r) if os.path.isfile(p)), default=0)
        elif name == "evidence":
            sig[name] = conn.execute("SELECT COALESCE(MAX(id),0) FROM evidence WHERE task_id=?", (r["task_id"],)).fetchone()[0]
        else:
            base = Path(wt or root)
            files = [p for p in _glob.glob(str(base / name[8:]), recursive=True) if os.path.isfile(p)]
            sig[name] = max((os.path.getmtime(p) for p in files), default=0)
    return sig


def check_round(conn, actor, root, r, cfg, at):
    """One check of one live round at clock `at` (epoch seconds, read by the caller first). Returns the updated row."""
    task = S.get_task(conn, r["task_id"])
    if task["status"] in ENDED_TASK:
        disarm(conn, actor, r["id"], why=f"task {task['status']}")
        return get(conn, r["id"])
    sig = signals(conn, root, r, cfg)
    prev = json.loads(r["progress_sig"]) if r["progress_sig"] else None
    progress = prev is None or sig != prev  # the first check is the baseline
    idle = 0 if progress else r["idle_checks"] + 1
    steps, limit = running_steps(conn, r, at), step_limit(cfg)
    busy = [x for x in steps if x[2] <= limit]  # a command still within its allowance: silence is expected
    late = [x for x in steps if x[2] > limit]
    last = _fmt(at) if progress else r["last_progress"]
    over = at - _ts(r["started"]) > r["wall_limit_s"]
    quiet = at - _ts(last or r["started"])  # wall-clock silence, whatever the number of checks
    idle_stuck = quiet >= IDLE_LIMIT * cfg["interval"] and not busy
    stuck = idle_stuck or over
    state = "stuck" if stuck else "armed"
    why = []
    if idle_stuck:
        why.append(f"no progress for {_age(quiet)} (last progress {last or r['started']})")
        if late:
            a, what, age = late[0]
            why.append(f"{a} has had `{what}` open for {_age(age)}, past the step limit {_age(limit)}")
    if over:
        why.append(f"past wall-clock limit {_age(r['wall_limit_s'])} (running {_age(at - _ts(r['started']))})")
    with tx(conn):
        conn.execute("UPDATE rounds SET last_check=?, last_progress=?, progress_sig=?, idle_checks=?, state=? WHERE id=?",
                     (_fmt(at), last, json.dumps(sig), idle, state, r["id"]))
        if stuck and r["state"] != "stuck":
            event(conn, actor, "round", r["id"], "stuck", {"why": why, "signals": sig})
    if stuck and r["state"] != "stuck":  # message once per stuck episode, with the evidence
        messages.ask(conn, actor, r["task_id"], "lead",
                     f"round {r['id']} ({label(r)}) looks stuck: {'; '.join(why)}. Signals: {json.dumps(sig)}. "
                     f"Check it (`python -m aesir round check {r['id']}`); Aesir does not stop rounds.")
    return get(conn, r["id"])


def check(conn, actor, root, rid=None, at=None):
    """Check one round or every live round. Reads the clock first. Returns the updated rows."""
    at = time.time() if at is None else at  # clock first
    cfg = load_config(root)
    return [check_round(conn, actor, root, r, cfg, at) for r in live(conn, rid)]


def view(conn, root, r, cfg, at):
    """Read-only text block for one live round: no row update, no ask (unlike `check`)."""
    since = r["last_progress"] or r["started"]
    running = len([a for a in round_agents(conn, r) if not a["ended"]])
    lines = [f"round {r['id']} {label(r)} task #{r['task_id']:03d} {r['state']} (read-only view, nothing recorded)",
             f"  last progress {since}, idle {_age(at - _ts(since))} of {_age(IDLE_LIMIT * cfg['interval'])} allowed; "
             f"last check {r['last_check'] or '-'}",
             f"  live tokens {token_text(conn, r)}, {running} agent(s) running"]
    lines += [f"  open command {a}: {what} ({_age(age)})" for a, what, age in running_steps(conn, r, at)] or ["  open command: none"]
    return "\n".join(lines)


def summary(r):
    return (f"round {r['id']} {label(r)} {r['state']} idle={r['idle_checks']} last_progress={r['last_progress'] or '-'} "
            f"checked={r['last_check'] or '-'}")


def status_flags(conn, root=None):
    """round_stuck anomaly dicts for `status`; sig = last progress, so an ack lapses once the round moves."""
    from .invoke import cmd as client_cmd
    res = []
    for r in conn.execute("SELECT * FROM rounds WHERE state='stuck' ORDER BY id"):
        res.append({"kind": "round_stuck", "key": f"round_stuck:{r['id']}", "role": "lead",
                    "text": f"round {r['id']} ({label(r)}, task #{r['task_id']:03d}) is stuck: idle {r['idle_checks']} check(s), "
                            f"last progress {r['last_progress'] or r['started']}, limit {_age(r['wall_limit_s'])}",
                    "cmd": client_cmd(f"python -m aesir round check {r['id']}"), "sig": r["last_progress"] or r["started"],
                    "round": r["id"], "task": r["task_id"]})
    return res


# ---- watch: one line per round per pass ----

def token_parts(conn, r):
    """(output tokens, context processed) over the round's agent rows; context = input + cache read + cache create. A
    running agent's row only gets its totals at stop, so its transcript is read for the live figure (#165, #169)."""
    from .agenthook import transcript_usage
    out = ctx = 0
    for a in round_agents(conn, r):
        if not a["ended"] and a["transcript_path"] and os.path.isfile(a["transcript_path"]):
            u = transcript_usage(a["transcript_path"])[0]
            out += u["output"]
            ctx += u["input"] + u["cache_read"] + u["cache_create"]
        else:
            out += a["tokens_out"] or 0
            ctx += sum(a[k] or 0 for k in ("tokens_in", "tokens_cache_read", "tokens_cache_create"))
    return out, ctx


def tokens(conn, r):
    """Sum of in+out tokens over the round's agent rows (see token_parts for the cache-aware figure)."""
    from .agenthook import transcript_usage
    total = 0
    for a in round_agents(conn, r):
        if not a["ended"] and a["transcript_path"] and os.path.isfile(a["transcript_path"]):
            u = transcript_usage(a["transcript_path"])[0]
            total += u["input"] + u["output"]
        else:
            total += a["tokens_in"] + a["tokens_out"]
    return total


def _compact(n):
    return str(n) if n < 1000 else f"{n / 1000:.1f}k" if n < 1_000_000 else f"{n / 1_000_000:.1f}M"


def token_text(conn, r):
    """'out 3.2k, ctx 1.4M': exact output tokens plus context processed; moves while an agent works."""
    out, ctx = token_parts(conn, r)
    return f"out {_compact(out)}, ctx {_compact(ctx)}"


def daily_pct():
    p = Path(os.environ.get("AESIR_USAGE_FILE") or Path.home() / ".claude" / "statusline-usage.json")
    try:
        v = json.loads(p.read_text(encoding="utf-8"))["daily_budget_pct"]
        return f"{v:g}%" if isinstance(v, (int, float)) and not isinstance(v, bool) else "-"
    except (OSError, ValueError, KeyError, TypeError):
        return "-"


def longest_step(cfg, cwd):
    """First stdout line of the project's [rounds] longest_step command ('' when unset or it fails)."""
    cmd = cfg.get("longest_step")
    if not cmd:
        return ""
    try:
        r = proc.run(cmd, shell=True, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    out = r.stdout.strip().splitlines()
    return out[0].strip() if r.returncode == 0 and out else ""


def watch_line(conn, root, r, cfg, at):
    wt = worktree(root, r)
    subject = _git(wt or root, "log", "-1", "--format=%s", r["branch"])[:40] if r["branch"] else ""
    unc = len([x for x in _git(wt, "status", "--short").splitlines() if x.strip()]) if wt else "-"
    step = longest_step(cfg, wt or root)
    return (f"{datetime.fromtimestamp(at).strftime('%H:%M')} {label(r)} head: {subject or '-'} | uncommitted: {unc} | "
            f"tokens: {token_text(conn, r)} | daily: {daily_pct()}" + (f" | step: {step}" if step else "")
            + f" | {r['state']}")


def watch(conn, actor, root, interval=None, max_s=MAX_DEFAULT, out=None, sleep=time.sleep, clock=time.time):
    """Monitor-ready loop: check all live rounds, print one flushed line each, sleep, repeat. Ends with a single
    'round watch: no armed rounds' (nothing left to watch) or 'watch: restart' (max_s reached: re-arm the Monitor)."""
    out = out or sys.stdout
    cfg = load_config(root)
    interval = cfg["interval"] if interval is None else interval
    t0 = clock()

    def emit(s):
        print(s, file=out, flush=True)

    while True:
        at = clock()  # clock first
        rows = check(conn, actor, root, at=at)
        rows = [r for r in rows if r["state"] != "disarmed"]
        if not rows:
            emit("round watch: no armed rounds")
            return 0
        for r in rows:
            emit(watch_line(conn, root, r, cfg, at))
        if clock() - t0 + interval >= max_s:
            emit("watch: restart")
            return 0
        sleep(interval)
