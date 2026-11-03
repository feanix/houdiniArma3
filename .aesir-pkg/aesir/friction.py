"""Friction and deviation capture (task #36). Everything is an event (no table):
  friction/<type> 'record'  {type, detail, agent, task, source, key}
  friction/<key>  'link'    {target}                (a task or tip that addresses a recurring friction)
  deviation/<task> 'record' {cause, criterion, detail, source, role, model, agent, task}
A friction event also carries `tool`/`command` (the failing call's normalised command) when captured from a
transcript; `triage` groups events by (type, command, error class) so repeat causes show up with counts.
Auto-captured at SubagentStop (transcript tool errors, trailer lines) and from the merge queue / gates; agents add
`friction:` / `deviation:` trailer lines to their final report."""
import json
import re
from collections import Counter, defaultdict

from . import gates
from .db import event
from .rolenames import agent_role, id_role
from .service import AesirError

TYPES = ("tool_error", "permission_denial", "hook_failure", "merge_conflict", "false_red", "msg_latency",
         "agent_timeout", "gate_skip", "evidence_skip", "reported", "command_failed", "no_context",
         "stray_write")  # stray_write (#138): a worktree agent's shell redirected output into the main checkout
QUIET_TYPES = ("command_failed", "no_context")  # recorded and reported, never flagged as recurring friction in status
# no_context (#128, agent started without its spawn context) has its own status anomaly, so triage_flags skips it.
CAUSES = ("spec", "context", "environment", "model", "judgement", "drift")
DEFAULTS = {"recurring_min": 3, "max_per_agent": 20}

FRICTION_RE = re.compile(r"(?im)^\W*friction:[ \t]*(.*?)[ \t]*$")
DEVIATION_RE = re.compile(r"(?im)^\W*deviation:[ \t]*(.*?)[ \t]*$")
NONE_RE = re.compile(r"^(none|n/a|-)\W*$", re.I)


def load_config(root):
    """[friction] in aesir.toml: recurring_min (N occurrences flag a friction in status), max_per_agent."""
    try:
        sec = gates.load_config(root).get("friction", {})
    except (AesirError, OSError, ValueError):
        sec = {}
    cfg = {**DEFAULTS, **sec}
    for k in DEFAULTS:
        if isinstance(cfg[k], bool) or not isinstance(cfg[k], int) or cfg[k] < 1:
            raise AesirError(f"bad aesir.toml: [friction] {k} must be an integer >= 1")
    return cfg


def _key(ftype, detail):
    """Recurrence key: the type for auto-captured frictions, type + normalised text for reported ones."""
    if ftype != "reported":
        return ftype
    words = re.sub(r"[^a-z0-9 ]", " ", detail.lower()).split()
    return "reported:" + " ".join(words[:8])


def record(conn, actor, ftype, detail="", agent="", task=None, source="auto", tool="", command=""):
    if ftype not in TYPES:
        raise AesirError(f"unknown friction type {ftype!r}; one of {', '.join(TYPES)}")
    detail = " ".join((detail or "").split())[:300]
    event(conn, actor, "friction", ftype, "record",
          {"type": ftype, "detail": detail, "agent": agent, "task": task, "source": source,
           "key": _key(ftype, detail), "tool": tool, "command": command})


def classify_cause(text):
    """The fixed-set cause named at the start of `text` (or anywhere as a whole word), else 'unclassified'."""
    t = (text or "").strip().lower()
    m = re.match(r"(%s)\b" % "|".join(CAUSES), t) or re.search(r"\b(%s)\b" % "|".join(CAUSES), t)
    return m.group(1) if m else "unclassified"


def _agent_ctx(conn, task=None, agent=""):
    """(role, model family) of the agent that did the work: the named agent, else the task's latest non-reviewer."""
    from .metrics import family
    q, a = "SELECT agent_id, agent_type, model FROM agents WHERE ", []
    if agent:
        q, a = q + "agent_id=? ORDER BY id DESC LIMIT 1", [agent]
    elif task:
        q, a = q + "task_id=? AND agent_type!='reviewer' AND NOT (agent_type='' AND agent_id LIKE 'reviewer-%') ORDER BY id DESC LIMIT 1", [int(task)]
    else:
        return "", ""
    r = conn.execute(q, a).fetchone()
    return (agent_role(r), family(r["model"])) if r else ("", "")


def record_deviation(conn, actor, task, cause, criterion="", detail="", agent="", source=None):
    """A deviation from the spec with its cause: one of CAUSES ('unclassified' only from an agent trailer)."""
    if cause not in CAUSES + ("unclassified",):
        raise AesirError(f"unknown deviation cause {cause!r}; one of {', '.join(CAUSES)}")
    role, model = _agent_ctx(conn, task, agent)
    event(conn, actor, "deviation", task or "", "record",
          {"cause": cause, "criterion": (criterion or "")[:300], "detail": (detail or "")[:300],
           "source": source or actor, "role": role, "model": model, "agent": agent, "task": task})


def parse_trailer(text):
    """(friction line or '', (criterion, cause_text) or None) from the last `friction:`/`deviation:` lines."""
    fr = FRICTION_RE.findall(text or "")
    dv = DEVIATION_RE.findall(text or "")
    f = fr[-1] if fr and not NONE_RE.match(fr[-1]) and fr[-1] else ""
    d = None
    if dv and dv[-1] and not NONE_RE.match(dv[-1]):
        crit, sep, cause = dv[-1].rpartition(" -- ")
        d = (crit, cause) if sep else (dv[-1], "")
    return f, d


def _result_text(c):
    if isinstance(c, str):
        return c
    return " ".join(b.get("text", "") for b in c or [] if isinstance(b, dict))


HOOK_RE = re.compile(r"\b(?:Pre|Post)ToolUse\S*\s+hook\b|\bhook (?:blocking )?error\b|\bhook (?:denied|blocked)\b"
                     r"|^\s*(?:\w+:\s*)?Aesir(?: scope)?:", re.I)
EXIT_RE = re.compile(r"^\s*(?:\w+:\s*)?Exit code \d+", re.I)


def classify_error(text):
    """Type for a failed tool call. hook_failure only for real hook signals (Claude Code hook error/block text,
    Aesir hook denials); a nonzero Bash/PowerShell exit is command_failed (ordinary, not flagged in status)."""
    head = text[:200]
    if HOOK_RE.search(head):
        return "hook_failure"
    if EXIT_RE.match(head):
        return "command_failed"
    t = text.lower()
    if "permission" in t or "denied" in t or "not allowed" in t:
        return "permission_denial"
    return "tool_error"


# Second words safe to keep (fixed verbs, never user data); anything else is dropped (privacy, #085).
SUBCOMMANDS = {
    "git": {"add", "branch", "checkout", "cherry-pick", "clone", "commit", "diff", "fetch", "log", "merge", "pull",
            "push", "rebase", "reset", "restore", "show", "stash", "status", "switch", "tag", "worktree"},
    "gh": {"api", "issue", "pr", "release", "repo", "run", "auth"},
    "npm": {"install", "ci", "run", "test", "build", "publish", "start"},
    "cargo": {"build", "check", "clippy", "fmt", "run", "test", "install", "publish"},
    "pip": {"install", "uninstall", "list", "show", "freeze"},
    "docker": {"build", "compose", "exec", "images", "logs", "ps", "pull", "push", "run", "stop"},
    "make": {"all", "build", "check", "clean", "install", "lint", "test"},
}


def norm_command(tool, inp):
    """Short stable name for a failing call: 'aesir task show', 'git rebase', 'Edit'. Never the arguments."""
    cmd = inp.get("command") if isinstance(inp, dict) else None
    if tool not in ("Bash", "PowerShell") or not isinstance(cmd, str):
        return tool or ""
    m = re.search(r"\baesir\s+(?:--\S+\s+(?:\S+\s+)?)*([a-z][\w-]*)(?:\s+([a-z][\w-]*))?", cmd)
    if m:
        return " ".join(["aesir"] + [g for g in m.groups() if g])
    words = [w for w in re.split(r"[\s;|&]+", cmd.strip()) if w and "=" not in w.split("/")[0]]
    if not words:
        return tool
    prog = re.sub(r"^.*[\\/]", "", words[0]).removesuffix(".exe")
    sub = words[1] if len(words) > 1 else ""
    # only classifies other people's failing commands by launcher name; nothing here invokes python
    if re.fullmatch(r"python\d*(\.\d+)?|py", prog) and sub == "-m" and len(words) > 2 and re.fullmatch(r"[A-Za-z_][\w.]*", words[2]):
        return f"{prog} -m {words[2]}"
    if sub in SUBCOMMANDS.get(prog, ()):
        return f"{prog} {sub}"
    return prog  # anything else may carry a secret or path (echo secret, cat ~/.ssh/id_rsa): keep the program only


def transcript_errors_ctx(records, limit=20):
    """[(type, detail, tool, command)] for each tool_result with is_error; the call is found via tool_use_id."""
    uses, found = {}, []
    for r in records:
        m = r.get("message")
        c = m.get("content") if isinstance(m, dict) else None
        for b in c if isinstance(c, list) else []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                uses[b.get("id")] = (b.get("name", ""), b.get("input"))
            elif r.get("type") == "user" and b.get("type") == "tool_result" and b.get("is_error"):
                t = _result_text(b.get("content"))
                tool, inp = uses.get(b.get("tool_use_id"), ("", None))
                found.append((classify_error(t), t, tool, norm_command(tool, inp) if tool else ""))
    return found[:limit]


def transcript_errors(records, limit=20):
    """[(type, detail)] for each tool_result with is_error in an iterable of transcript records."""
    return [(t, d) for t, d, _, _ in transcript_errors_ctx(records, limit)]


def capture_stop(conn, actor, row, records, last_message, cfg=None):
    """SubagentStop: tool errors from the transcript, plus the agent's friction/deviation trailer."""
    cfg = cfg or DEFAULTS
    aid, tid = row["agent_id"], row["task_id"]
    for ftype, detail, tool, cmd in transcript_errors_ctx(records, cfg["max_per_agent"]):
        record(conn, actor, ftype, detail, aid, tid, tool=tool, command=cmd)
    f, d = parse_trailer(last_message)
    if f:
        record(conn, actor, "reported", f, aid, tid, source="agent")
    if d:
        record_deviation(conn, actor, tid, classify_cause(d[1]), d[0], d[1], aid, source="agent")


def false_red(conn, actor, task, profile, sha):
    """Call after a gate PASS: an earlier FAIL of the same profile on the same sha (no change) was a false red."""
    if task is None:
        return
    n = conn.execute("SELECT COUNT(*) FROM gate_runs WHERE task_id=? AND profile=? AND sha=? AND passed=0",
                     (int(task), profile, sha)).fetchone()[0]
    if n:
        record(conn, actor, "false_red", f"gate {profile} red then green on {sha[:8]} with no change", task=task)


def queue_failure(conn, actor, task, reason):
    """Merge-queue failures that are environment friction rather than a real red gate."""
    if reason.startswith(("rebase onto", "merge conflict", "ff-merge failed", "cannot create worktree")):
        record(conn, actor, "merge_conflict", reason, task=task)


def link(conn, actor, key, target):
    if not re.match(r"^(task|tip):\d+$", target):
        raise AesirError("target must be task:<id> or tip:<id>")
    if not conn.execute("SELECT 1 FROM events WHERE entity='friction' AND action='record' AND "
                        "json_extract(payload,'$.key')=?", (key,)).fetchone() \
            and not (key.startswith("cause:") and any(g["key"] == key for g in triage(conn))):
        raise AesirError(f"no such friction key: {key}")
    event(conn, actor, "friction", key, "link", {"target": target})


def _events(conn, entity, action, since):
    for r in conn.execute("SELECT id, ts, entity_id, payload FROM events WHERE entity=? AND action=? AND ts>=? "
                          "ORDER BY id", (entity, action, since or "")):
        yield r, json.loads(r["payload"] or "{}")


def recurring(conn, cfg=None, since=""):
    """Friction keys seen >= recurring_min times: [{key, count, type, last, link, stopped}], most frequent first.
    link: the newest task/tip linked to the key (or ''). stopped: a link exists and nothing recorded after it."""
    cfg = cfg or DEFAULTS
    seen = defaultdict(lambda: {"count": 0, "type": "", "last": "", "last_id": 0, "detail": ""})
    for r, p in _events(conn, "friction", "record", since):
        s = seen[p.get("key") or p.get("type")]
        s.update(count=s["count"] + 1, type=p.get("type", ""), last=r["ts"], last_id=r["id"],
                 detail=p.get("detail", ""))
    links = {}
    for r, p in _events(conn, "friction", "link", ""):
        links[r["entity_id"]] = (r["id"], p.get("target", ""))
    out = []
    for k, s in seen.items():
        if s["count"] < cfg["recurring_min"]:
            continue
        lid, target = links.get(k, (0, ""))
        out.append({"key": k, "count": s["count"], "type": s["type"], "last": s["last"], "detail": s["detail"],
                    "link": target, "stopped": bool(lid) and s["last_id"] < lid})
    return sorted(out, key=lambda x: (-x["count"], x["key"]))


def status_flags(conn, cfg=None):
    """Recurring `reported` frictions with no linked task or tip, for `aesir status`. Every other type is owned by
    triage_flags (recurring_cause), so one hiccup raises one flag, not two."""
    return [x for x in recurring(conn, cfg) if not x["link"] and x["type"] == "reported"]


def report(conn, since="", cfg=None):
    """Stage-report section: friction counts by type, recurring frictions, deviation rate by cause/model/role."""
    from .metrics import family
    cfg = cfg or DEFAULTS
    by_type = Counter(p.get("type") for _, p in _events(conn, "friction", "record", since))
    devs = list(_events(conn, "deviation", "record", since))
    agents = {"model": Counter(), "role": Counter()}
    for a in conn.execute("SELECT agent_id, agent_type, model FROM agents WHERE ended!='' AND started>=?", (since or "",)):
        agents["role"][agent_role(a)] += 1
        agents["model"][family(a["model"])] += 1
    dev_by = {"model": Counter(p.get("model") or "?" for _, p in devs),
              "role": Counter(p.get("role") or "?" for _, p in devs)}
    rate = {k: {name: {"deviations": n, "agents": agents[k].get(name, 0),
                       "rate": round(n / agents[k][name], 2) if agents[k].get(name) else None}
                for name, n in sorted(dev_by[k].items())} for k in dev_by}
    half = len(devs) // 2
    late = sum(1 for _, p in devs[half:] if p["cause"] == "context")
    early = sum(1 for _, p in devs[:half] if p["cause"] == "context")
    return {"by_type": dict(sorted(by_type.items())), "recurring": recurring(conn, cfg, since),
            "deviations": len(devs),
            "deviation_by_cause": dict(sorted(Counter(p["cause"] for _, p in devs).items())),
            "deviation_rate": rate, "context_rising": late > early and late >= 2}


def stage_section(conn, since=""):
    """The friction section for stage_report (config read from the project's aesir.toml)."""
    from . import db
    try:
        cfg = load_config(db.project_root())
    except Exception:
        cfg = DEFAULTS
    return report(conn, since, cfg)


def render(d):
    print("friction:")
    print("  by type: " + (", ".join(f"{k} {v}" for k, v in d["by_type"].items()) or "none"))
    for x in d["recurring"][:5]:
        state = (f"linked {x['link']}, " + ("stopped" if x["stopped"] else "still recurring")) if x["link"] else "UNLINKED"
        print(f"  recurring x{x['count']} {x['key']}: {state}")
    print(f"  deviations: {d['deviations']} by cause: "
          + (", ".join(f"{k} {v}" for k, v in d["deviation_by_cause"].items()) or "none"))
    for k in ("model", "role"):
        print(f"  deviations per ended agent by {k}: "
              + (", ".join(f"{n} {v['deviations']}/{v['agents']}" for n, v in d["deviation_rate"][k].items()) or "none"))
    if d["context_rising"]:
        print("  WARNING: 'context' deviations rising; if overhead was trimmed (#35), it was trimmed too far")


# --- triage (#085): group events by normalised cause -------------------------------------------------------
# Ordered (pattern, class): the first match on the detail text names the error class, so the same hiccup with
# different paths/numbers lands in one group.
ERROR_CLASSES = (
    (r"hook error.*migrat", "hook: db migrate blocked"),
    (r"Aesir scope:", "hook: outside task scope"),
    (r"isolated in the worktree.*too complex", "worktree: command too complex"),
    (r"isolated in the worktree", "worktree: git redirect refused"),
    (r"hook (?:blocking )?error|PreToolUse|PostToolUse", "hook: other"),
    (r"unrecognized arguments|unknown argument|invalid choice|the following arguments are required|\busage: ",
     "bad arguments / usage"),
    (r"timed out", "timeout"),
    (r"FileNotFoundError|No such file|cannot find (?:the )?path|does not exist|File not found|ENOENT", "file not found"),
    (r"OperationalError|no such (?:column|table)", "sqlite error"),
    (r"unexpected EOF|syntax error|SyntaxError|ParserError|unexpected token", "shell/python syntax"),
    (r"dirty tree|uncommitted", "dirty tree"),
    (r"no passing|gate .*FAIL", "gate failed"),
    (r"Traceback", "python traceback"),
    (r"permission|denied|not allowed", "permission denied"),
    (r"not found|not recognized", "command not found"),
    (r"(?-i:FAILED|AssertionError|\bFAIL\b|test result: FAILED)", "test failure"),
)


def _cmd_from_detail(detail):
    """Best-effort command for events recorded before `command` existed: an `aesir <sub> [<sub>]` mention.
    Excludes "aesir is ..." / "aesir scope ..." because those are prose (hook text such as "Aesir scope: ..."),
    not subcommands; matching them would invent bogus commands and split groups."""
    m = re.search(r"\baesir\s+([a-z][\w-]*)(?:\s+([a-z][\w-]*))?", detail or "")
    return " ".join(["aesir"] + [g for g in m.groups() if g]) if m and m.group(1) not in ("is", "scope") else ""


def error_class(ftype, detail):
    """Normalised error class: a known class label, else the first words with numbers/paths/hex squashed."""
    for pat, label in ERROR_CLASSES:
        if re.search(pat, detail or "", re.I):
            return label
    t = re.sub(r"(?:[A-Za-z]:)?[\\/][\w.\\/-]+|0x[0-9a-f]+|[0-9a-f]{7,}|\d+", "#", detail or "")
    t = re.sub(r"^(?:\w+:\s*)?Exit code #\s*", "", t)
    w = re.sub(r"[^A-Za-z# ]", " ", t).split()
    return " ".join(w[:6]).lower() or ftype


def triage_key(ftype, command, klass):
    return "cause:" + "/".join((ftype, command or "-", klass))


def triage(conn, since="", top=0):
    """Friction events grouped by (type, command, error class): [{key, type, command, class, count, roles,
    first, last, example, link, stopped}], most frequent first; top>0 keeps the first N. `reported` events group
    by their own text key. link/stopped as in recurring()."""
    groups = {}
    for r, p in _events(conn, "friction", "record", since):
        ftype, detail = p.get("type", ""), p.get("detail", "")
        if ftype == "reported":
            cmd, klass, key = "", p.get("key", ""), p.get("key") or "reported"
        else:
            cmd = p.get("command") or _cmd_from_detail(detail)
            klass = error_class(ftype, detail)
            key = triage_key(ftype, cmd, klass)
        g = groups.setdefault(key, {"key": key, "type": ftype, "command": cmd, "class": klass, "count": 0,
                                    "roles": Counter(), "first": r["ts"], "last": "", "last_id": 0, "example": detail})
        g["count"] += 1
        g["roles"][id_role(conn, p.get("agent")) or "main"] += 1
        g["last"], g["last_id"] = r["ts"], r["id"]
    links = {r["entity_id"]: (r["id"], p.get("target", "")) for r, p in _events(conn, "friction", "link", "")}
    out = []
    for g in groups.values():
        lid, target = links.get(g["key"], (0, ""))
        out.append({**{k: v for k, v in g.items() if k != "last_id"}, "roles": dict(g["roles"].most_common()),
                    "link": target, "stopped": bool(lid) and g["last_id"] < lid})
    out.sort(key=lambda x: (-x["count"], x["key"]))
    return out[:top] if top else out


def triage_flags(conn, cfg=None):
    """Recurring UNLINKED cause groups (quiet types included: command_failed is the bulk of agent hiccups) for status.
    `reported` groups are left to status_flags, which covers them (and only them)."""
    cfg = cfg or DEFAULTS
    return [g for g in triage(conn) if g["count"] >= cfg["recurring_min"] and not g["link"]
            and g["type"] not in ("reported", "no_context")]


def render_triage(groups):
    if not groups:
        print("no friction events")
    for g in groups:
        roles = ", ".join(f"{k} {v}" for k, v in g["roles"].items())
        state = (f"linked {g['link']}" + (", stopped" if g["stopped"] else ", still recurring")) if g["link"] else "UNLINKED"
        print(f"x{g['count']} {g['type']} | {g['command'] or '-'} | {g['class']}  [{state}]")
        print(f"    roles: {roles}; {g['first']} .. {g['last']}")
        print(f"    e.g. {g['example'][:140]}")
        print(f"    key: {g['key']}")
