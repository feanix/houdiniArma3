"""Measurements derived from existing events/tables (no schema of its own): metrics and stage report.

Conventions (read-only, nothing new is recorded except by these):
- owner messages: events action 'owner-msg' (logged by the advisor-session UserPromptSubmit hook). They are
  per session, not per task. 'owner_msgs_per_merged_task' = owner-msg events in the window / merged tasks in the
  window. A task's 'owner_msgs_during_task' = owner-msg events between its creation and its merge; parallel tasks
  share the same message, so summing it over tasks overcounts (use 'owner_msgs_total').
- owner wait (#86): 'owner-wait'/'owner-answered' events (ownerwait.py); seconds per stage = by the task status the
  wait began in ('decision' for a decision), per merged task in the task rows; open waits count to now.
- window: --since if given, else the first event's timestamp ('since' always shows the real start).
- tokens: units are tokens (not thousands). Model names are normalised to a family (opus/sonnet/haiku/fable).
  Subagents come from the agents table; ad-hoc spawns (no task) are reported apart ('ad-hoc') and are not in
  per-task stats. Persistent sessions (lead, advisor) come from Claude Code transcripts: top-level <session>.jsonl
  files in [metrics] transcripts_dir (default ~/.claude/projects/<project path, non-alnum -> '-'>), one usage per
  assistant message id (field-wise max over its records), sidechain records skipped. Session role = the
  transcript's last custom-title/agent-name record, else the 'name' in <sessions_dir>/*.json for that sessionId
  (default ~/.claude/sessions); anything but lead/advisor is 'unknown'.
- cost: API-equivalent only (plan billing differs), from [pricing.<family>] per million tokens: in, out,
  cache_read; cache_write = 1.25 x in.
- owner sensor reports: events action 'owner-sensor' (`aesir event owner-sensor --task N --note ...`, recorded by
  the advisor when the owner had to describe observed app behaviour). 'owner_sensor_reports' = those events in the
  window; a merged task's own count = its owner-sensor events (target ~0).
- duplication findings: a task log line (aesir task log <id> "dup: <what duplicates what>") whose text starts
  with 'dup:' (case-insensitive). Reviewers record them that way.
- rework: a task status moving backwards after review began: in_review->changes_requested (review_rejections),
  approved/merged -> changes_requested/in_progress/escalated (post_approval_rework).
- waste (task #35, 'aesir waste' and the stage report's 'waste' section): per-agent overhead comes from subagent
  transcripts (<transcripts_dir>/<session>/subagents/**/agent-*.jsonl, one usage per assistant message id). First-turn
  input = input + cache_create + cache_read of the first assistant message (spawn prompt + injected context +
  auto-loaded files + system prompt/tools). Role = prefix of the Aesir agent id (agents.harness_agent_id match), else
  the transcript's 'Role:' line, else the meta agentType, else 'unknown'. Thresholds: aesir.toml [waste]
  first_turn_tokens (flag any agent or role average above it) and first_turn_share (first-turn input / the agent's
  total input over all turns). Rework tokens = tokens of agents on a task that started before one of that task's
  backward status moves (see rework above) and are not reviewers, plus every agent of a wontfix task (discarded).
  Re-read = cache_read share of persistent sessions' input (cache_read / (input+cache_create+cache_read)).
  Verified tasks = tasks with a 'set' event to verified in the window.
- persistent sessions (task #92): 'startup' = context size at the session's first assistant message (regardless of
  --since: a window that starts mid-session must not call conversation size a startup); 'current' = context size
  (input + cache_create + cache_read) at its latest message, or the compact_boundary's postTokens when a compaction
  came after it (#137). Startup split: the SessionStart hook output recorded in
  the transcript (attachment, hookEvent SessionStart; chars / 4 as a token estimate) vs the rest (system prompt,
  tools, CLAUDE.md); None when the transcript holds no such record. aesir.toml [sessions] restart_tokens (default
  300000): status/resume warn when a lead/advisor session's current size passes it.
- time per status: from the task's 'set' events; a status lasts until the next change; terminal statuses
  (verified, wontfix) take no time; a non-terminal last status runs until now.
"""
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from . import friction, gates, ownerwait, tips
from .db import now
from .invoke import cmd as client_cmd
from .rolenames import agent_role
from .service import AesirError

FAMILIES = ("opus", "sonnet", "haiku", "fable")
ROLES = ("lead", "advisor")
CACHE_WRITE_X = 1.25
COST_LABEL = "API-equivalent (plan billing differs)"
# USD per million tokens; aesir.toml [pricing.<family>] overrides
DEFAULT_PRICING = {"opus": {"in": 4, "out": 20, "cache_read": 0.20},
                   "sonnet": {"in": 2, "out": 10, "cache_read": 0.20},
                   "haiku": {"in": 1, "out": 5, "cache_read": 0.10},
                   "fable": {"in": 10, "out": 50, "cache_read": 0.25}}
DEFAULT_WASTE = {"first_turn_tokens": 30000, "first_turn_share": 0.25}  # aesir.toml [waste] overrides
DEFAULT_SESSIONS = {"restart_tokens": 300000}  # aesir.toml [sessions] overrides
SESSION_STALE_H = 24  # a persistent session idle longer than this is not "current": no restart warning
CHARS_PER_TOKEN = 4  # estimate for the hook-output share of startup
KEYS = ("input", "output", "cache_create", "cache_read")


def _t(ts):
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def norm_since(v):
    """'2026-10-06' or a full timestamp -> comparable 'YYYY-MM-DDTHH:MM:SSZ' string ('' = no limit)."""
    if not v:
        return ""
    v = v.strip()
    if len(v) == 10:
        v += "T00:00:00Z"
    try:
        _t(v)
    except ValueError:
        raise ValueError(f"bad --since {v!r}: use YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ")
    return v


def window_start(conn, since):
    """The actual start of the window: --since if given, else the first event's ts (else now)."""
    if since:
        return since
    return conn.execute("SELECT MIN(ts) FROM events").fetchone()[0] or now()


def family(model):
    """'claude-opus-5-5', 'claude-3-5-sonnet-20241022', 'Sonnet' -> opus/sonnet/haiku/fable, else 'unknown'."""
    m = (model or "").lower()
    for f in FAMILIES:
        if f in m:
            return f
    return "unknown"


def default_transcripts_dir(root):
    """Where Claude Code keeps a project's transcripts: drive letter + path with non-alnum -> '-'. The env var
    AESIR_TRANSCRIPTS_DIR overrides it (the test sandbox points it at an empty dir, #098); [metrics] transcripts_dir
    in aesir.toml still wins over both."""
    if os.environ.get("AESIR_TRANSCRIPTS_DIR"):
        return Path(os.environ["AESIR_TRANSCRIPTS_DIR"])
    return Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(root))


def default_sessions_dir():
    """Claude Code's live session files; AESIR_SESSIONS_DIR overrides (the test sandbox, #098)."""
    return Path(os.environ.get("AESIR_SESSIONS_DIR") or Path.home() / ".claude" / "sessions")


def load_config(root):
    """[metrics] transcripts_dir, sessions_dir and [pricing.<family>] in/out/cache_read, over the defaults."""
    try:
        cfg = gates.load_config(root)
    except AesirError as e:
        if "no aesir.toml" not in str(e):
            raise
        cfg = {}
    sec = cfg.get("metrics", {})
    for k in ("transcripts_dir", "sessions_dir"):
        if k in sec and not isinstance(sec[k], str):
            raise AesirError(f"bad aesir.toml: [metrics] {k} must be a path string")
    pricing = {f: dict(v) for f, v in DEFAULT_PRICING.items()}
    for f, v in cfg.get("pricing", {}).items():
        if not isinstance(v, dict) or not all(isinstance(x, (int, float)) and not isinstance(x, bool) and x >= 0
                                              for x in v.values()):
            raise AesirError(f"bad aesir.toml: [pricing.{f}] must hold non-negative numbers (in, out, cache_read)")
        pricing.setdefault(f, {}).update(v)
    waste = dict(DEFAULT_WASTE)
    for k, v in cfg.get("waste", {}).items():
        if k not in DEFAULT_WASTE or isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            raise AesirError(f"bad aesir.toml: [waste] {k} must be one of {sorted(DEFAULT_WASTE)} with a non-negative number")
        waste[k] = v
    sessions = dict(DEFAULT_SESSIONS)
    for k, v in cfg.get("sessions", {}).items():
        if k not in DEFAULT_SESSIONS or isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
            raise AesirError(f"bad aesir.toml: [sessions] {k} must be one of {sorted(DEFAULT_SESSIONS)} with a positive number")
        sessions[k] = v
    return {"transcripts_dir": Path(sec.get("transcripts_dir") or default_transcripts_dir(root)),
            "sessions_dir": Path(sec.get("sessions_dir") or default_sessions_dir()),
            "pricing": pricing, "waste": waste, "sessions": sessions}


def cost(fam, tok, pricing):
    """API-equivalent USD for one family's token counts (None when the family has no price)."""
    p = pricing.get(fam)
    if not p or "in" not in p:
        return None
    return round((tok["input"] * p["in"] + tok["output"] * p.get("out", 0)
                  + tok["cache_create"] * p["in"] * CACHE_WRITE_X
                  + tok["cache_read"] * p.get("cache_read", 0)) / 1e6, 4)


def _session_names(sessions_dir):
    out = {}
    for f in Path(sessions_dir).glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            if d.get("sessionId") and d.get("name"):
                out[d["sessionId"]] = d["name"]
        except (OSError, ValueError, AttributeError):
            continue
    return out


def _role_of(name):
    n = (name or "").strip().lower()
    n = n.rsplit("-", 1)[-1] if n.rsplit("-", 1)[-1] in ROLES else n  # '<project>-<role>' (task #32 names)
    return n if n in ROLES else "unknown"


_USAGE = (("input", "input_tokens"), ("output", "output_tokens"),
          ("cache_create", "cache_creation_input_tokens"), ("cache_read", "cache_read_input_tokens"))


def _read_transcript(f):
    """(session title or None, {message id: {model, ts, usage}}, chars of the first SessionStart hook output or None,
    (ts, postTokens) of the last compact_boundary or None) for one transcript file."""
    title, msgs, hook, boundary = None, {}, None, None
    try:
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None, {}, None, None
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if not isinstance(r, dict):
            continue
        if r.get("type") == "custom-title":
            title = r.get("customTitle") or title
        elif r.get("type") == "agent-name":
            title = r.get("agentName") or title
        elif r.get("type") == "system" and r.get("subtype") == "compact_boundary":
            post = (r.get("compactMetadata") or {}).get("postTokens")
            boundary = (r.get("timestamp") or "", post if isinstance(post, int) else 0)
        elif r.get("type") == "attachment" and hook is None:
            hook = _hook_chars(r.get("attachment"))
        elif r.get("type") == "assistant" and not r.get("isSidechain"):
            m = r.get("message") or {}
            u = m.get("usage")
            if not isinstance(u, dict) or not m.get("id") or m.get("model") == "<synthetic>":
                continue
            cur = msgs.setdefault(m["id"], {"model": m.get("model"), "ts": r.get("timestamp") or "",
                                            "u": dict.fromkeys(KEYS, 0)})
            for k, key in _USAGE:
                cur["u"][k] = max(cur["u"][k], u.get(key) or 0)
    return title, msgs, hook, boundary


def _hook_chars(att):
    """Size of a SessionStart hook output attachment. Real transcripts hold `type: hook_success` with the hook's JSON
    in `stdout` (additionalContext is the injected text); a following `hook_additional_context` record carries a
    list in `content` and is not our output, so non-string content is ignored. None for any other record."""
    if not isinstance(att, dict) or att.get("hookEvent") != "SessionStart" or att.get("type") == "hook_additional_context":
        return None
    text = att.get("stdout") or att.get("content") or ""
    if not isinstance(text, str):
        return None
    try:
        ctx = json.loads(text)["hookSpecificOutput"]["additionalContext"]
        text = ctx if isinstance(ctx, str) else text
    except (ValueError, KeyError, TypeError):
        pass
    return len(text)


def session_usage(cfg, since="", until=""):
    """Rows {role, model, messages, tokens..., cost_usd} per (role, family) from top-level transcripts; only
    assistant messages stamped within [since, until]. A missing transcripts dir gives []."""
    d = Path(cfg["transcripts_dir"])
    if not d.is_dir():
        return []
    names = _session_names(cfg["sessions_dir"])
    acc = {}
    for f in sorted(d.glob("*.jsonl")):
        title, msgs, _, _ = _read_transcript(f)
        role = _role_of(title or names.get(f.stem))
        for m in msgs.values():
            ts = m["ts"][:19] + "Z" if m["ts"] else ""
            if (since and ts < since) or (until and ts > until):
                continue
            fam = family(m["model"])
            row = acc.setdefault((role, fam), {"role": role, "model": fam, "messages": 0, **dict.fromkeys(KEYS, 0)})
            row["messages"] += 1
            for k in KEYS:
                row[k] += m["u"][k]
    out = sorted(acc.values(), key=lambda r: (r["role"] not in ROLES, r["role"], r["model"]))
    for r in out:
        r["cost_usd"] = cost(r["model"], r, cfg["pricing"])
    return out


def _sum(rows):
    t = dict.fromkeys(KEYS, 0)
    for r in rows:
        for k in KEYS:
            t[k] += r[k] or 0
    costs = [r["cost_usd"] for r in rows if r.get("cost_usd") is not None]
    t["cost_usd"] = round(sum(costs), 4) if costs else None
    return t


def _status_changes(conn, tid):
    out = []
    for r in conn.execute("SELECT ts, payload FROM events WHERE entity='task' AND entity_id=? AND action='set'"
                          " ORDER BY id", (str(tid),)):
        p = json.loads(r["payload"] or "{}")
        if "status" in p:
            out.append((r["ts"], p["status"]["from"], p["status"]["to"]))
    return out


def _time_in_status(created, changes, end, terminal=("verified", "wontfix")):
    out, cur, since = {}, "open", created
    for ts, _, to in changes:
        out[cur] = out.get(cur, 0) + max(0, int((_t(ts) - _t(since)).total_seconds()))
        cur, since = to, ts
    if cur not in terminal:
        out[cur] = out.get(cur, 0) + max(0, int((_t(end) - _t(since)).total_seconds()))
    return out


def _task_row(conn, t, merged_ts, end):
    ch = _status_changes(conn, t["id"])
    owner_msgs = conn.execute("SELECT COUNT(*) FROM events WHERE action='owner-msg' AND ts>=? AND ts<=?",
                              (t["created"], merged_ts)).fetchone()[0]
    sensor = conn.execute("SELECT COUNT(*) FROM events WHERE entity='task' AND entity_id=? AND action='owner-sensor'",
                          (str(t["id"]),)).fetchone()[0]
    rej = sum(1 for _, f, to in ch if f == "in_review" and to == "changes_requested")
    post = sum(1 for _, f, to in ch if f in ("approved", "merged") and to in ("changes_requested", "in_progress", "escalated"))
    dups = [r["text"] for r in conn.execute("SELECT text FROM task_log WHERE task_id=? ORDER BY id", (t["id"],))
            if r["text"].strip().lower().startswith("dup:")]
    tok = conn.execute("SELECT COALESCE(SUM(tokens_in),0) i, COALESCE(SUM(tokens_out),0) o,"
                       " COALESCE(SUM(tokens_cache_read),0) cr, COALESCE(SUM(tokens_cache_create),0) cc,"
                       " COUNT(*) n FROM agents WHERE task_id=?", (t["id"],)).fetchone()
    return {"id": t["id"], "title": t["title"], "status": t["status"], "merged": merged_ts,
            "owner_msgs_during_task": owner_msgs,
            "owner_wait_seconds": ownerwait.summary(conn, t["created"], {t["id"]})["seconds"],
            "owner_sensor_reports": sensor, "attempts": t["attempts"], "escalations": t["escalations"],
            "review_rejections": rej, "post_approval_rework": post,
            "duplication_findings": len(dups), "duplications": dups,
            "seconds_in_status": _time_in_status(t["created"], ch, end),
            "agents": tok["n"], "tokens": {"input": tok["i"], "output": tok["o"],
                                           "cache_read": tok["cr"], "cache_create": tok["cc"]}}


def merged_tasks(conn, since=""):
    """[(task row, ts of first merged event)] for tasks merged at or after `since`."""
    rows = conn.execute(
        "SELECT t.*, MIN(e.ts) AS merged_ts FROM tasks t JOIN events e ON e.entity='task' AND e.entity_id=CAST(t.id AS TEXT)"
        " AND e.action='set' AND json_extract(e.payload,'$.status.to')='merged' GROUP BY t.id HAVING MIN(e.ts)>=?"
        " ORDER BY t.id", (since,)).fetchall()
    return [(r, r["merged_ts"]) for r in rows]


def _agent_rows(conn, since):
    return conn.execute(
        "SELECT agent_id, role, task_id, model, started, tokens_in, tokens_out, tokens_cache_read, tokens_cache_create"
        " FROM agents WHERE started>=? ORDER BY id", (since,)).fetchall()


def _by_family(rows, pricing):
    acc = {}
    for r in rows:
        a = acc.setdefault(family(r["model"]), {"agents": 0, **dict.fromkeys(KEYS, 0)})
        a["agents"] += 1
        a["input"] += r["tokens_in"] or 0
        a["output"] += r["tokens_out"] or 0
        a["cache_create"] += r["tokens_cache_create"] or 0
        a["cache_read"] += r["tokens_cache_read"] or 0
    out = [{"model": k, **v} for k, v in sorted(acc.items())]
    for r in out:
        r["cost_usd"] = cost(r["model"], r, pricing)
    return out


def tokens_by(conn, key, since="", pricing=None):
    """Subagent tokens by model family ('model', all spawns) or by task id ('task', ad-hoc spawns excluded)."""
    rows = _agent_rows(conn, since)
    if key == "model":
        return _by_family(rows, pricing or DEFAULT_PRICING)
    acc = {}
    for r in rows:
        if r["task_id"]:
            a = acc.setdefault(r["task_id"], {"agents": 0, **dict.fromkeys(KEYS, 0)})
            a["agents"] += 1
            a["input"] += r["tokens_in"] or 0
            a["output"] += r["tokens_out"] or 0
            a["cache_create"] += r["tokens_cache_create"] or 0
            a["cache_read"] += r["tokens_cache_read"] or 0
    return [{"task": k, **v} for k, v in sorted(acc.items())]


def per_agent(conn, since=""):
    return [{"agent_id": r["agent_id"], "role": r["role"], "task_id": r["task_id"], "task": r["task_id"] or "ad-hoc",
             "model": family(r["model"]), "started": r["started"], "input": r["tokens_in"], "output": r["tokens_out"],
             "cache_read": r["tokens_cache_read"], "cache_create": r["tokens_cache_create"]}
            for r in _agent_rows(conn, since)]


def token_report(conn, since, until, cfg):
    """Everything token-related: subagents by family, ad-hoc spawns apart, persistent sessions by role, totals."""
    cfg = cfg or {"pricing": DEFAULT_PRICING, "transcripts_dir": "", "sessions_dir": ""}
    pricing = cfg["pricing"]
    rows = _agent_rows(conn, since)
    by_model = _by_family(rows, pricing)
    adhoc = [r for r in rows if not r["task_id"]]
    sessions = session_usage(cfg, since, until) if cfg["transcripts_dir"] else []
    sub, ses = _sum(by_model), _sum(sessions)
    return {"units": "tokens", "cost_label": COST_LABEL,
            "by_model": by_model,
            "by_task": tokens_by(conn, "task", since),
            "ad_hoc": {"agents": len(adhoc), "by_model": _by_family(adhoc, pricing)},
            "by_agent": per_agent(conn, since),
            "sessions": sessions,
            "totals": {"subagents": sub, "sessions": ses,
                       "all": _sum([{**sub}, {**ses}])}}


def _sensor_total(conn, since):
    return conn.execute("SELECT COUNT(*) FROM events WHERE action='owner-sensor' AND ts>=?", (since,)).fetchone()[0]


def _flaky_total(conn, since):
    return conn.execute("SELECT COUNT(*) FROM events WHERE entity='gate' AND action='flaky' AND ts>=?",
                        (since,)).fetchone()[0]


def _tips_section(conn):
    """Tip usage (#56); [tips] and [friction] config from the project's aesir.toml."""
    from . import db
    try:
        root = db.project_root()
        return tips.report(conn, tips.load_config(root), friction.load_config(root))
    except Exception:
        return tips.report(conn)


def metrics(conn, since="", cfg=None):
    end = now()
    start = window_start(conn, since)
    rows = [_task_row(conn, t, m, end) for t, m in merged_tasks(conn, since)]
    secs = {}
    for r in rows:
        for k, v in r["seconds_in_status"].items():
            secs[k] = secs.get(k, 0) + v
    n = len(rows)
    owner_total = conn.execute("SELECT COUNT(*) FROM events WHERE action='owner-msg' AND ts>=?", (since,)).fetchone()[0]
    sensor_total = _sensor_total(conn, since)
    esc = conn.execute("SELECT COUNT(*) FROM escalations WHERE ts>=?", (since,)).fetchone()[0]
    return {"since": start, "since_requested": since, "generated": end,
            "summary": {"merged_tasks": n, "owner_msgs_total": owner_total,
                        "owner_msgs_per_merged_task": round(owner_total / n, 2) if n else None,
                        "attempts_per_merged_task": round(sum(r["attempts"] for r in rows) / n, 2) if n else 0,
                        "owner_wait": ownerwait.summary(conn, since),
                        "owner_sensor_reports": sensor_total,
                        "owner_sensor_reports_per_merged_task": round(sensor_total / n, 2) if n else None,
                        "flaky_retries": _flaky_total(conn, since),
                        "escalations_in_period": esc,
                        "rework": sum(r["review_rejections"] + r["post_approval_rework"] for r in rows),
                        "duplication_findings": sum(r["duplication_findings"] for r in rows),
                        "seconds_in_status": secs},
            "tasks": rows,
            "tokens": token_report(conn, since, end, cfg),
            "tips": _tips_section(conn)}


def _first_turn(u):
    return u["input"] + u["cache_create"] + u["cache_read"]


def _read_agent_transcript(f):
    """{first_turn, input_all, output, messages, role_line, started} for one subagent transcript, or None. Usage is
    one per assistant message id (field-wise max); first turn = the earliest assistant message."""
    try:
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    msgs, order, prompt, started = {}, [], "", ""
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if not isinstance(r, dict):
            continue
        m = r.get("message") if isinstance(r.get("message"), dict) else {}
        if not started and r.get("timestamp"):
            started = r["timestamp"][:19] + "Z"
        if r.get("type") == "user" and not prompt and m.get("role") == "user":
            c = m.get("content")
            prompt = c if isinstance(c, str) else " ".join(b.get("text", "") for b in c or [] if isinstance(b, dict))
        elif r.get("type") == "assistant" and isinstance(m.get("usage"), dict) and m.get("model") != "<synthetic>":
            mid = m.get("id") or r.get("uuid")
            if mid not in msgs:
                msgs[mid] = dict.fromkeys(KEYS, 0)
                order.append(mid)
            for k, key in _USAGE:
                msgs[mid][k] = max(msgs[mid][k], m["usage"].get(key) or 0)
    if not order:
        return None
    tot = {k: sum(v[k] for v in msgs.values()) for k in KEYS}
    rm = re.search(r"(?im)^\W*Role:\s*([A-Za-z][\w-]*)", prompt)
    return {"first_turn": _first_turn(msgs[order[0]]), "input_all": tot["input"] + tot["cache_create"] + tot["cache_read"],
            "output": tot["output"], "messages": len(order), "role_line": rm.group(1).lower() if rm else "",
            "started": started}


def agent_overhead(conn, cfg, since="", until=""):
    """One row per subagent transcript (agent-*.jsonl anywhere under <transcripts_dir>/*/subagents): agent id, role,
    task, first_turn input, total input over all turns, output, first_turn_share. [] without a transcripts dir."""
    d = Path(cfg["transcripts_dir"])
    if not d.is_dir():
        return []
    known = {r["harness_agent_id"]: r for r in conn.execute(
        "SELECT agent_id, agent_type, task_id, harness_agent_id FROM agents WHERE harness_agent_id!='' ORDER BY id")}
    out = []
    for f in sorted(d.glob("*/subagents/**/agent-*.jsonl")):
        p = _read_agent_transcript(f)
        if not p or (since and p["started"] < since) or (until and p["started"] > until):
            continue
        row = known.get(f.stem[len("agent-"):])
        try:
            atype = json.loads(f.with_suffix(".meta.json").read_text(encoding="utf-8")).get("agentType") or ""
        except (OSError, ValueError, AttributeError):
            atype = ""
        role = (agent_role(row) if row else p["role_line"]) or atype
        out.append({"agent": row["agent_id"] if row else f.stem, "role": role or "unknown", "agent_type": atype,
                    "task": row["task_id"] if row else None, "started": p["started"], "messages": p["messages"],
                    "first_turn": p["first_turn"], "input_all": p["input_all"], "output": p["output"],
                    "first_turn_share": round(p["first_turn"] / p["input_all"], 3) if p["input_all"] else None})
    return out


def _flag(first_turn, share, th):
    f = []
    if first_turn > th["first_turn_tokens"]:
        f.append("first_turn")
    if share is not None and share > th["first_turn_share"]:
        f.append("share")
    return f


def overhead_by_role(agents, th):
    """Per role: agents, avg/max first turn, total first-turn tokens, total input/output, share, flags."""
    acc = {}
    for a in agents:
        acc.setdefault(a["role"], []).append(a)
    rows = []
    for role, xs in sorted(acc.items()):
        ft, ia = sum(x["first_turn"] for x in xs), sum(x["input_all"] for x in xs)
        avg = round(ft / len(xs))
        share = round(ft / ia, 3) if ia else None
        rows.append({"role": role, "agents": len(xs), "first_turn_avg": avg,
                     "first_turn_max": max(x["first_turn"] for x in xs), "first_turn_total": ft, "input_all": ia,
                     "output": sum(x["output"] for x in xs), "first_turn_share": share, "flags": _flag(avg, share, th)})
    return rows


def session_overhead(cfg, since="", until=""):
    """Persistent sessions per role: sessions, first-turn input of each session's first assistant message (the
    startup cost, taken from the session's first message even when --since starts mid-session: system prompt + tools + CLAUDE.md + SessionStart context), re-read share (cache_read / input)."""
    d = Path(cfg["transcripts_dir"])
    if not d.is_dir():
        return []
    names = _session_names(cfg["sessions_dir"])
    acc = {}
    for f in sorted(d.glob("*.jsonl")):
        title, msgs, _, _ = _read_transcript(f)
        role = _role_of(title or names.get(f.stem))
        allm = [m for ts, m in sorted((m["ts"], m) for m in msgs.values() if m["ts"])]
        ms = [m for m in allm if not ((since and m["ts"][:19] + "Z" < since) or (until and m["ts"][:19] + "Z" > until))]
        if not ms:
            continue
        a = acc.setdefault(role, {"role": role, "sessions": 0, "first_turns": [], "input_all": 0, "cache_read": 0,
                                  "output": 0})
        a["sessions"] += 1
        a["first_turns"].append(_first_turn(allm[0]["u"]))  # the session's real first message, not the window's
        for m in ms:
            a["input_all"] += _first_turn(m["u"])
            a["cache_read"] += m["u"]["cache_read"]
            a["output"] += m["u"]["output"]
    out = []
    for a in sorted(acc.values(), key=lambda r: (r["role"] not in ROLES, r["role"])):
        ft = a.pop("first_turns")
        out.append({**a, "first_turn_avg": round(sum(ft) / len(ft)), "first_turn_max": max(ft),
                    "reread_share": round(a["cache_read"] / a["input_all"], 3) if a["input_all"] else None})
    return out


def persistent_sessions(cfg):
    """One row per lead/advisor transcript: role, session id, messages, started/last ts, startup (context size at
    the first message), current (at the latest), hook_tokens (SessionStart output, estimated; None if unrecorded)
    and rest_tokens (startup - hook_tokens). Sorted by last activity, newest last. [] without a transcripts dir."""
    d = Path(cfg["transcripts_dir"])
    if not d.is_dir():
        return []
    names = _session_names(cfg["sessions_dir"])
    out = []
    for f in sorted(d.glob("*.jsonl")):
        title, msgs, hook, boundary = _read_transcript(f)
        role = _role_of(title or names.get(f.stem))
        ms = [m for ts, m in sorted((m["ts"], m) for m in msgs.values() if m["ts"])]
        if role not in ROLES or not ms:
            continue
        startup = _first_turn(ms[0]["u"])
        current = _first_turn(ms[-1]["u"])
        if boundary and boundary[0] >= ms[-1]["ts"]:  # compacted since the last message: that size is stale (#137)
            current = boundary[1]
        ht = min(hook // CHARS_PER_TOKEN, startup) if hook is not None else None
        out.append({"role": role, "session": f.stem, "messages": len(ms), "started": ms[0]["ts"][:19] + "Z",
                    "last": ms[-1]["ts"][:19] + "Z", "startup": startup, "current": current,
                    "hook_tokens": ht, "rest_tokens": startup - ht if ht is not None else None})
    return sorted(out, key=lambda r: r["last"])


def current_sessions(cfg, at=None, own=None):
    """The newest session per role that was active within SESSION_STALE_H (older ones are dead, not current).
    own: a session id; only that session's row (none while its transcript has no assistant message)."""
    at = at or now()
    latest = {r["role"]: r for r in persistent_sessions(cfg) if own is None or r["session"] == own}
    return [r for r in latest.values() if (_t(at) - _t(r["last"])).total_seconds() <= SESSION_STALE_H * 3600]


def session_warnings(cfg, role=None, at=None, active=(), session=None):
    """Current lead/advisor sessions whose context passes [sessions] restart_tokens: dicts with role, session,
    current, limit and the text that names the fix. `active`: ids of agents still running with recent transcript
    activity (#195); the text names them and says to restart only after they end (none: text unchanged).
    `session`: the id of the session asking (SessionStart, reports#100): only that transcript is measured, and one
    with no usage record yet gives no warning (never the newest other session of the role). None: newest per role."""
    tail = (f"; agents still running with recent activity: {', '.join(active)}: restart only after they end"
            if active else "")
    limit = (cfg.get("sessions") or DEFAULT_SESSIONS)["restart_tokens"]
    return [{"role": r["role"], "session": r["session"], "current": r["current"], "limit": limit,
             "text": client_cmd(f"{r['role']} session context is {r['current']} tokens (restart_tokens {limit}): write a handoff "
                     f"(`python -m aesir --as {r['role']} role note {r['role']} \"<handoff>\"`), then restart the "
                     "session at a safe point" + tail)}
            for r in current_sessions(cfg, at, own=session) if r["current"] > limit and role in (None, r["role"])]


def _agent_tok(r):
    return {"in_out": (r["tokens_in"] or 0) + (r["tokens_out"] or 0),
            "all": sum(r[k] or 0 for k in ("tokens_in", "tokens_out", "tokens_cache_read", "tokens_cache_create"))}


def waste_report(conn, since="", cfg=None):
    """Headline (tokens per verified task) and waste categories: rework, re-read, duplication, overhead."""
    cfg = cfg or {}
    th = cfg.get("waste") or DEFAULT_WASTE
    end = now()
    ver = sorted(int(r["entity_id"]) for r in conn.execute(
        "SELECT entity_id FROM events WHERE entity='task' AND action='set' AND ts>=?"
        " AND json_extract(payload,'$.status.to')='verified' GROUP BY entity_id", (since,)))
    on_task = {}
    for a in conn.execute("SELECT agent_id, agent_type, task_id, started, tokens_in, tokens_out, tokens_cache_read,"
                          " tokens_cache_create FROM agents WHERE started>=? AND task_id IS NOT NULL", (since,)):
        on_task.setdefault(a["task_id"], []).append(a)
    ver_tok = {"in_out": 0, "all": 0}
    for t in ver:
        for a in on_task.get(t, []):
            for k, v in _agent_tok(a).items():
                ver_tok[k] += v
    rework = {"agents": 0, "in_out": 0, "all": 0, "tasks": []}
    for tid, xs in sorted(on_task.items()):
        t = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
        back = [ts for ts, f, to in _status_changes(conn, tid)
                if (f == "in_review" and to == "changes_requested")
                or (f in ("approved", "merged") and to in ("changes_requested", "in_progress", "escalated"))]
        hit = [a for a in xs if (t and t["status"] == "wontfix")
               or (back and agent_role(a) != "reviewer" and a["started"] < max(back))]
        if hit:
            rework["tasks"].append(tid)
            rework["agents"] += len(hit)
            for a in hit:
                for k, v in _agent_tok(a).items():
                    rework[k] += v
    ao = agent_overhead(conn, cfg, since, end) if cfg.get("transcripts_dir") else []
    ses = session_overhead(cfg, since, end) if cfg.get("transcripts_dir") else []
    persistent = [r for r in persistent_sessions(cfg) if r["last"] >= since] if cfg.get("transcripts_dir") else []
    ft, ia = sum(a["first_turn"] for a in ao), sum(a["input_all"] for a in ao)
    dups = conn.execute("SELECT COUNT(*) FROM task_log WHERE lower(ltrim(text)) LIKE 'dup:%' AND ts>=?",
                        (since,)).fetchone()[0]
    n = len(ver)
    return {"units": "tokens", "verified_tasks": ver,
            "tokens_per_verified_task": {"in_out": round(ver_tok["in_out"] / n) if n else None,
                                         "all": round(ver_tok["all"] / n) if n else None},
            "rework": rework,
            "reread": [{k: s[k] for k in ("role", "sessions", "reread_share", "cache_read")} for s in ses],
            "duplication": {"findings": dups, "per_verified_task": round(dups / n, 2) if n else None},
            "overhead": {"agents": len(ao), "first_turn_total": ft, "input_all": ia,
                         "share": round(ft / ia, 3) if ia else None},
            "thresholds": dict(th),
            "overhead_by_role": overhead_by_role(ao, th),
            "overhead_by_agent": sorted(ao, key=lambda a: -a["first_turn"]),
            "flagged_agents": [a["agent"] for a in ao if _flag(a["first_turn"], a["first_turn_share"], th)],
            "sessions": ses,
            "persistent": persistent}


def _total(rows):
    return sum((r["input"] or 0) + (r["output"] or 0) for r in rows)


def stage_report(conn, since, cfg=None):
    end = now()
    start = window_start(conn, since)
    wall = int((_t(end) - _t(start)).total_seconds())
    mt = merged_tasks(conn, since)
    tok = token_report(conn, since, end, cfg)
    task_rows = [r for r in tok["by_task"] if r["task"] in {t["id"] for t, _ in mt}]
    task_in_out = sum(r["input"] + r["output"] for r in task_rows)
    owner = conn.execute("SELECT COUNT(*) FROM events WHERE action='owner-msg' AND ts>=?", (since,)).fetchone()[0]
    sensor = _sensor_total(conn, since)
    return {"since": start, "since_requested": since, "until": end, "wall_seconds": wall, "tasks_merged": len(mt),
            "task_ids": [t["id"] for t, _ in mt],
            "owner_msgs": owner,
            "owner_msgs_per_merged_task": round(owner / len(mt), 2) if mt else None,
            "owner_wait": ownerwait.summary(conn, since),
            "owner_sensor_reports": sensor,
            "owner_sensor_reports_per_merged_task": round(sensor / len(mt), 2) if mt else None,
            "tokens": tok,
            "tokens_by_model": tok["by_model"],
            "tokens_in_out_total": _total(tok["by_model"]),
            "tokens_per_merged_task": round(task_in_out / len(mt)) if mt else None,
            "waste": waste_report(conn, since, cfg),
            "friction": friction.stage_section(conn, since)}


def _hms(s):
    return f"{s // 3600}h{s % 3600 // 60:02d}m"


def _tok_line(r, lead):
    c = r.get("cost_usd")
    return (f"  {lead}: in={r['input']} out={r['output']} cache_create={r['cache_create']} "
            f"cache_read={r['cache_read']} cost~${c:.2f}" if c is not None else
            f"  {lead}: in={r['input']} out={r['output']} cache_create={r['cache_create']} "
            f"cache_read={r['cache_read']} cost~n/a")


def render_tokens(tk, agents=False):
    print(f"\ntokens (units: tokens; cost: {tk['cost_label']})")
    print("subagents by model (task + ad-hoc):")
    for r in tk["by_model"]:
        print(_tok_line(r, f"{r['model']} agents={r['agents']}"))
    ah = tk["ad_hoc"]
    print(f"ad-hoc spawns (no task, not in per-task stats): {ah['agents']}")
    for r in ah["by_model"]:
        print(_tok_line(r, f"{r['model']} agents={r['agents']}"))
    print("persistent sessions by role:")
    for r in tk["sessions"] or []:
        print(_tok_line(r, f"{r['role']}/{r['model']} msgs={r['messages']}"))
    if not tk["sessions"]:
        print("  (no transcripts found)")
    for k, label in (("subagents", "total subagents"), ("sessions", "total sessions"), ("all", "total all")):
        print(_tok_line(tk["totals"][k], label))
    if agents:
        print("\ntokens by agent:")
        for r in tk["by_agent"]:
            print(f"  {r['agent_id']} ({r['role']}, task {r['task'] if r['task_id'] else 'ad-hoc'}, {r['model']}): "
                  f"in={r['input']} out={r['output']}")


def _render_wait(w):
    st = ", ".join(f"{k} {_hms(v)}" for k, v in w["seconds_by_stage"].items()) or "-"
    print(f"  owner wait: {w['waits']} ({w['open']} open), {_hms(w['seconds'])} total; per stage: {st}")


def render_metrics(d):
    s = d["summary"]
    print(f"metrics since {d['since']} ({d['generated']})")
    for k in ("merged_tasks", "owner_msgs_total", "owner_msgs_per_merged_task", "owner_sensor_reports",
              "owner_sensor_reports_per_merged_task", "attempts_per_merged_task",
              "escalations_in_period", "flaky_retries", "rework", "duplication_findings"):
        v = s[k]
        print(f"  {k}: {'n/a' if v is None else v}")
    print("  (owner_msgs_per_merged_task = owner messages in window / merged tasks in window)")
    _render_wait(s["owner_wait"])
    print("  time per status: " + (", ".join(f"{k} {_hms(v)}" for k, v in s["seconds_in_status"].items()) or "-"))
    print("\ntasks (merged; owner_msgs_during_task = owner messages between task creation and merge):")
    for r in d["tasks"]:
        print(f"  #{r['id']:03d} owner_msgs_during_task={r['owner_msgs_during_task']} owner_wait={_hms(r['owner_wait_seconds'])} sensor={r['owner_sensor_reports']} attempts={r['attempts']} "
              f"esc={r['escalations']} rejected={r['review_rejections']} rework={r['post_approval_rework']} "
              f"dup={r['duplication_findings']} agents={r['agents']} "
              f"tok_in_out={r['tokens']['input'] + r['tokens']['output']}  {r['title']}")
    render_tokens(d["tokens"], agents=True)
    tips.render(d["tips"])


def render_stage(d):
    print(f"stage report {d['since']} -> {d['until']}")
    print(f"  wall time: {_hms(d['wall_seconds'])}")
    print(f"  tasks merged: {d['tasks_merged']} {d['task_ids']}")
    pm = d["owner_msgs_per_merged_task"]
    print(f"  owner messages: {d['owner_msgs']} ({'n/a' if pm is None else pm} per merged task)")
    _render_wait(d["owner_wait"])
    ps = d["owner_sensor_reports_per_merged_task"]
    print(f"  owner sensor reports: {d['owner_sensor_reports']} ({'n/a' if ps is None else ps} per merged task; target ~0)")
    render_tokens(d["tokens"])
    print(f"  subagent tokens (in+out): {d['tokens_in_out_total']}; per merged task "
          f"(that task's agents only, ad-hoc excluded): {d['tokens_per_merged_task']}")
    print()
    render_waste(d["waste"])
    friction.render(d["friction"])


def render_waste(w, agents=False):
    pv = w["tokens_per_verified_task"]
    print(f"waste (units: tokens; verified tasks: {len(w['verified_tasks'])} {w['verified_tasks']})")
    print(f"  tokens per verified task: in+out={pv['in_out']} all(with cache)={pv['all']}")
    r = w["rework"]
    print(f"  rework: agents={r['agents']} in+out={r['in_out']} all={r['all']} tasks={r['tasks']}")
    for s in w["reread"]:
        print(f"  re-read {s['role']}: sessions={s['sessions']} cache_read share of input={s['reread_share']}")
    d = w["duplication"]
    print(f"  duplication findings: {d['findings']} ({d['per_verified_task']} per verified task)")
    o, th = w["overhead"], w["thresholds"]
    print(f"  overhead: agents={o['agents']} first-turn input={o['first_turn_total']} of total input {o['input_all']} "
          f"(share {o['share']}); flags above first_turn_tokens={th['first_turn_tokens']} / "
          f"first_turn_share={th['first_turn_share']}")
    for x in w["overhead_by_role"]:
        flag = " FLAG " + ",".join(x["flags"]) if x["flags"] else ""
        print(f"    {x['role']}: agents={x['agents']} first_turn avg={x['first_turn_avg']} max={x['first_turn_max']} "
              f"input_all={x['input_all']} out={x['output']} share={x['first_turn_share']}{flag}")
    for s in w["sessions"]:
        print(f"    session {s['role']}: n={s['sessions']} startup first_turn avg={s['first_turn_avg']} "
              f"max={s['first_turn_max']}")
    for p in w.get("persistent", []):
        split = ("" if p["hook_tokens"] is None else
                 f" (SessionStart injection ~{p['hook_tokens']}, rest {p['rest_tokens']})")
        print(f"    persistent {p['role']} {p['session'][:8]}: startup={p['startup']}{split} "
              f"current={p['current']} msgs={p['messages']} last={p['last']}")
    if agents:
        for a in w["overhead_by_agent"]:
            print(f"    {a['agent']} ({a['role']}, task {a['task'] or 'ad-hoc'}): first_turn={a['first_turn']} "
                  f"input_all={a['input_all']} out={a['output']} share={a['first_turn_share']}")
