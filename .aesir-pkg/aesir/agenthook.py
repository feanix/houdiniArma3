"""Claude Code hook entry (`python -m aesir hook`): binds each subagent to an Aesir instance id.

Flow: PreToolUse(Agent) allocates <role>-<task>-<letter> and injects an "Aesir-Agent: <id>" first line into the
subagent's prompt. The harness's own agent_id is bound to it at SubagentStart (or the subagent's first tool call) by
reading that line back from the subagent transcript. A bound agent's Bash/PowerShell commands get AESIR_AGENT set
by PreToolUse; SubagentStop ends it and sums token usage. Always exits 0 and does nothing outside an Aesir project
(no database file found). Never creates a database."""
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import audit, db, friction, gates, install, messages, metrics, observe, ownerwait, proc, scratch, skew, tips, tracks, service as S
from .invoke import cmd as client_cmd
from .rolenames import agent_role, resolve_role, session_prefix

TRAILER_RE = re.compile(r"(?m)^Aesir-Agent:\s*([A-Za-z0-9_-]+)(?:[ \t]+\([^()\r\n]*\))?[ \t]*$")  # '<id>' or '<id> (<model>)\'
ROLE_RE = re.compile(r"(?im)^\W*Role:\s*([A-Za-z][\w-]*)")
TASK_RE = re.compile(r"(?i)\btask\s*#\s*(\d+)")
NAME_RE = re.compile(r"(?im)^\W*agent\s+name:\s*(" + S.AGENT_NAME_PAT + r")\s*$")
BRANCH_RE = re.compile(r"refs/heads/task/(\d+)-")
HEADER_PREFIX = "Aesir spawn-context v1"
# the Workflow harness indents every line of a computed task, so the header may carry leading whitespace
HEADER_RE = re.compile(r"(?m)^[ \t]*" + re.escape(HEADER_PREFIX) + r"\b")
HEADER_START_RE = re.compile(r"\s*" + re.escape(HEADER_PREFIX) + r" task (\d+) type ([A-Za-z][\w-]*)")
HEADER_FIELDS_RE = re.compile(r"(?m)^[ \t]*" + re.escape(HEADER_PREFIX) + r" task (\d+) type ([A-Za-z][\w-]*)")


def _warn(what, exc):
    """One stderr line for a hook step that failed and fell back to a default (#130); the hook never crashes."""
    print(f"aesir hook: {what}: {exc}", file=sys.stderr)


def _degraded(conn, what, exc):
    """A silent default made loud (#130, D115: the guard may fail open but never silently): one stderr line AND a
    hook_failure friction event, so a broken guard shows in `status`. Best-effort: a db that is itself the problem
    only gets the stderr line."""
    _warn(what, exc)
    try:
        with db.tx(conn):
            friction.record(conn, "aesir-hook", "hook_failure", f"{what}: {exc}")
    except Exception as e:  # the friction event is the extra; the line above already went out
        _warn("friction not recorded", e)


def _sanitize(role):
    return re.sub(r"[^a-z0-9_]", "_", role.lower()).strip("_") or "agent"


def _norm(p):
    return os.path.normcase(os.path.realpath(p))


def agent_def_model(root, agent_type):
    """`model:` from <root>/.claude/agents/<type>.md front matter, else ''."""
    f = Path(root) / ".claude" / "agents" / f"{agent_type}.md"
    try:
        text = f.read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(r"(?m)^model:\s*(\S+)", text.split("---")[1] if text.startswith("---") else "")
    return m.group(1) if m else ""


def _subagent_transcript(d):
    """The subagent's transcript: the stop event names it; else <session>/subagents/agent-<id>.jsonl, or (workflow
    agents) <session>/subagents/workflows/<run>/agent-<id>.jsonl."""
    tp, aid = d.get("transcript_path"), d.get("agent_id")
    if d.get("agent_transcript_path"):
        return Path(d["agent_transcript_path"])
    if not (tp and aid):
        return None
    base = Path(tp).with_suffix("") / "subagents"
    direct = base / f"agent-{aid}.jsonl"
    if direct.exists():
        return direct
    return next(iter(base.glob(f"workflows/*/agent-{aid}.jsonl")), direct)


def _known_path(path):
    """The transcript path as text when the file exists now, else '' (a guessed path would mislead rounds, #165)."""
    return str(path) if path and Path(path).is_file() else ""


def _records(path, limit=None):
    try:
        with open(path, encoding="utf-8") as f:
            for i, line in enumerate(f):
                if limit and i >= limit:
                    return
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


def _text(content):
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content or [] if isinstance(b, dict))


def transcript_instance(path):
    """The Aesir-Agent id on the first user message of a subagent transcript (written by the harness)."""
    for r in _records(path, 10):
        if r.get("type") == "user" and "message" in r:
            m = TRAILER_RE.search(_text(r["message"].get("content")))
            return m.group(1) if m else None
    return None


# The harness's literal record prefixes ('[Workflow harness <em dash> user request]' and '... computed task]'). The dash
# is U+2014 in the real text, so match any non-word run between the words instead of one spelling of it.
RELAY_RE = re.compile(r"\[Workflow harness\W+user request\]")
COMPUTED_RE = re.compile(r"\[Workflow harness\W+computed task\]")


def transcript_prompt(path):
    """Text of the user message that carries the agent's prompt ('' if unreadable). A workflow agent may get a
    harness relay of the user request first and the computed task second, so look at the first three user messages
    and take the first one that matches, trying these in order (a later message is preferred where noted):
    1 Aesir-Agent trailer; 2 spawn-context header (last); 3 'Role:' line (last); 4 'Agent name:' line (last);
    5 the computed-task marker or a 'task #N' (first); 6 any message that is not the relay (first);
    else the first message. The relay is never chosen while another message exists (#237, VT #261 round 1)."""
    msgs = []
    for r in _records(path, 10):
        if r.get("type") == "user" and "message" in r:
            msgs.append(_text(r["message"].get("content")))
            if len(msgs) >= 3:
                break
    for test, newest_first in (
            (TRAILER_RE.search, False), (HEADER_FIELDS_RE.search, True), (ROLE_RE.search, True),
            (NAME_RE.search, True), (lambda t: COMPUTED_RE.match(t) or TASK_RE.search(t), False),
            (lambda t: not RELAY_RE.match(t), False)):
        for t in (reversed(msgs) if newest_first else msgs):
            if test(t):
                return t
    return msgs[0] if msgs else ""


def transcript_usage(path):
    """({input, output, cache_read, cache_create}, model) summed per distinct assistant message id."""
    per, model = {}, ""
    for r in _records(path):
        m = r.get("message")
        if not isinstance(m, dict) or m.get("role") != "assistant" or not m.get("usage"):
            continue
        model = model or m.get("model", "")
        u, cur = m["usage"], per.setdefault(m.get("id") or r.get("uuid"), {})
        for k, f in (("input", "input_tokens"), ("output", "output_tokens"),
                     ("cache_read", "cache_read_input_tokens"), ("cache_create", "cache_creation_input_tokens")):
            cur[k] = max(cur.get(k, 0), int(u.get(f) or 0))
    tot = {k: sum(v.get(k, 0) for v in per.values()) for k in ("input", "output", "cache_read", "cache_create")}
    return tot, model


def _adoption(prompt, agent_type):
    """(role, task, agent_type) for a Workflow agent no PreToolUse(Agent) saw, or None to leave it unbound.
    Precedence when several identities are present: Aesir-Agent trailer (the caller never gets here: it binds the
    pre-allocated id) > spawn-context header ('Aesir spawn-context v1 task N type <role>') > 'Role:' line (with
    'Task #N') > the harness agentType when it is a known role (implementer/reviewer/runner/researcher; no task).
    general-purpose / workflow-subagent / unknown types yield None."""
    h = HEADER_FIELDS_RE.search(prompt)
    if h:
        return _sanitize(h.group(2)), int(h.group(1)), agent_type or h.group(2)
    r = ROLE_RE.search(prompt)
    if r:
        t = TASK_RE.search(prompt)
        role = _sanitize(r.group(1))
        return role, _name_or_task(prompt, t), agent_type
    if agent_type in tracks.BASES:  # #207: a 'task #N' in the prompt still names the task
        t = TASK_RE.search(prompt)
        return agent_type, _name_or_task(prompt, t), agent_type
    return None


def _name_or_task(prompt, task_match):
    """The task a prompt gives: the one in its 'Agent name: <role>-N-x' line, else the 'task #M' text. The name wins
    when both are present and differ (#237): it is the line written for this purpose, 'task #M' may be a mention."""
    return name_task(prompt_name(prompt)) or (int(task_match.group(1)) if task_match else None)


def prompt_name(prompt):
    """The agent id a spawn prompt asks for ('Agent name: implementer-238-a'), else None (#207)."""
    m = NAME_RE.search(prompt)
    return m.group(1) if m else None


def name_task(name):
    """Task N from a well-formed name '<prefix>-<N>[-]<x>' (#237), else None. The prefix is not compared with the
    role (#248, reports#153): 'checker-261' names task 261 for a reviewer. For a prompt with no 'task #N' and no
    header, the name is the only place that says which task the agent works on."""
    m = S.AGENT_NAME_RE.fullmatch(name or "")
    return (int(m.group(2)) or None) if m else None


def transcript_model(path):
    """The model id of the first assistant message in the agent's own transcript ('' until it has spoken). Read per
    agent so two parallel agents of different models never share a value (#207, reports#140). Not shared with
    transcript_usage: that one needs a usage block and sums the whole file, this stops at the first message and must
    work for a message without usage (and skips '<synthetic>')."""
    for r in _records(path):
        m = r.get("message")
        if r.get("type") == "assistant" and isinstance(m, dict) and m.get("model") not in (None, "", "<synthetic>"):
            return m["model"]
    return ""


def _result_texts(r):
    """The text of each tool_result block in a user record (the output of a command the agent ran)."""
    m = r.get("message")
    if r.get("type") != "user" or not isinstance(m, dict) or not isinstance(m.get("content"), list):
        return
    for b in m["content"]:
        if isinstance(b, dict) and b.get("type") == "tool_result":
            c = b.get("content")
            yield c if isinstance(c, str) else _text(c)


def transcript_header_task(path, limit=300):
    """Task N from a spawn-context header the agent read, else None (#207). Only the FIRST line of a tool_result counts
    (that is where `spawn-context` prints it): the agent's own text, tool inputs, and a file or test that merely
    contains a header line mid-text never bind. Bounded: the first `limit` lines, and a line is decoded only when
    it holds the header text at all (the common line is skipped without json parsing, which was the per-call cost)."""
    try:
        with open(path, encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i >= limit:
                    return None
                if HEADER_PREFIX not in line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                for t in _result_texts(r):
                    h = HEADER_START_RE.match(t)
                    if h:
                        return int(h.group(1))
    except OSError:
        return None
    return None


def worktree_branch_task(worktree):
    """Task N when the git worktree's checked-out branch is task/N-*, else None (#207). Reads HEAD from the
    worktree's gitdir directly: this runs on every tool call of a task-less agent, so no subprocess. `worktree` may
    be a subdirectory (walks up, at most 8 levels, to the dir holding .git); a relative 'gitdir:' resolves against
    the dir holding the .git file."""
    if not worktree:
        return None
    try:
        top = Path(worktree)
        for _ in range(8):
            if (top / ".git").exists():
                break
            if top.parent == top:
                return None
            top = top.parent
        g = top / ".git"
        gitdir = g
        if g.is_file():
            gitdir = Path(g.read_text(encoding="utf-8").split("gitdir:", 1)[1].strip())
            if not gitdir.is_absolute():
                gitdir = top / gitdir
        m = BRANCH_RE.search((gitdir / "HEAD").read_text(encoding="utf-8"))
    except (OSError, IndexError):
        return None
    return int(m.group(1)) if m else None


def _late_task(conn, row, d, root):
    """#207: a row bound with no task learns it from evidence that appears later: the spawn-context header the agent
    read, or its worktree's branch task/N-*. Best-effort; the row is returned refreshed."""
    cwd = d.get("cwd") or ""
    wt = row["worktree"] or _cwd_worktree(d)
    task, why = worktree_branch_task(wt), "worktree branch"
    if task is None:
        path = row["transcript_path"] or _known_path(_subagent_transcript(d))
        task, why = (transcript_header_task(path) if path else None), "spawn-context header read"
    if task is None:
        return row
    try:
        S.bind_task(conn, "aesir-hook", row["agent_id"], task, why)
    except S.AesirError:  # unknown task number: leave the row alone
        return row
    return S.find_agent(conn, row["harness_agent_id"])


def _late_prompt(conn, row, d, root=None):
    """#237: a task-less row re-reads its prompt (first few records only). The row may have been created before the
    prompt was readable (SubagentStart saw only the relay record, so it got <role>-000-<x> and no task): take the
    prompt's name when the row still has a 000 name, and bind the task the prompt or its name gives.
    Best-effort; the row is returned refreshed."""
    path = row["transcript_path"] or _known_path(_subagent_transcript(d))
    prompt = transcript_prompt(path) if path else ""
    name = prompt_name(prompt) if prompt else None
    if not name:
        return row
    task = name_task(name)
    if task is None:
        a = _adoption(prompt, row["agent_type"])
        task = a[1] if a and a[0] == row["role"] else None
    if task is None:
        return row
    if re.fullmatch(r"[a-z][a-z0-9_]*-000-[a-z]+", row["agent_id"]) and name_task(name):
        S.rename_agent(conn, "aesir-hook", row["agent_id"], name, root)  # None (no rename) when the old id is in use
    row = S.find_agent(conn, row["harness_agent_id"])
    try:
        S.bind_task(conn, "aesir-hook", row["agent_id"], task, "prompt name")
    except S.AesirError:  # unknown task number: keep the name, leave the task null
        return row
    return S.find_agent(conn, row["harness_agent_id"])


def ensure_bound(conn, d, wait=0.0, reopen=False):
    """The Aesir instance row for the harness agent in `d`, binding it first if needed. A workflow agent (no
    Aesir-Agent line, since PreToolUse(Agent) never saw it) gets an id allocated from the 'Role:' / 'Task #N'
    lines of its prompt. reopen: a bound agent whose row ended (it was resumed) gets a fresh running row."""
    harness = d.get("agent_id")
    if not harness:
        return None
    row = S.find_agent(conn, harness)
    if row:
        if not row["transcript_path"]:  # the file did not exist at bind time (#165): record it once it does
            known = _known_path(_subagent_transcript(d))
            if known:
                conn.execute("UPDATE agents SET transcript_path=? WHERE id=?", (known, row["id"]))
                row = S.find_agent(conn, harness)
        if row["ended"]:
            return S.reopen_agent(conn, "aesir-hook", harness) if reopen else row
        if not row["model"] and row["transcript_path"]:  # adopted before the agent's first message: fill it in now
            model = transcript_model(row["transcript_path"])
            if model:
                conn.execute("UPDATE agents SET model=? WHERE id=?", (model, row["id"]))
                row = S.find_agent(conn, harness)
        if not row["task_id"]:
            root = _proj(d)
            row = _late_prompt(conn, row, d, root)
        if not row["task_id"]:
            row = _late_task(conn, row, d, root)
        return row
    path = _subagent_transcript(d)
    inst, prompt = None, ""
    for _ in range(int(wait / 0.2) + 1):
        prompt = path and transcript_prompt(path) or ""
        m = TRAILER_RE.search(prompt)
        inst = m.group(1) if m else None
        if inst or prompt or not wait:
            break
        time.sleep(0.2)
    cwd = d.get("cwd") or ""
    wt = _cwd_worktree(d)
    adopt = None if inst or not prompt else _adoption(prompt, d.get("agent_type") or "")
    if adopt:
        role, task, atype = adopt
        # #207: the model is the agent's own (first assistant message), else empty until it speaks; never the agent
        # def's default, which was wrong for any agent spawned with another model (reports#140)
        model = transcript_model(path) if path else ""
        row = S.adopt_agent(conn, "aesir-hook", role, task, harness, atype, wt, model,
                            session=d.get("session_id") or "", transcript=_known_path(path),
                            name=prompt_name(prompt))
        _name_note(prompt_name(prompt), row["agent_id"])
        return row
    if not inst:  # transcript not readable yet: bind only if it is unambiguous (one recent pending, same type)
        cands = S.pending_agents(conn, d.get("agent_type", ""))
        inst = cands[0]["agent_id"] if len(cands) == 1 else None
    if not inst:
        return None
    return S.bind_agent(conn, "aesir-hook", inst, harness, wt, transcript=_known_path(path))


def _spawn(conn, d):
    ti = dict(d.get("tool_input") or {})
    prompt = ti.get("prompt") or ""
    tuid = d.get("tool_use_id") or ""
    stype = ti.get("subagent_type") or "general-purpose"
    if tuid:
        old = conn.execute("SELECT agent_id FROM agents WHERE tool_use_id=?", (tuid,)).fetchone()
        if old:  # hook delivered twice: same instance
            agent_id = old[0]
            return _inject_prompt(ti, prompt, agent_id)
    m = ROLE_RE.search(prompt)
    role = _sanitize(m.group(1) if m else stype)
    t = TASK_RE.search(prompt)
    name = prompt_name(prompt)  # #248: the prompt's 'Agent name:' names the row here too, as for a Workflow agent
    task = name_task(name) or (int(t.group(1)) if t else None)
    model = ti.get("model") or agent_def_model(_proj(d), stype)
    g = S.spawn_agent(conn, "aesir-hook", role, task, name=name,
                      parent=os.environ.get("AESIR_ROLE") or "lead", model=model, agent_type=stype,
                      tool_use_id=tuid, strict=False)
    _name_note(name, g["id"])
    return _inject_prompt(ti, prompt, g["id"])


def _name_note(name, agent_id):
    """#248: a prompt asked for an agent name and the row got another id (the name is taken, names another task, or
    borrows a role or type prefix): say so on stderr, so a lead sees why the row is not named as written."""
    if name and name != agent_id:
        _warn("prompt agent name not used", f"'{name}' was refused (taken, wrong task, or a role/type prefix); row is {agent_id}")


def _inject_prompt(ti, prompt, agent_id):
    prompt = TRAILER_RE.sub("", prompt, count=1).lstrip("\n") if prompt.startswith("Aesir-Agent:") else prompt
    ti["prompt"] = f"Aesir-Agent: {agent_id}\n{prompt}"
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": ti}}


def _shell_env(row, db_file, powershell):
    safe = lambda v: re.sub(r"[^A-Za-z0-9._:/\\ -]", "", v)
    pairs = [("AESIR_AGENT", row["agent_id"]), ("AESIR_MODEL", safe(row["model"]))]
    if powershell:
        return "".join(f"$env:{k}='{v.replace(chr(39), chr(39) * 2)}'; " for k, v in pairs)
    return "export " + " ".join(f"{k}='{v.replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'"
                                for k, v in pairs) + "; "


# Migrate guard (#095, #123, D115). Scope: these guards prevent MISTAKES, not a deliberately hostile agent. They stop
# a subagent from accidentally using the migrate override (the allow-migrate flag / AESIR_ALLOW_MIGRATE) against the
# live db. The backstop in db._guard keys on AESIR_AGENT, which this hook exports into every bound shell and protects
# from casual removal (_marker_attempt). The text guard parses the command into segments and judges each one:
#   - a bare word that IS the override (a `-`/`--` option, or a NAME=value assignment) is refused in any segment;
#   - echo/printf/grep/rg/cat (_INERT) only print or search their text, so mentions there are fine, unless the
#     output is redirected to a file or piped into a non-inert segment (that is how text becomes a script);
#   - text inside a quoted argument or a heredoc body handed to some other command is just text (a commit message,
#     a sed expression, a python script that merely mentions the flag) UNLESS it looks like a real use (STRONG_RE:
#     running aesir with the flag, allow_migrate=True, setting the env var), or the command is a shell/executor
#     that would run the text (_RUNNERS), in which case any mention is refused;
#   - `$` or a backtick next to flag text outside heredoc bodies, an unparseable command, and PowerShell refuse.
# A deliberately hostile agent can still evade text checks (encodings, scripts written by other tools); that is out
# of scope (D115) and we do not chase further expansion splits.
MIGRATE_RE = re.compile(r"allow[-_]?m|allow.{0,16}migr", re.I | re.S)
STRONG_RE = re.compile(r"(?:-m[\"',\s]+|[\"'])aesir\b[\"']?[^\n]*--?allow[-_]?m"
                       r"|allow[-_]?migrate\s*[=:]\s*(?:true|1|yes)|AESIR_ALLOW_MIGRATE[\"'\]\s]*[=:,)]", re.I)
_OPTION_RE = re.compile(r"-+allow[-_]?m|[\w.]+=.*allow[-_]?m|[\w.]*ALLOW[-_]?MIGRATE", re.I | re.S)
_INERT = {"echo", "printf", "grep", "rg", "cat"}  # print or search their arguments; never run them
_FILTERS = {"head", "tail", "wc", "sort", "uniq", "less", "more", "cut", "tr", "nl", "tac", "column"}  # stdin only
_RUNNERS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "pwsh", "powershell", "cmd", "eval", "exec", "xargs", "env",
            "source", ".", "sudo", "nohup", "time", "timeout", "watch", "find", "make", "ssh", "while", "for", "do",
            "then", "if"}  # run (or hand on) text they are given: any flag mention in their words is refused


def _segments(cmd, info=None):
    """Split a POSIX shell command into (segments of dequoted words, file_redirect) -- split on ; & | ( ) { } and
    newlines outside quotes; heredoc bodies are skipped, not parsed. file_redirect is True when an unquoted `>`
    targets anything but /dev/null or an fd. None when it cannot be parsed (unterminated quote or heredoc, a tag glued to
    more word pieces); `<<<` is a here-string with no body.
    `info` (a dict) is filled for callers that need more, keyed by id() of the segment's word list:
    info["bodies"][id] = heredoc body texts, info["piped"] = ids of segments whose output is piped on,
    info["spans"] = (start, end) of each heredoc body in `cmd`, info["hd"] = (start, end, quoted_tag, owner words)
    of each."""
    segs, words, cur, have, i, n, pend, redir = [], [], [], False, 0, len(cmd), [], False
    if info is not None:
        info.update(bodies={}, piped=set(), spans=[], hd=[])

    def flush():
        nonlocal cur, have
        if have:
            words.append("".join(cur))
        cur, have = [], False

    def cut():
        nonlocal words
        flush()
        if words:
            segs.append(words)
        words = []

    while i < n:
        c = cmd[i]
        if c == "\\" and i + 1 < n:
            cur.append(cmd[i + 1]); have = True; i += 2
        elif c in "'\"":
            j = i + 1
            while j < n and cmd[j] != c:
                j += 2 if (c == '"' and cmd[j] == "\\") else 1
            if j >= n:
                return None
            cur.append(cmd[i + 1:j]); have = True; i = j + 1
        elif c == "\n":
            cut(); i += 1
            for tag, owner, quoted in pend:  # skip the bodies of heredocs started on the previous line
                m = re.search(r"^[ \t]*" + re.escape(tag) + r"[ \t\r]*$", cmd[i:], re.M)  # \r: CRLF end line
                if not m:
                    return None
                if info is not None:
                    info["bodies"].setdefault(id(owner), []).append(cmd[i:i + m.start()])
                    info["spans"].append((i, i + m.end()))
                    info["hd"].append((i, i + m.end(), quoted, owner))
                i += m.end() + 1
            pend = []
        elif c in ";&|(){}":
            flush()
            if info is not None and c == "|" and words and not cmd.startswith("||", i):
                info["piped"].add(id(words))
            cut(); i += 1
        elif c == ">":
            flush(); i += 1
            while i < n and cmd[i] in ">|":
                i += 1
            if i < n and cmd[i] == "&":
                i += 1
                if not (i < n and (cmd[i].isdigit() or cmd[i] == "-")):
                    redir = True
                continue
            m = re.match(r"[ \t]*(\S*)", cmd[i:])
            if re.sub(r"[\"'\\]", "", m.group(1)) != "/dev/null":
                redir = True
            i += m.end()
        elif cmd.startswith("<<<", i):
            flush(); i += 3  # here-string: one unit, no body; the word after it (even quoted) is an ordinary argument
        elif cmd.startswith("<<", i):
            flush()
            m = re.match(r"<<-?[ \t]*(?:'([^']*)'|\"([^\"]*)\"|\\?([\w.-]+))", cmd[i:])
            if not m:
                return None
            # bash joins adjacent pieces into one tag word (<<'EO'F is EOF); only the first piece is read here, so a
            # tag followed by anything but a separator is unparseable (fail closed, reports#150 r0 review)
            if i + m.end() < n and cmd[i + m.end()] not in " \t\r\n;|&<>()":
                return None
            # a quoted tag ('EOF', "EOF", \EOF) means the body is not expanded
            quoted = m.group(3) is None or "\\" in m.group(0)
            pend.append((next(g for g in m.groups() if g is not None), words, quoted)); i += m.end()
        elif c in " \t":
            flush(); i += 1
        else:
            cur.append(c); have = True; i += 1
    cut()
    return None if pend else (segs, redir)


_BODY_SINKS = {"cat", "tee"}


def _data_body_spans(cmd):
    """#268 (aesir-reports#150 shape 2): (start, end) spans in `cmd` of quoted-tag heredoc bodies that only a data sink
    (bare cat or tee, not piped on) receives, so a '>' inside them (an f-string '->') is text, not a redirect. Uses the
    spans _segments computes (no second scanner). Empty when unparseable or when the command has any $( ), backtick,
    subshell, group, function or eval (the body could then run or be passed on): main's verdict, every match judged.
    Known gap: a script written by a cat heredoc and then run ('bash s.sh') passes its body redirects; 'bash file' was
    never judged."""
    info = {}
    if _segments(cmd, info) is None:
        return []
    outside = cmd
    for a, b in sorted(info["spans"], reverse=True):
        outside = outside[:a] + outside[b:]
    if re.search(r"[`(){}]|\beval\b|\bfunction\b", outside):
        return []
    out = []
    for a, b, quoted, owner in info["hd"]:
        rest = [w for w in owner if not re.match(r"[A-Za-z_]\w*=", w)]
        if quoted and rest and rest[0] in _BODY_SINKS and id(owner) not in info["piped"]:
            out.append((a, b))
    return out


def _inert(words):
    """True when the segment's command is a bare echo/printf/grep/rg/cat (no path, no rg --pre)."""
    if "/" in words[0] or "\\" in words[0]:
        return False  # path-qualified head: not provably the real echo/cat
    head = re.sub(r"\.exe$", "", words[0], flags=re.I).lower()
    return head in _INERT and not (head == "rg" and any(w.startswith("--pre") for w in words))


def _feeds_runner(segs, k, piped):
    """True when segment k's output is piped into a following segment that is neither inert nor a plain stdin
    filter (head, wc, ...): xargs, sh, tee and the like could run or store it."""
    nxt = segs[k + 1] if k + 1 < len(segs) else None
    return id(segs[k]) in piped and nxt is not None and not _inert(nxt) and nxt[0].lower() not in _FILTERS


def _migrate_attempt(cmd, powershell=False):
    """True (refuse) when `cmd` really uses the migrate flag, per the threat-model comment above. Mentions in
    inert output, quoted text and heredoc bodies pass; each segment of a compound command is judged on its own.
    PowerShell: any mention refuses."""
    cmd = re.sub(r"\\\r?\n", "", cmd)
    if not MIGRATE_RE.search(re.sub(r"[\"'\\]", "", cmd)):
        return False
    if powershell:
        return True
    info = {}
    parsed = _segments(cmd, info)
    if parsed is None:
        return True
    segs, redir = parsed
    outside = cmd
    for a, b in sorted(info["spans"], reverse=True):
        outside = outside[:a] + outside[b:]
    if ("$" in outside or "`" in outside) and MIGRATE_RE.search(re.sub(r"[\"'\\]", "", outside)):
        return True  # expansion could build the flag from pieces: not provably text
    for k, words in enumerate(segs):
        bodies = info["bodies"].get(id(words), [])
        body_hit = [b for b in bodies if MIGRATE_RE.search(b)]
        word_hit = [w for w in words if MIGRATE_RE.search(w)]
        if not (word_hit or body_hit):
            continue
        feeds = _feeds_runner(segs, k, info["piped"])
        if _inert(words):
            if (word_hit and redir) or feeds or (redir and any(STRONG_RE.search(b) for b in body_hit)):
                return True
            continue
        head = re.sub(r"\.exe$", "", words[0], flags=re.I).lower()
        if head in _RUNNERS:
            return True
        for w in word_hit:
            if _OPTION_RE.match(w) or STRONG_RE.search(w):
                return True
        if any(STRONG_RE.search(b) for b in body_hit):
            return True
    return False


AGENT_RE = re.compile(
    r"\bunset\b[^;&|\n]*AESIR_AGENT"  # unset AESIR_AGENT, unset -v AESIR_AGENT, in any segment
    r"|\benv(?:\s+(?:-\S+|\w+=\S*))*?\s+(?:-[a-z]*[iu]|--ignore-environment|--unset)\b"  # env -u / -i
    r"|AESIR_AGENT\s*\+?="  # prefix or export assignment, $env:AESIR_AGENT =
    r"|(?:remove|clear|set)-item\b[^;\n]*AESIR_AGENT|\bri\s+env:\\?AESIR_AGENT",  # PowerShell Env: drive
    re.I)


def _marker_attempt(cmd):
    """True (refuse) when `cmd` unsets or overrides AESIR_AGENT. Plain mentions (echo $AESIR_AGENT) pass, and so
    does text an inert echo/printf/grep/cat prints or a heredoc body given to a non-shell command (#123). Each
    segment of a compound command is judged on its own; an unparseable command is checked as a whole."""
    cmd = re.sub(r"\\\r?\n", "", cmd)
    strip = lambda t: re.sub(r"[\"'\\]", "", t)
    info = {}
    parsed = _segments(cmd, info)
    if parsed is None:
        return bool(AGENT_RE.search(strip(cmd)))
    segs, redir = parsed
    for k, words in enumerate(segs):
        if _inert(words):
            feeds = _feeds_runner(segs, k, info["piped"])
            if (redir or feeds) and AGENT_RE.search(" ".join(words)):
                return True
            if feeds and any(AGENT_RE.search(strip(b)) for b in info["bodies"].get(id(words), [])):
                return True
            continue
        head = re.sub(r"\.exe$", "", words[0], flags=re.I).lower()
        # a quoted argument with spaces (commit message, sed text) is prose unless a shell would run it
        if AGENT_RE.search(" ".join(w for w in words if head in _RUNNERS or not re.search(r"\s", w))):
            return True
        if head in _RUNNERS and any(AGENT_RE.search(strip(b)) for b in info["bodies"].get(id(words), [])):
            return True
    return False


# The role names a bound agent may pass to `--as`: its own instance id and its own role (its agent type, e.g.
# `implementer` for implementer-217-a or checker-217-a; never the id prefix, which is free text, #248). The row's
# `role` column is NOT used: a spawned agent's row carries the spawning session's role (lead). #217 (aesir-reports#81): Workflow shells carried no AESIR_AGENT, so `--as lead` was recorded as lead.
def _own_names(row):
    aid = row["agent_id"]
    return {aid, agent_role(row)}


def _foreign_as(cmd, row):
    """The first `--as` / `--as=` value on an aesir command in `cmd` that names neither the agent's id nor its own role,
    else None. Only segments that run aesir (a word `aesir`, as in `python -m aesir`) are read, so quoted text that merely
    mentions the flag is ignored; an unparseable command is not judged (the migrate guard refuses what it must)."""
    parsed = _segments(cmd)
    if parsed is None:
        return None
    own = _own_names(row)
    for words in parsed[0]:
        if not any(os.path.basename(w.replace("\\", "/")).lower() in ("aesir", "aesir.exe") for w in words):
            continue
        for k, w in enumerate(words):
            val = words[k + 1] if w == "--as" and k + 1 < len(words) else w[5:] if w.startswith("--as=") else None
            if val is not None and val not in own:
                return val
    return None


def _bash(d, row, db_file):
    ti = dict(d.get("tool_input") or {})
    cmd = ti.get("command")
    if not isinstance(cmd, str):
        return None
    if d.get("tool_name") != "PowerShell" and _segments(cmd) is None:  # D221: refused once, up front, for every guard
        why = ("Aesir: this Bash command cannot be parsed, so the guards cannot judge it: an unterminated quote or "
               "heredoc, or a heredoc tag built from glued pieces (<<'EO'F, <<'EOF'x, <<E'O'F). Use a plain 'EOF' "
               "tag with the end line alone on its own line; put long scripts in a file written with Write/Edit. Known "
               "parser limits (#271), also refused: a quote character inside a # comment (# it's) and $(( )) "
               "arithmetic with <<; drop the comment or compute the value another way.")
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": why}}
    if _marker_attempt(cmd):
        why = ("Aesir: the agent marker (AESIR_AGENT) is required in subagent shells; do not unset, override or "
               "clear it (no unset, env -u/-i, assignment, Remove-Item Env:). Ask the lead if you need that.")
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": why}}
    if _migrate_attempt(cmd, d.get("tool_name") == "PowerShell"):
        why = ("Aesir: subagents must not migrate the live db (--allow-migrate / AESIR_ALLOW_MIGRATE). "
               "Branch code that needs a newer schema cannot run against it; use a temp db (--db) for tests, "
               "or ask the lead. Plain bare echo/printf/grep/rg/cat text naming the flag is fine (no file redirect, "
               "no $ or backticks); anything else with the flag is refused, so write the text with Edit/Write.")
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": why}}
    posed = _foreign_as(cmd, row)
    if posed is not None:
        why = (f"Aesir: you are {row['agent_id']}; `--as {posed}` would record your change as {posed}. Drop --as "
               f"(your calls are attributed to {row['agent_id']} automatically) or use `--as {row['agent_id']}`.")
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": why}}
    ti["command"] = _shell_env(row, db_file, d.get("tool_name") == "PowerShell") + cmd
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": ti}}


# a redirect operator (> >> >| 2> *>, Out-File, tee); the target is read after it by _shell_word. fd duplications
# (2>&1, >&2) have '&' where the word would start and yield an empty word. A '>' or '>>' directly followed by '='
# is a comparison (`WHERE n >= 3`, reports#150, D214), not a redirect; no quote masking (a flat scanner cannot match
# bash). `>|` is the noclobber override, so its target follows the '|' (reports#167, #261).
# Known gaps, accepted (the guard is a backstop against stray files, not a sandbox): a write to a file whose name
# starts with '=' is not refused; a target word whose expansion ($var, $(..), backticks, ~) comes before any '/' is
# skipped as unknown (absolute and relative literal prefixes up to a '/' are judged).
_REDIRECT_RE = re.compile(r"""(?:\d?>>?(?!=)\|?|\*>|\btee(?:\s+-a)?|\bOut-File(?:\s+-FilePath)?)\s*""", re.I)
_EXPANSION_RE = re.compile(r"[A-Za-z_{(0-9@*#?!$-]")  # what after a '$' starts an expansion
_ANSI_SIMPLE = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n", "r": "\r", "t": "\t",
                "v": "\v", "\\": "\\", "'": "'", '"': '"', "?": "?"}


def _ansi_c(s, i):
    """Decode a $'..' body starting at s[i] (after the opening quote) with bash's full escape set. Unknown escapes
    keep the backslash, and a hex escape with no digit stays literal. Returns (text, index past the closing quote);
    an unterminated body runs to the end. Raises ValueError on a code point that is a surrogate or above U+10FFFF
    (it cannot be encoded; the caller fails closed)."""
    out = []
    n = len(s)
    while i < n and s[i] != "'":
        c = s[i]
        if c != "\\" or i + 1 >= n:
            out.append(c)
            i += 1
            continue
        e = s[i + 1]
        i += 2
        if e in _ANSI_SIMPLE:
            out.append(_ANSI_SIMPLE[e])
        elif e in "01234567":
            m = re.compile(r"[0-7]{1,3}").match(s, i - 1)
            out.append(chr(int(m.group(), 8) & 0xFF))
            i = m.end()
        elif e in "xuU":
            m = re.compile(r"[0-9A-Fa-f]{1,%d}" % {"x": 2, "u": 4, "U": 8}[e]).match(s, i)
            if m:
                cp = int(m.group(), 16)
                if cp > 0x10FFFF or 0xD800 <= cp <= 0xDFFF:
                    raise ValueError(f"unencodable code point {cp:#x}")
                out.append(chr(cp))
                i = m.end()
            else:
                out.append("\\" + e)
        elif e == "c" and i < n:
            out.append(chr(ord(s[i]) & 0x1F) if s[i] != "?" else "\x7f")
            i += 1
        else:
            out.append("\\" + e)
    return "".join(out).split("\0")[0], i + 1  # bash truncates a word at NUL


def _shell_word(s, i, literal_bs=False):
    """Read one shell word from s[i:] with bash quote removal and return (value, expands, prefix). The word runs to
    unquoted whitespace or one of ; | & < > ( ). Handles '..', "..", $'..', $"..", backslash escapes and any
    concatenation of them. `expands` is True when an unquoted or double-quoted $name, ${, $( or backtick, or a
    leading unquoted ~, makes the value unknowable statically; `prefix` is then the literal text decoded before the
    first expansion marker (else ""). Raises ValueError when a part cannot be decoded.
    An unterminated quote runs to the end of the string. With `literal_bs` an unquoted backslash is an ordinary
    character (a Windows path separator) instead of an escape; quotes are still removed."""
    out = []
    expands = i < len(s) and s[i] == "~"
    prefix = ""
    n = len(s)
    dq = False
    while i < n:
        c = s[i]
        if dq:
            if c == '"':
                dq = False
            elif c == "\\" and i + 1 < n and s[i + 1] in '$`"\\\n':  # real newline: backslash-newline vanishes
                if s[i + 1] != "\n":
                    out.append(s[i + 1])
                i += 1
            elif c == "`" or (c == "$" and i + 1 < n and _EXPANSION_RE.match(s[i + 1])):
                if not expands:
                    expands, prefix = True, "".join(out)
                out.append(c)
            else:
                out.append(c)
            i += 1
        elif c in " \t\r\n;|&<>()":
            break
        elif c == "'":
            j = s.find("'", i + 1)
            j = n if j < 0 else j
            out.append(s[i + 1:j])
            i = j + 1
        elif c == '"':
            dq = True
            i += 1
        elif c == "$" and s[i + 1:i + 2] == "'":
            t, i = _ansi_c(s, i + 2)
            out.append(t)
        elif c == "$" and s[i + 1:i + 2] == '"':
            dq = True
            i += 2
        elif c == "\\" and not literal_bs:
            if i + 1 < n and s[i + 1] != "\n":
                out.append(s[i + 1])
            i += 2
        else:
            if c == "`" or (c == "$" and i + 1 < n and _EXPANSION_RE.match(s[i + 1])):
                if not expands:
                    expands, prefix = True, "".join(out)
            out.append(c)
            i += 1
    return "".join(out), expands, prefix


_CD_RE = re.compile(r"(?:^|[;&|(])\s*(?:cd|pushd|chdir|set-location|sl)\b", re.I)
_CD_ARG_RE = re.compile(r"(?:^|[;&|(])\s*(?:cd|pushd|chdir|set-location|sl)\s+(?:-\w+\s+)?[\"']?([^\s\"';&|)]+)", re.I)
_NULLS = ("/dev/null", "nul", "nul:", "$null")


def _inside_dir(p, folder):
    a, b = _norm(p), _norm(folder)
    return a == b or a.startswith(b + os.sep)


def _native(p):
    """git-bash /e/Projects -> E:/Projects on Windows; other paths unchanged."""
    if os.name == "nt" and re.match(r"/[A-Za-z]/", p):
        return p[1].upper() + ":" + p[2:]
    return p


def _dir_at(cmd, pos, cwd):
    """The directory a command runs in at offset `pos` of `cmd`: `cwd` moved by every cd/pushd/Set-Location before
    it (#168). None when a cd target cannot be resolved statically ($var, ~, `cd -`, a bare cd), so callers skip
    the judgement rather than guess."""
    here = cwd
    for m in _CD_RE.finditer(cmd, 0, pos):
        a = _CD_ARG_RE.match(cmd, m.start())
        t = a.group(1) if a else None
        if not t or "$" in t or t.startswith("~") or t == "-":
            return None
        t = _native(t)
        here = t if os.path.isabs(t) or re.match(r"[A-Za-z]:|/", t) else os.path.join(here, t)
    return here


# git subcommands that move HEAD or rewrite the working tree. In the shared main checkout that yanks the tree from
# under the lead and every other agent (aesir-reports#95, #168); a subagent reads other refs without checking out.
_HEAD_MOVERS = {"checkout", "switch", "reset", "rebase", "merge", "pull", "cherry-pick", "revert"}
_GIT_VALUE_OPTS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}


def _git_head_move(words, here):
    """(subcommand, dir) when git argv `words` (starting at 'git') moves HEAD / the tree, with `dir` the directory it
    acts in after `-C` options; else None."""
    i, d = 1, here
    while i < len(words) and words[i].startswith("-"):
        w = words[i]
        if w == "-C" and i + 1 < len(words):
            t = _native(words[i + 1])
            d = t if os.path.isabs(t) or re.match(r"[A-Za-z]:|/", t) else os.path.join(d, t)
        i += 2 if w in _GIT_VALUE_OPTS else 1
    if i >= len(words):
        return None
    sub = words[i]
    if sub in _HEAD_MOVERS or (sub == "stash" and any(w in ("pop", "apply") for w in words[i + 1:])):
        return sub, d
    return None


def _refuse_head_move(conn, row, d, resp):
    """#168 (aesir-reports#95): refuse a subagent's git command that moves HEAD or changes the working tree of the
    MAIN checkout (checkout, switch, reset, rebase, merge, pull, stash pop/apply, cherry-pick, revert) -- run from a
    cwd inside it, after a cd into it, or via `git -C <main>`. The message names the read-only alternatives and the
    agent's own worktree. `git -C <own worktree> ...` and read-only git pass. Returns the deny, else `resp`."""
    wt = row["worktree"]
    cwd = wt or d.get("cwd")
    cmd = (d.get("tool_input") or {}).get("command") or ""
    if not cwd or not resp or "updatedInput" not in resp["hookSpecificOutput"] or "git" not in cmd:
        return resp
    try:
        main = _norm(_proj(d, wt))
        parsed = _segments(cmd)
        if parsed is None:
            return resp
        here = cwd
        for words in parsed[0]:
            head = re.sub(r"\.exe$", "", words[0], flags=re.I).lower()
            if head in ("cd", "pushd", "chdir") and len(words) > 1 and "$" not in words[1] and words[1] != "-":
                t = _native(words[1])
                here = t if os.path.isabs(t) or re.match(r"[A-Za-z]:|/", t) else os.path.join(here, t)
                continue
            if head != "git":
                continue
            hit = _git_head_move(words, here)
            if hit and _worktree_of(os.path.abspath(hit[1]))[0] == main:
                mine = wt or "your own worktree"
                why = (f"Aesir: `git {hit[0]}` would move HEAD or change the working tree of the main checkout "
                       f"({main}), which every agent shares. Read other branches without checking them out: "
                       "`git show <ref>:<path>`, `git diff A..B`, `git log <ref>`. To work on a branch use your own "
                       f"worktree ({mine}): `git -C <worktree> ...`.")
                return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                               "permissionDecisionReason": why}}
    except Exception as e:  # fails open but loud (D115, #130)
        _degraded(conn, "head-move check skipped", e)
    return resp


# #235: a subagent kills only a process it started. By name/image/pattern it also takes down other agents', the lead's
# and VT's runs on the shared machine (an implementer's `pkill -f aesir.paratest` hit the lead's gate); by PID it must
# be a process whose working directory is the agent's own worktree or a scratchpad it owns.
_KILL_NAME_HEADS = {"pkill", "killall"}
_KILL_HEADS = {"kill", "stop-process", "spps", "taskkill", "tskill", "wmic"} | _KILL_NAME_HEADS
_KILL_WRAP = {"env", "nohup", "sudo", "time", "timeout", "xargs", "start", "call", "command", "exec", "nice",
              "setsid", "&", "stdbuf"}
_KILL_SHELLS = {"sh", "bash", "zsh", "dash", "powershell", "pwsh", "cmd", "eval"}  # their string argument is parsed again
_KILL_FLOW = {"do", "then", "else", "elif", "if", "while", "until", "!"}  # loop/conditional heads before a command
_PIDFILE_RE = re.compile(r"(?:\$\(|\(|`)\s*(?:cat|<|get-content|gc|type)\s+(?:-path\s+)?[\"']?([^\s\"')`]+)[\"']?\s*(?:\)|`)",
                         re.I)
_KILL_ALT = ("Kill only the PID you started: record it when you start a background run (write it to a file beside the "
             "log, e.g. `cmd > run.log 2>&1 & echo $! > run.pid`) and stop just that PID "
             "(git-bash: `kill $(cat run.pid)`; `taskkill //PID <windows pid> //T //F`; PowerShell: `Stop-Process -Id <pid>`).")


def _kill_head(w):
    return re.sub(r"\.exe$", "", os.path.basename(w.replace("\\", "/")), flags=re.I).lower()


def _taskkill_targets(low):
    """taskkill argv (lowercased, without the head). Git-bash passes `//PID` for `/PID`; both forms are read."""
    low = [a[1:] if a.startswith("//") else a for a in low]
    if any(re.match(r"[/-](im|fi)$", a) for a in low):
        return ("name", None)
    pids, k = [], 0
    while k < len(low):
        if re.match(r"[/-]pid$", low[k]) and k + 1 < len(low):
            if not low[k + 1].isdigit():
                return ("unknown", None)
            pids.append(int(low[k + 1]))
            k += 1
        k += 1
    return ("pids", pids) if pids else ("unknown", None)


def _kill_targets(words, msys=False):
    """What one killer argv (its head is already a killer) selects, for the caller to judge:
      ('name', None)  by name, image or command-line pattern (always refused);
      ('unknown', None)  a variable, job, substitution or piped object we cannot resolve statically (refused);
      ('pids', [n..])  Windows PIDs (taskkill, Stop-Process, wmic, and `kill` under PowerShell);
      ('mpids', [n..])  PIDs given to a git-bash `kill`: msys PIDs, not Windows PIDs (`msys=True`);
      ('mwpids', [n..])  `kill -f` / `kill -W`: msys PID or, failing that, a Windows PID, so both are judged;
      None  not a kill (kill -l, wmic query)."""
    head, args = _kill_head(words[0]), words[1:]
    low = [a.lower() for a in args]
    if head in _KILL_NAME_HEADS:
        return ("name", None)
    if head == "wmic":
        text = " ".join(low)
        if "process" not in text or not re.search(r"\b(terminate|delete)\b", text):
            return None
        m = re.search(r"processid\s*=\s*(\d+)", text)
        if m and "name" not in text and "commandline" not in text:
            return ("pids", [int(m.group(1))])
        return ("name", None)
    if head == "taskkill":
        return _taskkill_targets(low)
    if head == "tskill":  # tskill takes a process id or, with no id, a name
        a = [x for x in low if not x.startswith(("/", "//"))]
        if a and a[0].isdigit():
            return ("pids", [int(a[0])])
        return ("unknown", None) if a and "$" in a[0] else ("name", None)
    ops, k, win_flag = [], 0, False
    while k < len(low):
        a = low[k]
        by_name = a in ("-name", "-processname") or a.startswith(("-name:", "-processname:"))
        if head != "kill" and (a == "-n" or a.startswith("-n:")):
            by_name = True  # Stop-Process -n is -Name; for kill, -n takes a signal
        if by_name:
            return ("name", None)
        if re.match(r"-(id|pid)(:|$)", a):
            if ":" in a:
                ops.append(a.split(":", 1)[1])
            else:
                ops.append(low[k + 1] if k + 1 < len(low) else "")
                k += 1
        elif a in ("-s", "-n", "-signal"):
            k += 1  # signal name or number
        elif a in ("-inputobject", "-input") and head != "kill":
            return ("unknown", None)
        elif a == "-l":
            return None
        elif a == "--":
            ops.extend(low[k + 1:])
            break
        elif a.startswith("-"):
            win_flag = win_flag or a in ("-f", "-w")  # kill -f / -W: msys kill may fall back to a Windows PID
            # else a signal or switch (-9, -KILL, -Force, -Confirm:$false)
        else:
            ops.append(a)
        k += 1
    pids = []
    for o in ops:
        for part in filter(None, re.split(r"[,\s]+", o)):
            if part.isdigit():
                pids.append(int(part))
            elif part.startswith(("$", "%", "-", "@", "(")) or "$" in part:
                return ("unknown", None)
            else:
                return ("name", None)
    if not pids:
        return ("name", None) if head in ("stop-process", "spps") else None  # piped Get-Process: selected by name
    if head == "kill" and msys:
        return ("mwpids" if win_flag else "mpids", pids)
    return ("pids", pids)


def _kill_inner(words, shell, cwd, depth):
    """Findings for the command string a shell wrapper (`bash -c`, `powershell -Command`, `cmd /c`) runs: a quoted
    argument is parsed again; unquoted words (`powershell -Command Stop-Process -Name x`) are re-read from the first
    word that names a killer."""
    ps = shell in ("powershell", "pwsh")
    quoted = [w for w in words[1:] if re.search(r"\s", w)]
    if quoted:
        return [f for w in quoted for f in _kill_findings(w, ps, cwd, depth + 1)]
    for k, w in enumerate(words[1:], 1):
        if _kill_head(w) in _KILL_HEADS:
            return _kill_findings(" ".join(words[k:]), ps, cwd, depth + 1)
    return []


def _kill_findings(cmd, powershell, cwd, depth=0):
    """List of (kind, pids) for every kill command in `cmd` (see _kill_targets), shell wrappers and loop heads read
    through. `powershell` False means git-bash, where a plain `kill N` takes an msys PID."""
    cmd = re.sub(r"\\\r?\n", "", cmd)

    def sub(m):
        try:
            p = _native(m.group(1))
            t = Path(p if os.path.isabs(p) or re.match(r"[A-Za-z]:|/", p) else os.path.join(cwd, p)).read_text(
                encoding="utf-8", errors="replace").strip()
            return " " + t + " " if re.fullmatch(r"[\d\s,]+", t) else " $unresolved "
        except OSError:
            return " $unresolved "
    cmd = _PIDFILE_RE.sub(sub, cmd)
    out = []
    if powershell and re.search(r"\b(get-process|gps|get-wmiobject|get-ciminstance|gwmi)\b", cmd, re.I) \
            and re.search(r"\.kill\(|\.terminate\(|\bterminate\b", cmd, re.I):
        out.append(("name", None))
    parsed = _segments(cmd)
    if parsed is None:
        return out + ([("unknown", None)] if re.search(r"\b(kill|taskkill|stop-process)\b", cmd, re.I) else [])
    for words in parsed[0]:
        # `do kill $p`, `then pkill x`: judge the command after the keyword; `FOO=1 pkill x` and
        # `MSYS_NO_PATHCONV=1 taskkill /IM x` (the usual git-bash way to pass /IM) hide the head behind NAME=val words
        while words and (words[0].lower() in _KILL_FLOW or re.match(r"[A-Za-z_][A-Za-z0-9_]*=", words[0])):
            words = words[1:]
        if not words:
            continue
        h = _kill_head(words[0])
        if h in _KILL_SHELLS:
            if depth < 3:
                out += _kill_inner(words, h, cwd, depth)
            continue
        if h in _KILL_WRAP:
            nxt = [w for w in words[1:] if _kill_head(w) in _KILL_HEADS]
            if not nxt:
                continue
            words = words[words.index(nxt[0]):]
            if h == "xargs":
                out.append(("name", None))  # fed from a pipe: selected by whatever listed it
                continue
        elif h not in _KILL_HEADS:
            continue
        got = _kill_targets(words, msys=not powershell)
        if got:
            out.append(got)
    return out


def _msys_to_win(pids):
    """{msys pid: (state, winpid)} for the git-bash (msys) PIDs of one command, state 'ok' | 'gone' | 'unknown'. In
    git-bash `kill N`, `$!` and the PID column of `ps` are msys PIDs, a different number space from the Windows PIDs
    _proc_cwd reads. One msys `ps` listing (PID and WINPID columns; found on PATH or beside git) answers every PID, so a
    command naming several costs one subprocess, not one each. 'gone' only when ps ran fine and does not list the PID
    (a plain msys kill then finds nothing); a ps that cannot run, times out or cannot be parsed makes all 'unknown'.
    Native Windows python cannot read msys /proc, hence the subprocess."""
    if os.name != "nt":
        return {p: ("ok", p) for p in pids}
    unknown = {p: ("unknown", None) for p in pids}
    import shutil
    ps = shutil.which("ps")
    git = shutil.which("git")
    if not ps and git:  # Git for Windows keeps it in <root>\usr\bin; git itself is in <root>\cmd or <root>\mingw64\bin
        root = os.path.dirname(os.path.dirname(git))
        ps = shutil.which("ps", path=os.pathsep.join(os.path.join(r, "usr", "bin") for r in (root, os.path.dirname(root))))
    if not ps:
        return unknown
    try:
        r = proc.run([ps], capture_output=True, text=True, timeout=3)
        lines = r.stdout.splitlines()
        cols = lines[0].split() if lines else []
        if "PID" not in cols or "WINPID" not in cols:
            return unknown
        table = {}
        for line in lines[1:]:
            f = line.split()
            if not f:
                continue
            if len(f[0]) == 1 and not f[0].isdigit():  # msys ps prints a status char (I, S, O...) before PID on some rows
                f = f[1:]
            if len(f) <= max(cols.index("PID"), cols.index("WINPID")):
                return unknown  # unparseable row: never silently skipped (a live PID would read 'gone')
            table[str(int(f[cols.index("PID")]))] = int(f[cols.index("WINPID")])
        return {p: (("ok", table[str(p)]) if str(p) in table else ("gone", None)) for p in pids}
    except (OSError, ValueError, subprocess.SubprocessError):
        return unknown


def _proc_cwd(pid):
    """(state, cwd): ('ok', path) | ('gone', None) | ('unknown', None). Stdlib only: /proc, or on Windows the PEB of the
    process read through ctypes (same-bitness targets; anything unreadable is 'unknown')."""
    if os.name != "nt":
        try:
            return "ok", os.readlink(f"/proc/{pid}/cwd")
        except FileNotFoundError:
            return "gone", None
        except OSError:
            return "unknown", None
    import ctypes
    from ctypes import wintypes
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        return "unknown", None
    k32, nt = ctypes.WinDLL("kernel32", use_last_error=True), ctypes.WinDLL("ntdll")
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                      ctypes.POINTER(ctypes.c_size_t)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    nt.NtQueryInformationProcess.argtypes = [wintypes.HANDLE, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
                                             ctypes.POINTER(ctypes.c_ulong)]
    h = k32.OpenProcess(0x0410, False, pid)  # QUERY_INFORMATION | VM_READ
    if not h:
        return ("gone", None) if ctypes.get_last_error() == 87 else ("unknown", None)
    try:
        pbi = (ctypes.c_void_p * 6)()
        n = ctypes.c_ulong()
        if nt.NtQueryInformationProcess(h, 0, ctypes.byref(pbi), ctypes.sizeof(pbi), ctypes.byref(n)) != 0:
            return "unknown", None

        def read(addr, size):
            buf = ctypes.create_string_buffer(size)
            got = ctypes.c_size_t()
            if not k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, size, ctypes.byref(got)) or got.value != size:
                raise OSError("read")
            return buf.raw
        params = int.from_bytes(read(pbi[1] + 0x20, 8), "little")  # PEB.ProcessParameters
        us = read(params + 0x38, 16)  # CurrentDirectory.DosPath (UNICODE_STRING)
        length, buf = int.from_bytes(us[:2], "little"), int.from_bytes(us[8:], "little")
        if not length or length > 4096:
            return "unknown", None
        return "ok", read(buf, length).decode("utf-16-le").rstrip("\\") or "unknown"
    except (OSError, ValueError):
        return "unknown", None
    finally:
        k32.CloseHandle(h)


def _refuse_kill(conn, row, d, resp):
    """#235: refuse a subagent's process kill that selects by name/image/pattern (pkill, killall, taskkill /IM /FI,
    Stop-Process -Name, Get-Process | Stop-Process, `kill $(pgrep ..)`, xargs kill, wmic .. where name=..), and a kill by
    PID whose process working directory is outside the agent's own worktree and scratch folders (or cannot be read).
    The messages name the alternative: kill the PID you started. Returns the deny, else `resp`."""
    cmd = (d.get("tool_input") or {}).get("command") or ""
    if not resp or "updatedInput" not in resp["hookSpecificOutput"] or not re.search(
            r"kill|stop-process|spps|terminate|wmic", cmd, re.I):
        return resp

    def deny(why):
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": "Aesir: " + why}}
    try:
        wt = row["worktree"]
        cwd = wt or d.get("cwd") or os.getcwd()
        found = _kill_findings(cmd, d.get("tool_name") == "PowerShell", _dir_at(cmd, len(cmd), cwd) or cwd)
        if not found:
            return resp
        if any(k == "name" for k, _ in found) or (any(k == "unknown" for k, _ in found) and re.search(
                r"\b(pgrep|pidof|tasklist|ps|grep|awk|get-process|gps|select-string|findstr)\b", cmd, re.I)):
            return deny("do not kill processes by name, image or command-line pattern (pkill, killall, taskkill /IM, "
                        "Stop-Process -Name, kill $(pgrep ..), xargs kill, wmic ... where name=..): it also stops other "
                        "agents', the lead's and VT's runs on this machine (#235). " + _KILL_ALT)
        if any(k == "unknown" for k, _ in found):
            return deny("cannot tell which process this kill targets (a variable, job or substitution instead of a "
                        "literal PID). Use a literal PID, or `$(cat run.pid)` of the file you wrote. " + _KILL_ALT)
        root = _proj(d, wt)
        main = _norm(root)
        try:
            mine = scratch.path(root, row["agent_id"])
        except Exception:
            mine = None
        def mine_dir(p):
            return ((wt and _norm(wt) != main and _inside_dir(p, wt)) or (mine and _inside_dir(p, mine))
                    or _scratchpad(p, None, d, root))

        def verdict(pid, winpid):
            """Deny text for a Windows PID not provably in the agent's own tree, else None (own, or already gone)."""
            state, pcwd = _proc_cwd(winpid)
            if state == "gone":
                return None  # nothing to kill
            if state != "ok":
                return (f"cannot determine the working directory of PID {pid} (access denied or unsupported), "
                        "so it cannot be shown to be yours (#235). " + _KILL_ALT)
            if mine_dir(pcwd):
                return None
            return (f"PID {pid} runs in {pcwd}, outside your worktree ({wt or 'none'}) and your scratch folders, "
                    "so it is not yours to kill (#235). " + _KILL_ALT)
        mpids = {p for k, ps in found if k in ("mpids", "mwpids") for p in ps}
        mapped = _msys_to_win(sorted(mpids)) if mpids else {}
        for kind, pids in found:
            for pid in pids:
                if kind in ("pids", "mwpids"):
                    why = verdict(pid, pid)
                    if why:
                        return deny(why)
                if kind in ("mpids", "mwpids"):
                    state, winpid = mapped[pid]  # git-bash kill/$!/ps PIDs are msys PIDs, not Windows PIDs
                    if state == "unknown":
                        return deny(f"cannot map git-bash PID {pid} to a Windows process (`ps -p {pid}` unavailable), "
                                    "so its directory cannot be checked (#235). " + _KILL_ALT)
                    why = verdict(pid, winpid) if state == "ok" else None
                    if why:
                        return deny(why)
    except Exception as e:  # fails open but loud (D115, #130)
        _degraded(conn, "kill check skipped", e)
    return resp


def _flag_stray_redirect(conn, row, d, resp):
    """#138/#164: a Bash/PowerShell command run by a subagent that redirects output (> >> 2> tee Out-File) into the MAIN
    checkout (.aesir included) leaves a stray file that dirties the tree and blocks every gate (#88). Refuse it, naming
    the agent's scratch folder, and record the friction. Returns the deny response, else `resp` unchanged.
    Judged for every subagent, reviewers (cwd = the main checkout, no worktree) included: an absolute target inside the
    main checkout, or a relative one when the agent's cwd is the main checkout (no worktree), is refused. Allowed: the agent's scratch folder,
    temp dirs, a worktree, /dev/null and NUL, fd duplications (2>&1). A relative target is not judged when the command
    changes directory (cd, pushd, Set-Location) since it may land elsewhere. aesir CLI commands that write .aesir
    themselves are not redirects. Main sessions never reach this."""
    wt = row["worktree"]
    cwd = wt or d.get("cwd")
    cmd = (d.get("tool_input") or {}).get("command") or ""
    if not cwd or not resp or "updatedInput" not in resp["hookSpecificOutput"]:  # refused already: skip
        return resp
    try:
        root = _proj(d, wt)
        main = _norm(root)
        try:
            mine = scratch.path(root, row["agent_id"])
        except Exception:
            mine = None
        def judge(m, literal_bs):
            """The path a redirect target writes inside the main checkout under one reading of the word, else None.
            Raises when the word cannot be decoded or judged (the caller fails closed)."""
            p, expands, prefix = _shell_word(cmd, m.end(), literal_bs)
            if not p or p.lower() in _NULLS:
                return None
            if expands:
                # value unknown (known gap: an expansion before any '/', as in $HOME/x or `pwd`/x, passes), but a
                # literal prefix up to a '/' (MAIN/`x`, MAIN/$HOME, ../../../$x) already fixes the directory
                if not re.search(r"[/\\]", prefix):
                    return None
                p = _native(prefix + "_" if prefix[-1:] in "/\\" else prefix)  # "_" stands for the file name
            else:
                p = _native(p)
            if not (os.path.isabs(p) or re.match(r"[A-Za-z]:|/", p)):
                # a relative target lands in the directory the command is in at that point: the cwd moved
                # by any cd before it (#168). A cd out of main exempts it; a cd into main, or ../ out of a
                # subdir, does not.
                base = _dir_at(cmd, m.start(), cwd)
                if base is None:
                    return None
                p = os.path.join(base, p)
            if (mine is not None and _inside_dir(p, mine)) or (
                    wt and _norm(wt) != main and _inside_dir(p, wt)):
                return None  # scratch, or the agent's own worktree (physically under main/.claude/worktrees)
            return p if _worktree_of(os.path.dirname(os.path.abspath(p)))[0] == main else None

        data_spans = _data_body_spans(cmd)
        for m in _REDIRECT_RE.finditer(cmd):
            if any(a <= m.start() < b for a, b in data_spans):
                continue  # '>' in the text a quoted heredoc feeds cat/tee (#268)
            hit = None
            try:  # the whole per-target judgement fails closed: any error denies, never skips (r2 must1)
                # two readings, denied if either lands in main: bash quote removal, and backslashes kept literal
                # (a Windows path C:\proj\f, or PowerShell Out-File, where '\' separates, r3 must1)
                for literal_bs in (False, True):
                    p = judge(m, literal_bs)
                    if p:
                        hit = True
                        break
            except Exception:  # undecodable or unjudgeable target: fail closed
                raw = (cmd[m.end():].split() or [""])[0][:80]
                return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                               "permissionDecisionReason": (
                    f"Aesir: cannot decode the redirect target {raw!r}, so it is refused as a possible write into "
                    "the main checkout. Write plain paths under your worktree or scratch folder.")}}
            if hit:
                with db.tx(conn):
                    friction.record(conn, "aesir-hook", "stray_write",
                                    f"{row['agent_id']} redirected output into the main checkout: {p} "
                                    "(scratch goes in the worktree or the scratch folder)",
                                    agent=row["agent_id"], task=row["task_id"], tool=d.get("tool_name") or "")
                where = f"your scratch folder {mine}" if mine else "your worktree or scratch folder"
                why = (f"Aesir: do not redirect or tee output into the main checkout ({p}): a stray file there dirties "
                       f"the tree and blocks every gate. Write it under {where} (or your worktree) instead.")
                return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                               "permissionDecisionReason": why}}
    except Exception as e:  # the check itself fails open but loud (D115, #130)
        _degraded(conn, "stray-write check skipped", e)
    return resp


EDIT_TOOLS = ("Edit", "Write", "NotebookEdit", "MultiEdit")


def _worktree_of(path):
    """(toplevel, common dir) of the checkout containing `path` (which may not exist yet), by walking up to the
    nearest '.git' entry. No subprocess. (None, None) if there is none or it is unreadable."""
    cur = Path(path)
    for cand in (cur, *cur.parents):
        g = cand / ".git"
        try:
            if g.is_dir():
                return _norm(cand), _norm(g)
            if g.is_file():
                m = re.match(r"gitdir:\s*(.+)", g.read_text(encoding="utf-8", errors="replace").strip())
                if not m:
                    return None, None
                gd = Path(m.group(1).strip())
                gd = gd if gd.is_absolute() else cand / gd
                cd = gd / "commondir"
                common = (gd / cd.read_text(encoding="utf-8").strip()) if cd.is_file() else gd.parent.parent
                return _norm(cand), _norm(common)
        except OSError:
            return None, None
    return None, None


def _scope_rel(fp, base, row, d):
    """Path of an edit target relative to the checkout it belongs to, for matching against the task scope.
    A target inside a linked worktree of this repository is matched relative to that worktree's root, wherever
    the worktree lives and whatever the agent's cwd (aesir-reports#2). A target in the main checkout, or outside
    any worktree of this repository, keeps the old rule (relative to the agent's cwd/worktree; '../' = denied)."""
    p = fp if os.path.isabs(fp) or not base else os.path.join(base, fp)
    top, common = _worktree_of(os.path.dirname(os.path.abspath(p)))
    main = _proj(d, row["worktree"])
    pcommon = _worktree_of(main)[1]
    if top and pcommon and common == pcommon and top != _norm(main):
        try:
            return os.path.relpath(_norm(p), top).replace("\\", "/")
        except ValueError:  # different drive
            return "../" + fp
    try:
        return os.path.relpath(_norm(p), _norm(base)).replace("\\", "/") if base else fp.replace("\\", "/")
    except ValueError:  # different drive
        return "../" + fp


def _claude_project_dir(root):
    """The directory name Claude Code gives a project under <temp>/claude/: the main checkout's absolute path with
    every character that is not a letter or digit replaced by '-' (E:\\Projects\\Aesir -> E--Projects-Aesir).
    '' when it cannot be derived (callers then fall back to the current-session rule only: fail closed, #173)."""
    try:
        return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(str(root)))
    except (TypeError, ValueError, OSError):
        return ""


def _scratchpad(fp, base, d, root=None):
    """True when `fp` is inside a Claude session scratchpad <OS temp dir>/claude/<project dir name>/<session id>/scratchpad.
    Allowed: (a) the hook's own session_id under any project dir (#34/#96; subagent events carry the parent session's
    id), or (b) any session id under THIS project's dir name (#173: a lead's scratchpad of record from an earlier
    session). A different project's dir with another session is refused. The path is checked after realpath, so a
    '..' climb or a symlink out is judged where it lands."""
    sid = d.get("session_id")
    sid = sid if isinstance(sid, str) and re.fullmatch(r"[A-Za-z0-9_-]+", sid) else None
    proj = _claude_project_dir(root) if root else ""
    if not sid and not proj:
        return False
    p = fp if os.path.isabs(fp) or not base else os.path.join(base, fp)
    rel = os.path.relpath(_norm(p), _norm(tempfile.gettempdir())).replace("\\", "/") if p else ""
    parts = rel.split("/")
    if not (len(parts) > 4 and parts[0] == "claude" and parts[3] == "scratchpad" and ".." not in parts):
        return False
    if sid and parts[2] == sid:
        return True
    return bool(proj) and os.path.normcase(parts[1]) == os.path.normcase(proj) and bool(re.fullmatch(r"[A-Za-z0-9_-]+", parts[2]))


def _own_scratch(fp, base, row, root):
    """#173: True when `fp` lies inside this agent's own Aesir scratch folder (scratch.path), on the real path."""
    try:
        mine = scratch.path(root, row["agent_id"])
    except Exception:
        return False
    p = fp if os.path.isabs(fp) or not base else os.path.join(base, fp)
    return _inside_dir(p, mine) and _norm(p) != _norm(mine)


RESEARCHER_DIRS = "docs/"  # #181: the only project path a researcher may write, whatever the task scope


def _scope_deny(conn, d, row):
    """Deny an implementer's or researcher's file edit outside its task scope. A researcher (#181) is further held to
    docs/ whatever the scope says (it has no shell; its job is to write notes), besides its scratch folders. Bash
    writes are not checked here (known gap): the in_review transition and the merge queue refuse any branch diff
    outside scope."""
    kind = agent_role(row)  # #248: the agent type, not the (free) id prefix
    researcher = kind == "researcher"
    if not (researcher or kind == "implementer") or not row["task_id"]:
        return None
    try:
        t = S.get_task(conn, row["task_id"])
        scope = tracks.effective_scope(t, _proj(d, row["worktree"])).strip()
    except Exception as e:  # fail open (D115) but loudly (#130)
        _degraded(conn, "scope guard disabled for this edit", e)
        return None
    ti = d.get("tool_input") or {}
    fp = ti.get("file_path") or ti.get("notebook_path")
    if (not scope and not researcher) or not isinstance(fp, str) or not fp:
        return None
    base = row["worktree"] or d.get("cwd") or ""
    root = _proj(d, row["worktree"])
    for allowed in (lambda: _scratchpad(fp, base, d, root), lambda: _own_scratch(fp, base, row, root)):
        try:
            if allowed():
                return None
        except ValueError:  # different drive: not under the temp dir
            pass
    rel = _scope_rel(fp, base, row, d)
    inside = not rel.startswith("../") and rel != ".."
    in_task = inside and S.in_scope(rel, scope)
    if in_task and (not researcher or S.in_scope(rel, RESEARCHER_DIRS)):
        return None
    try:
        mine = f" ({scratch.path(root, row['agent_id'])})"
    except Exception:
        mine = ""
    if researcher and in_task:
        why = (f"Aesir scope: a researcher writes only under docs/; '{fp}' is outside it. "
               f"Scratch files go in your Aesir scratch folder{mine}. Ask lead if the task needs more.")
    else:
        ents = "".join(f"\n  {e}" for e in S.scope_entries(scope))  # #209/reports#135: a separator slip shows here
        why = (f"Aesir scope: task #{t['id']:03d} scope entries (split on commas and spaces):{ents}\n'{fp}' is outside it. "
               f"Edit only files inside the scope; scratch files go in your Aesir scratch folder{mine}. "
               "Ask lead for scope change.")
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": why}}


def _session_name(d):
    """Session name: `session_title` in the hook input, else the live session file ~/.claude/sessions/<pid>.json
    (carries sessionId + name, updated by /rename)."""
    name = d.get("session_title")
    sid = d.get("session_id")
    if not name and sid:
        try:
            for f in metrics.default_sessions_dir().glob("*.json"):
                try:
                    j = json.loads(f.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if isinstance(j, dict) and j.get("sessionId") == sid:
                    name = j.get("name")
                    break
        except OSError:
            pass
    return name if isinstance(name, str) else ""


def session_role(conn, d):
    """AESIR_ROLE, else the session name; either is '<role>' or '<prefix>-<role>' where role is advisor, lead or a
    checked-in role and prefix is the project's session prefix (or, when the rest is a known role, any prefix).
    Else ''."""
    try:
        prefix = session_prefix(_proj(d))
    except Exception as e:
        _degraded(conn, "session prefix defaulted to 'aesir'", e)
        prefix = "aesir"
    for raw in (os.environ.get("AESIR_ROLE") or "", _session_name(d)):
        role = resolve_role(conn, raw, prefix)
        if role:
            return role
    return ""


def _ctx(ev, text):
    return {"hookSpecificOutput": {"hookEventName": ev, "additionalContext": text}}


def _is_aesir_repo(d, conn=None):
    """True in the Aesir source repo itself (package at the root), not in a client with the vendored copy."""
    try:
        root = _proj(d)
    except Exception as e:
        _degraded(conn, "project root unknown, treated as not the Aesir repo", e)
        return False
    return (root / "aesir" / "upstream_pickup.py").exists() and (root / "aesir.toml").exists()


def _record_session_name(conn, d, role):
    """Store the live session name on the role's checkin (#120) when it names this role; notify hints use it."""
    try:
        name = _session_name(d)
        try:
            prefix = session_prefix(_proj(d))
        except Exception as e:
            _degraded(conn, "session prefix defaulted to 'aesir'", e)
            prefix = "aesir"
        if name and resolve_role(conn, name, prefix) == role:
            S.set_session_name(conn, role, name)
    except Exception as e:  # best-effort; never blocks the session start
        print(f"aesir hook: session name: {e}", file=sys.stderr)


def _session_start(conn, d):
    role = session_role(conn, d)
    if not role:
        return _ctx("SessionStart", client_cmd("Aesir: run `python -m aesir --as <role> role checkin <role>` then "
                                               "`python -m aesir resume <role>` for your role (advisor or lead)."))
    from .commands.resume import _render_resume, resume_data
    if d.get("source") == "startup":  # resume/clear/compact: same role session, resume only
        S.checkin(conn, role, role, d.get("session_id") or "")
    _record_session_name(conn, d, role)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _render_resume(resume_data(conn, role, session=d.get("session_id") or None))
    text = f"Aesir resume for {role} (checked in):\n" + buf.getvalue()
    if role == "lead" and _is_aesir_repo(d, conn):
        text += "\nStart the upstream monitor: Monitor -> " + client_cmd("python -m aesir upstream watch") + "\n"
    return _ctx("SessionStart", text)


def _review_context(conn, row, t, root):
    """Reviewer-only lines (#084): adopted practices' questions and the branch audit command. Empty for a project that
    adopted nothing and has no audit toolkit; a broken config never blocks the spawn."""
    try:
        flags = gates.load_config(root).get("practices", {}) if (Path(root) / "aesir.toml").exists() else {}
    except Exception as e:  # no aesir.toml at all is normal (nothing adopted); an unreadable one is not
        _degraded(conn, "reviewer practices config unreadable, none adopted", e)
        flags = {}
    try:
        audit_on = bool(audit.load_config(root)["toolkits"])
    except Exception as e:
        _degraded(conn, "audit config unreadable, audit line omitted", e)
        audit_on = False
    keys = {r[0] for r in conn.execute("SELECT key FROM core WHERE role='lead'")}
    return install.review_lines(flags, keys, t["id"], row["worktree"], audit_on)


def _local_branches(root):
    """Local branch names read from the git dir (loose refs and packed-refs), no git subprocess. [] when unreadable."""
    g = Path(root) / ".git"
    names = set()
    try:
        heads = g / "refs" / "heads"
        names |= {p.relative_to(heads).as_posix() for p in heads.rglob("*") if p.is_file()}
        packed = g / "packed-refs"
        if packed.is_file():
            for ln in packed.read_text(encoding="utf-8", errors="replace").splitlines():
                if " refs/heads/" in ln and not ln.startswith(("#", "^")):
                    names.add(ln.split(" refs/heads/", 1)[1].strip())
    except OSError as e:
        _warn("re-spin hint omitted from spawn context", e)
    return sorted(names)


# Why this line exists (aesir-reports #78): without it implementers read the generic "pushes are outward actions" rule
# and left finished task branches unpushed. Only the task branch is allowed; main and tags stay lead-only.
PUSH_LINE = ("Push: you may `git push origin <your task branch>` (task/<id>-<slug>); that is part of the task, not an "
             "outward action. Pushing main or tags stays lead-only.")


def respin_lines(t, root):
    """#129: an implementer on a task whose branch already exists gets a fresh worktree that cannot check that branch
    out (it is held by the old one). Name the path: a new branch `<base>-r<n>` from the task branch head; the lead
    then runs `task set --branch`, and the merge queue ff-merges it (it contains the old commits). Empty when the
    task has no existing branch."""
    refs = _local_branches(root)
    cur = t["branch"] if t["branch"] in refs else ""
    mine = [r for r in refs if r.startswith(f"task/{t['id']}-")]
    if not cur and not mine:
        return []
    base = re.sub(r"-r\d+$", "", cur or sorted(mine, key=len)[0])
    # every local ref counts, not only task/<id>-*: a Workflow branch (worktree-wf_*-r1, #164) must get -r2, never -r1 again
    ns = [int(m.group(1)) for r in refs if (m := re.fullmatch(re.escape(base) + r"-r(\d+)", r))]
    head = cur or (f"{base}-r{max(ns)}" if ns else base)
    new = f"{base}-r{max(ns, default=0) + 1}"
    return [f"Re-spin: branch {head} already exists (checked out in an earlier worktree), so do NOT try to commit to it. "
            f"In your worktree run `git switch -c {new} {head}` and commit there; it keeps the earlier commits. "
            f"Report {new} as your branch. The lead then runs `" + client_cmd(f"python -m aesir --as lead task set {t['id']} "
            f"--branch {new}") + "`; the merge queue takes it as is."]


def subagent_context_text(conn, row, cwd=None):
    """The text SubagentStart injects for an agent row: id, task, scope, track, criteria, tips, reviewer questions,
    observe lines. One builder for the hook and for `spawn-context` (#125), which prints it for Workflow-spawned agents
    whose hook context Claude Code drops. A row with no agent_id (spawn-context found no bound row) omits the id line."""
    aid = row["agent_id"] or ""
    kind = agent_role(row) if aid else (row["agent_type"] or "")  # #248: the type, not the free id prefix
    lines = ([f"Aesir agent id: {aid}"] if aid else []) + [
             name_form_line(kind, row["task_id"]),
             "End your final report with: `friction: none|<one line>` and `deviation: none|<criterion> -- <cause>` "
             "(cause: spec, context, environment, model, judgement, drift)."]
    if aid:
        try:  # #138: the agent's own scratch folder; fail open
            sroot = db.project_root(row["worktree"] or cwd or None)
            running = {r[0] for r in conn.execute("SELECT agent_id FROM agents WHERE ended=''")}
            scratch.sweep(sroot, running, conn)
            lines.append(scratch.context_line(scratch.ensure(sroot, aid), scratch.keep_hours(sroot), row["task_id"],
                                              scratch.keep_bound_days(sroot)))
        except Exception as e:
            _degraded(conn, "scratch folder not created", e)
    tid = row["task_id"]
    if tid:
        try:
            t = S.get_task(conn, tid)
        except Exception as e:
            _degraded(conn, f"task #{tid} lookup failed, task context omitted", e)
            t = None
        if t:
            root = db.project_root(row["worktree"] or cwd or None)
            lines += [f"Task #{t['id']:03d}: {t['title']}",
                      f"Scope: {tracks.effective_scope(t, root) or '(none stated)'}"]
            try:  # #209: an entry matching nothing would refuse every edit; fail open
                lines += S.scope_warnings(root, tracks.effective_scope(t, root))
            except Exception as e:
                _degraded(conn, "scope match check skipped", e)
            name = tracks.task_track(t)
            if name:
                try:
                    lines += tracks.context_lines(tracks.get(root, name))
                except Exception:
                    lines.append(f"Track: {name} (not found in aesir.toml)")
            lines += ["Acceptance criteria:", t["spec"] or "(none)"]
            if kind == "implementer":
                lines += respin_lines(t, root)
                lines.append(PUSH_LINE)
            try:
                lines += tips.agent_lines(conn, {"agent_id": aid or kind, "agent_type": kind}, t, root, name)
            except Exception as e:
                _warn("tips omitted from spawn context", e)
            try:  # #157: prior decisions/tasks/docs on this subject
                lines += tips.related_lines(conn, t, root)
            except Exception as e:
                _warn("related matches omitted from spawn context", e)
            if kind == "reviewer":
                lines += _review_context(conn, row, t, root)
            if kind in ("implementer", "reviewer", "researcher"):  # id = <role>-<task>-<letter>
                try:
                    lines += observe.context_lines(root)
                except Exception as e:
                    _warn("observe lines omitted from spawn context", e)
    # #163: every command in the text (audit, tips, Related, re-spin...) uses this install's invocation, like `status`
    return client_cmd("\n".join(lines))


def name_form_line(role, task):
    """#248 (VT 0.17.0 check a): the prompt line that names the agent row and the form it must take, so a lead writing
    a spawn prompt does not guess ('implementer-332a' once left a -000- row)."""
    ex = f"{role or 'implementer'}-{int(task or 0) or 248}-a"
    return (f"Agent name form: a prompt line `Agent name: <role>-<task>-<letter>` (e.g. {ex}) names the agent row "
            "and binds the task. The prefix is free text (role comes from the agent type, not the name), but not a role "
            "or another type ('lead-..', 'reviewer-..' for an implementer); the dash before the letter may be left out.")


def spawn_header(task, agent_type):
    """The fixed first line of `spawn-context` output (#128). A lead that pastes the output into a spawn prompt
    carries it into the agent's prompt, which is how the hook and the agent defs tell the context was delivered."""
    return f"{HEADER_PREFIX} task {task} type {agent_type}"


def _flag_missing_header(conn, d, row):
    """#128: a freshly bound agent whose prompt has no spawn-context header started without its context (Claude Code
    drops the hook's additionalContext for Workflow agents, whose transcript is under subagents/workflows/; Agent-tool
    spawns are never flagged, #132): record a friction event, shown by `status`. The prompt is
    read from the subagent transcript (the hook input does not carry it); unreadable means unknown, so no event."""
    path = _subagent_transcript(d)
    if not path or "/subagents/workflows/" not in str(path).replace("\\", "/"):
        return  # #132: Agent-tool spawns keep their hook context; only Workflow agents lose it
    prompt = transcript_prompt(path) or ""
    if not prompt or HEADER_RE.search(prompt):
        return
    try:
        with db.tx(conn):
            friction.record(conn, "aesir-hook", "no_context",
                            f"agent started without context: {row['agent_id']} type {d.get('agent_type') or '-'} "
                            f"task {row['task_id'] or '-'}", agent=row["agent_id"], task=row["task_id"])
    except Exception as e:  # best-effort; never blocks the spawn
        print(f"aesir hook: no-context flag: {e}", file=sys.stderr)


def _subagent_context(conn, row, d=None):
    return _ctx("SubagentStart", subagent_context_text(conn, row, str(_proj(d)) if d else None))


def _msg_text(ti):
    m = ti.get("message")
    if isinstance(m, dict):
        m = m.get("text") or m.get("message") or json.dumps(m, sort_keys=True)
    return m if isinstance(m, str) else ""


def _capture_send(conn, d):
    """PreToolUse(SendMessage) in a role session: record the message in the db before it goes. Never blocks."""
    try:
        role = session_role(conn, d)
        ti = d.get("tool_input") or {}
        to = ti.get("to") or ti.get("recipient") or ""
        if role and isinstance(to, str):
            prefix = session_prefix(_proj(d))
            messages.capture_direct(conn, role, to, _msg_text(ti), prefix)
    except Exception as e:  # best-effort; the send must go through
        print(f"aesir hook: message capture: {e}", file=sys.stderr)


def _prompt_submit(conn, d):
    role = session_role(conn, d)
    ctx = None
    if role:  # backstop for a missed notify: unanswered asks addressed to this role
        try:
            asks = [m for m in messages.inbox(conn, role) if m["kind"] == "ask" and not m["acked"]]  # #190: acked ones stay quiet
        except Exception as e:
            _degraded(conn, "inbox unreadable, unanswered-ask reminder skipped", e)
            asks = []
        if asks:
            ctx = _ctx("UserPromptSubmit", f"Aesir: {len(asks)} unanswered ask(s) for {role} (" + ", ".join(
                f"M{m['id']} from {m['sender']}" for m in asks[:5]) + "). Run: " + client_cmd("python -m aesir inbox"))
    if role != "advisor":
        return ctx
    p = d.get("prompt") or ""
    pay = {"session": d.get("session_id") or "", "length": len(p)}  # the event ts is the timestamp
    try:
        if ownerwait.load_config(_proj(d))["record_text"]:  # opt-in only (#86)
            pay["text"] = p
    except Exception as e:  # a bad config must not lose the count
        _warn("owner-wait config unreadable, text not recorded", e)
    with db.tx(conn):
        db.event(conn, "owner", "session", d.get("session_id") or "", "owner-msg", pay)
    return ctx


def _capture_friction(conn, row, tp, last, d):
    try:
        cfg = friction.load_config(_proj(d))
        with db.tx(conn):
            friction.capture_stop(conn, "aesir-hook", row, _records(tp) if tp else [], last, cfg)
    except Exception as e:  # best-effort; never lose the stop itself
        print(f"aesir hook: friction capture: {e}", file=sys.stderr)


def _gist(body, n=80):
    """First line of a note body, cut to n chars: the one-line reminder for a repeat (#172)."""
    g = (body.strip().splitlines() or [""])[0]
    return g if len(g) <= n else g[:n - 3] + "..."


def _notes_text(notes):
    """Context text for unanswered notes: full on first delivery, one short line per repeat (#172)."""
    reply = client_cmd(f"python -m aesir reply M{notes[-1]['id']} \"<text>\"")
    lines = [f"Aesir note from {n['sender']} (M{n['id']}): {n['body']}" if n["new"] else
             f"Aesir note M{n['id']} from {n['sender']} still unanswered: {_gist(n['body'])}" for n in notes]
    return "\n".join(lines) + ("\nSteering only; your task spec is unchanged. Reply to every note (commit and push "
                              "are refused until you do): " + reply)


_GIT_ARG = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}  # options taking a separate value


_WRAP = {"env", "time", "xargs", "nohup", "command", "exec", "sudo", "nice", "then", "do", "else", "if", "!"}  # hand on to the next word
_SHELLS = {"bash", "sh", "zsh", "dash", "powershell", "pwsh"}
_WRAP_ARG = {"-u", "-n", "-I", "-P"}  # wrapper options taking a separate value (env -u X, xargs -I {})


def _prog(word):
    return re.sub(r"\.exe$", "", word.replace("\\", "/").rsplit("/", 1)[-1], flags=re.I).lower()


def _words_git_write(w, depth):
    """One segment's words: skip VAR=val and wrapper words (and their options), recurse into `sh -c "script"`,
    then look for git commit/push."""
    i, wrapped = 0, False
    while i < len(w):
        if re.match(r"[A-Za-z_]\w*=", w[i]) or _prog(w[i]) in _WRAP:
            wrapped = wrapped or _prog(w[i]) in _WRAP
            i += 1
        elif wrapped and w[i].startswith("-"):
            i += 2 if w[i] in _WRAP_ARG else 1
        else:
            break
    if i >= len(w):
        return False
    prog = _prog(w[i])
    if prog in _SHELLS:
        for j in range(i + 1, len(w) - 1):
            if w[j].lower() in ("-c", "-command", "-lc", "-ic", "-cl"):
                return _git_write(w[j + 1], depth + 1)
        return False
    if prog != "git":
        return False
    i += 1
    while i < len(w) and w[i].startswith("-"):
        i += 2 if w[i] in _GIT_ARG else 1
    return i < len(w) and w[i].lower() in ("commit", "push")


def _git_write(cmd, depth=0):
    """True when `cmd` has a git commit or git push segment (global options such as -C x / -c k=v skipped, leading
    VAR=val and env/time/xargs wrappers skipped, `bash -c "..."` scripts recursed into). Steering, not a security
    boundary. Unparseable text falls back to a whole-command regex: refusing too much beats letting a commit through."""
    parsed = _segments(re.sub(r"\\\r?\n", "", cmd))
    if parsed is None:
        return bool(re.search(r"\bgit\b[^\n;&|]*\b(commit|push)\b", cmd))
    return depth < 4 and any(_words_git_write(w, depth) for w in parsed[0])


def _note_block(conn, row, d):
    """#172: deny git commit / git push while the agent has an unanswered steering note. Fail-open on a db error."""
    cmd = (d.get("tool_input") or {}).get("command")
    if not row or not isinstance(cmd, str) or not _git_write(cmd):
        return None
    try:
        notes = messages.unanswered_notes(conn, row["agent_id"])
    except Exception as e:
        _degraded(conn, "unanswered-note commit check skipped", e)
        return None
    if not notes:
        return None
    ids = ", ".join(f"M{n['id']}" for n in notes)
    why = (f"Aesir: git commit and git push are refused while you have unanswered steering notes ({ids}). Reply to "
           "each, then retry.\n" + _notes_text([dict(n, new=True) for n in notes]))
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": why}}


def _with_notes(conn, row, resp):
    """#141/#172: add the agent's unanswered steering notes to a PreToolUse response as additionalContext, on every
    call until it replies. A denied call delivers nothing new (new notes stay unread for the next call; the commit
    deny carries its own text). Fail-open: a db error logs, adds friction, and the tool call goes on."""
    if not row or (resp and resp["hookSpecificOutput"].get("permissionDecision") == "deny"):
        return resp
    try:
        notes = messages.deliver_notes(conn, row["agent_id"])
    except Exception as e:
        _degraded(conn, "steering notes not delivered", e)
        return resp
    if not notes:
        return resp
    resp = resp or {"hookSpecificOutput": {"hookEventName": "PreToolUse"}}
    resp["hookSpecificOutput"]["additionalContext"] = _notes_text(notes)
    return resp


def handle(d, conn, db_file):
    ev, tool, harness = d.get("hook_event_name"), d.get("tool_name"), d.get("agent_id")
    if ev == "SessionStart":
        return _session_start(conn, d)
    if ev == "UserPromptSubmit":
        return _prompt_submit(conn, d)
    if ev == "PreToolUse":
        if tool in ("Agent", "Task"):
            return _spawn(conn, d)
        if tool == "SendMessage" and not harness:
            _capture_send(conn, d)
            return None
        if harness:
            row = ensure_bound(conn, d, reopen=True)
            resp = None
            if row and tool in ("Bash", "PowerShell"):
                resp = _refuse_head_move(conn, row, d, _flag_stray_redirect(
                    conn, row, d, _refuse_kill(conn, row, d, _note_block(conn, row, d) or _bash(d, row, db_file))))
            elif row and tool in EDIT_TOOLS:
                resp = _scope_deny(conn, d, row)
            return _with_notes(conn, row, resp)
    elif ev == "SubagentStart":
        fresh = bool(harness) and S.find_agent(conn, harness) is None
        row = ensure_bound(conn, d, wait=1.0, reopen=True)
        if row and fresh:
            _flag_missing_header(conn, d, row)
        if row:
            return _subagent_context(conn, row, d)
    elif ev == "SubagentStop":
        ensure_bound(conn, d)
        tp = d.get("agent_transcript_path") or str(_subagent_transcript(d) or "")
        tokens, model = transcript_usage(tp) if tp else ({}, "")
        row = S.stop_agent(conn, "aesir-hook", harness, tp, tokens, d.get("last_assistant_message", ""), model,
                           start=d.get("cwd"))
        if row:
            _capture_friction(conn, row, tp, d.get("last_assistant_message", ""), d)
    elif ev == "PostToolUseFailure" and not harness and not d.get("is_interrupt"):
        # session-level failure; subagent failures are read from the transcript at SubagentStop (no double count)
        err = str(d.get("error") or "")
        with db.tx(conn):
            friction.record(conn, "aesir-hook", friction.classify_error(err), f"{tool or '?'}: {err}")
    return None


def _env_root():
    """The session's project root from the hook process environment: Claude Code sets CLAUDE_PROJECT_DIR for every hook
    command (our own HOOK_CMD relies on it) to the directory the session started in, whatever cwd the tool call's shell
    has moved to. A linked worktree maps to its main checkout. None when unset or not a directory."""
    v = os.environ.get("CLAUDE_PROJECT_DIR")
    return db.project_root(v) if v and os.path.isdir(v) else None


def _own_root(d):
    """The project this hook belongs to (#279): CLAUDE_PROJECT_DIR first, else the payload cwd. The payload cwd follows
    the agent's shell, so a `cd` into a temp or foreign project must not change which db and project are judged."""
    return _env_root() or db.project_root(d.get("cwd") or None)


def _proj(d, wt=None):
    """Main-checkout root for a guard. `wt`: the agent's bound worktree (maps to its main checkout), else the own
    project root `run` resolved (kept in d['_aesir_root']), never the cwd the shell wandered to. Where the command RUNS
    still comes from the payload cwd; only "which project" comes from here."""
    if wt:
        return db.project_root(wt)
    return Path(d["_aesir_root"]) if d.get("_aesir_root") else _own_root(d)


def _cwd_worktree(d):
    """The payload cwd when it is inside a linked worktree of the own project (not the main checkout, not another
    project), else ''. Used to learn an agent's worktree: a cwd in a temp project is not its worktree."""
    cwd = d.get("cwd") or ""
    if not cwd:
        return ""
    top = db.project_root(cwd)
    own = _proj(d)
    return cwd if _norm(top) == _norm(own) and _norm(cwd) != _norm(own) else ""


def _no_db_deny(d, path):
    """A subagent whose own project db cannot be found must not run unguarded (#279 fail closed): deny its shell and
    edit tools with the cause and fix. None for anything else (persistent sessions may run outside a project)."""
    if not d.get("agent_id") or d.get("hook_event_name") != "PreToolUse":
        return None
    if d.get("tool_name") not in ("Bash", "PowerShell") + tuple(EDIT_TOOLS):
        return None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": f"Aesir: no database at {path}, so this subagent's guards cannot run and the call "
                                        "is refused. Cause: the hook found no Aesir project for this session "
                                        "(CLAUDE_PROJECT_DIR unset or its .aesir/aesir.db missing). Fix: start the session "
                                        "in the Aesir project, or pass --db / set AESIR_DB."}}


def run(text, db_arg=None):
    """Process one hook payload; returns the JSON-able response or None."""
    d = json.loads(text)
    if d.get("hook_event_name") == "PreToolUse" and d.get("tool_name") in EDIT_TOOLS:
        ti = d.get("tool_input") or {}
        fp = ti.get("file_path") or ti.get("notebook_path")
        if isinstance(fp, str) and install.edit_denied(fp):  # a client's pinned Aesir copy: change it via `upgrade`
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                    "permissionDecisionReason": "Aesir: .aesir-pkg is the pinned Aesir copy and is read-only. "
                                                "Edit your own project files instead; report the problem upstream "
                                                "(`report-upstream`), versions change with `upgrade`."}}
    root = _own_root(d)
    d["_aesir_root"] = str(root)
    path = db.db_path(db_arg, start=root)  # --db / AESIR_DB win inside db_path
    if not path.exists():
        return _no_db_deny(d, path)
    conn = db.connect(path, migrate_db=False, busy_ms=1500)  # fast; a db behind on schema is left alone
    try:
        if db._version(conn) > len(db.MIGRATIONS):  # #178: this code is older than the db; never silent
            return _skewed(d, conn, db._version(conn))
        if not db.current(conn):
            return None
        return handle(d, conn, str(path))
    finally:
        conn.close()


_CONTEXT_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "SubagentStart")  # events that take additionalContext


def _skew_edit_deny(d, why):
    """Fail closed (#178): with the schema unreadable the task scope is unknown, so a subagent's edit is allowed only
    inside its own checkout (the scope inside it cannot be checked) or a session scratchpad (#173 rule). Else a deny."""
    ti = d.get("tool_input") or {}
    fp = ti.get("file_path") or ti.get("notebook_path")
    cwd = d.get("cwd") or ""
    if not isinstance(fp, str) or not fp:
        return None
    try:
        root = db.project_root(cwd or None)
        top = _worktree_of(cwd)[0] if cwd else None
        p = fp if os.path.isabs(fp) or not cwd else os.path.join(cwd, fp)
        if top and _inside_dir(p, top) and _norm(top) != _norm(root):
            return None  # inside the agent's own linked worktree
        if _scratchpad(fp, cwd, d, root):
            return None
    except (ValueError, OSError):
        pass
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": f"{why} Edit refused: '{fp}' is outside this worktree."}}


def _skewed(d, conn, db_schema):
    """The hook code is older than the db (#178). Say so to the agent (stderr plus additionalContext; the first time
    per agent or session and always on session/subagent start), record it where `status` shows it, and refuse
    subagent edits outside the agent's own worktree. Legacy: nothing else runs, as before."""
    why = skew.message(len(db.MIGRATIONS), db_schema, db.code_root())
    new = skew.record(conn, d, db_schema)
    print(f"aesir hook: {why}", file=sys.stderr)
    ev, tool = d.get("hook_event_name"), d.get("tool_name")
    if ev == "PreToolUse" and tool in EDIT_TOOLS and d.get("agent_id"):
        deny = _skew_edit_deny(d, why)
        if deny:
            return deny
    if ev in _CONTEXT_EVENTS and (new or ev != "PreToolUse"):
        return _ctx(ev, why)
    return None


def main(db_arg=None):
    try:
        resp = run(sys.stdin.read(), db_arg)
        if resp:
            sys.stdout.write(json.dumps(resp))
    except Exception as e:  # a hook must never break the session
        print(f"aesir hook: {e}", file=sys.stderr)
    return 0
