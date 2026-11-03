"""agent spawn/list/end/show; spawn-context (#125): the SubagentStart context on demand, for Workflow-spawned agents."""
import os

from .. import agenthook, db, gates, scratch, service as S
from ..rolenames import agent_role
from ._common import out, out_list, hint, line, add, grp, paging, client_cmd, steer_line


def _preamble(g, db_file):
    i = g["id"]
    return (f"You are {i} ({g['model']}), working task #{g['task']:03d}. Before any aesir or git command set env: "
            f"bash: export AESIR_AGENT={i} AESIR_MODEL={g['model']} AESIR_DB='{db_file}' ; "
            f"PowerShell: $env:AESIR_AGENT='{i}'; $env:AESIR_MODEL='{g['model']}'; $env:AESIR_DB='{db_file}'. "
            "Aesir then records you as the actor and git commits get an Aesir-Agent trailer. "
            + client_cmd(f"Spec: python -m aesir task show {g['task']}."))


def agent_spawn(c, a):
    g = S.spawn_agent(c, a.actor, a.role, a.task, a.parent, a.model, a.session)
    db_file = str(db.db_path(a.db).resolve())
    g["env"] = {"AESIR_AGENT": g["id"], "AESIR_MODEL": g["model"], "AESIR_DB": db_file}
    g["preamble"] = _preamble(g, db_file)

    def render(d):
        print(f"id: {d['id']}\nmodel: {d['model']}")
        print("env: " + " ".join(f"{k}={v}" for k, v in d["env"].items()))
        print("preamble: " + d["preamble"])
    out(a, g, render)


def agent_list(c, a):
    def render(rows):
        for g in rows:
            tok = g["tokens_in"] + g["tokens_out"] + g["tokens_cache_read"] + g["tokens_cache_create"]
            line(f"{g['agent_id']:<22}", f"task {('#%03d' % g['task_id']) if g['task_id'] else '-':<5}",
                 f"{g['model'] or '-':<8}", "running" if not g["ended"] else f"ended {g['ended']}",
                 f"tokens {tok}")
    rows = S.list_agents(c, a.task, a.role, a.running)
    out_list(a, rows, render, "python -m aesir agent list" + (f" --task {a.task}" if a.task is not None else "")
             + (f" --role {a.role}" if a.role else "") + (" --running" if a.running else ""),
             f"python -m aesir agent show {rows[-1]['agent_id']}" if rows else None, "python -m aesir agent end --stale")


def agent_end(c, a):
    if a.stale:
        ids = S.end_stale_agents(c, a.actor, a.older_than, a.result or "stale: ended by sweep")
        out(a, {"ended": ids}, lambda r: print("\n".join([f"ended {len(r['ended'])} stale agent(s)"]
                                                         + [f"  {x}" for x in r["ended"]])))
        return
    if not a.id:
        raise S.AesirError("give an agent id, or --stale")
    rows = [g for g in S.list_agents(c, running=True) if g["agent_id"] == a.id]
    if not rows:
        raise S.AesirError(f"no running agent {a.id}")
    S.agent_end(c, a.actor, rows[0]["role"], a.id, a.result)
    out(a, {"ended": [a.id]}, lambda r: print(f"agent {a.id} ended"))


def agent_show(c, a):
    def render(g):
        for k, v in g.items():
            if k not in ("events", "tokens") and v not in ("", None):
                print(f"{k}: {v}")
        print("tokens: " + " ".join(f"{k}={v}" for k, v in g["tokens"].items()))
        print("events:")
        for e in g["events"]:
            print(f"- {e['ts']} {e['actor']} {e['entity']}/{e['entity_id']} {e['action']} {e['payload']}")
    out(a, S.show_agent(c, a.id), render)
    hint(a, f"python -m aesir agent end {a.id}", "python -m aesir agent list --running")


def agent_bind(c, a):
    """#207: the lead binds a running agent row to a task by hand (a Workflow agent whose prompt named none)."""
    S.bind_task(c, a.actor, a.id, a.task, "manual")
    out(a, {"agent": a.id, "task": a.task}, lambda r: print(f"agent {r['agent']} bound to task #{r['task']:03d}"))
    hint(a, f"python -m aesir task show {a.task}", f"python -m aesir agent show {a.id}")


def _prebind_scratch_line(c, a):
    """#275 c7: the scratch line for an agent with no bound row yet, so it knows its path and lifetime before its first
    write. The folder is keyed by the agent id, which is --name when the spawn would take it (same test as the row
    allocation: well-formed, this task's number, not taken, #248); else the id is not known and the path is the form."""
    root = db.project_root(None)
    hours = scratch.keep_hours(root)
    nm = S.name_allowed(c, a.name, a.agent_type, a.agent_type) if a.name else None
    if nm and int(nm.group(2)) == int(a.task) and not c.execute(
            "SELECT 1 FROM agents WHERE agent_id=?", (a.name,)).fetchone():
        return scratch.context_line(scratch.ensure(root, a.name), hours, a.task, scratch.keep_bound_days(root))
    line = scratch.context_line(scratch.base(root) / "<your Agent name>", hours, a.task, scratch.keep_bound_days(root))
    return line + (f" (--name {a.name!r} would not be used as an agent id: taken, wrong task or reserved prefix)"
                   if a.name else " Pass --name <agent name> to get the exact path.")


def _gate_state_line(c, task_id):
    """#224: the reviewer's gate state at the task branch head, so it need not look it up: 'quick gate PASS at <sha>
    (gate run <id>)' or 'no gate at head <sha>'. The head is resolved like in_review does (origin when local lags,
    #204). '' for a task with no branch or an unresolvable head (the context is only a convenience)."""
    t = S.get_task(c, task_id)
    if not t["branch"]:
        return ""
    try:
        sha = S.resolve_branch_head(db.project_root(None), t["branch"])[1]
    except (S.AesirError, OSError):
        return ""
    hit = gates.passed_at_head(c, task_id, sha, "quick")
    if hit is None:
        return f"Gate state: no gate at head {sha[:8]}"
    return f"Gate state: quick gate PASS at {sha[:8]} (gate run {hit['id']}, {hit['profile']})"


def spawn_context(c, a):
    """Print what SubagentStart would inject for <agent-type> on --task. Claude Code drops that hook's additionalContext
    for Workflow-spawned agents, which cannot know their own id, so the bound running row (most recent) is looked up."""
    rows = [r for r in c.execute("SELECT * FROM agents WHERE ended='' AND task_id=? ORDER BY id DESC", (a.task,))
            if agent_role(r) == a.agent_type]  # #248: the type, the id prefix only for rows with no known type
    if a.name:  # c7: --name names one agent; never answer with another live agent's id and folder
        row = next((r for r in rows if r["bound"] and r["agent_id"] == a.name), None)
    else:
        row = next((r for r in rows if r["bound"]), None)  # a bound row is a live spawn; unbound ones may be stale
    if row is None:
        row = {"agent_id": "", "task_id": a.task, "worktree": "", "agent_type": a.agent_type}
    text = agenthook.subagent_context_text(c, row)
    if not row["agent_id"]:
        text = (f"(no bound running {a.agent_type} agent for task #{a.task:03d}; context below has no agent id, "
                "so your commands are not attributed to an agent row)\n" + text)
    if not row["agent_id"]:
        text += "\n" + _prebind_scratch_line(c, a)
    if a.agent_type == "reviewer":
        gate_line = _gate_state_line(c, a.task)
        text += ("\n" + gate_line) if gate_line else ""
    text = agenthook.spawn_header(a.task, a.agent_type) + "\n" + text  # fixed first line, even with no agent row (#128)
    text += "\n" + steer_line()  # #172
    out(a, {"agent_id": row["agent_id"], "context": text}, lambda d: print(d["context"]))


def whoami(c, a):
    """The bound agent row for the calling agent (AESIR_AGENT, which the hook exports into a bound agent's shell).
    Lets an agent whose prompt carries the spawn-context header learn its own id (#128)."""
    aid = os.environ.get("AESIR_AGENT")
    if not aid:
        raise S.AesirError("AESIR_AGENT is not set: this shell is not a bound Aesir agent")
    r = c.execute("SELECT * FROM agents WHERE agent_id=? ORDER BY id DESC LIMIT 1", (aid,)).fetchone()
    if r is None or not r["bound"]:
        raise S.AesirError(f"no bound agent row for {aid}")
    d = {"agent_id": r["agent_id"], "task": r["task_id"], "agent_type": r["agent_type"], "model": r["model"],
         "worktree": r["worktree"], "running": not r["ended"], "scratch": ""}
    if not r["ended"]:  # #138: Workflow agents get pasted pre-spawn context with no scratch line; whoami supplies it
        try:
            d["scratch"] = str(scratch.ensure(db.project_root(r["worktree"] or None), r["agent_id"]))
        except (OSError, S.AesirError) as e:  # fail open (D115): the id is still printed
            agenthook._degraded(c, "scratch folder not created", e)

    def render(g):
        print(f"Aesir agent id: {g['agent_id']}")
        if g["scratch"]:
            print(scratch.context_line(g["scratch"], scratch.keep_hours(db.project_root(g["worktree"] or None)),
                                       g["task"], scratch.keep_bound_days(db.project_root(g["worktree"] or None))))
        print(f"task: {g['task'] or '-'}  type: {g['agent_type'] or '-'}  running: {'yes' if g['running'] else 'no'}")
    out(a, d, render)


def register(sub):
        add(sub, "whoami", whoami)
        add(sub, "spawn-context", spawn_context, ("agent_type", {"help": "implementer, reviewer, runner or researcher"}),
            ("--task", {"type": int, "required": True}),
            ("--name", {"default": "", "help": "the Agent name the spawn prompt gives: names the exact scratch folder"}))
        g = grp(sub, "agent")
        add(g, "spawn", agent_spawn, ("role", {"help": "implementer, reviewer, runner, researcher, ..."}),
            ("--task", {"type": int, "required": True}), ("--model", {"help": "default by role"}),
            ("--parent", {"default": "lead", "help": "role that owns the agent record (default lead)"}),
            ("--session", {"default": ""}))
        paging(add(g, "list", agent_list, ("--task", {"type": int}), ("--role", {}),
                   ("--running", {"action": "store_true", "help": "only agents not yet ended"})))
        add(g, "end", agent_end, ("id", {"nargs": "?", "help": "agent id to end (omit with --stale)"}),
            ("--stale", {"action": "store_true", "help": "end every running agent older than --older-than"}),
            ("--older-than", {"default": "2h", "help": "age for --stale, e.g. 90m, 2h, 1d (default 2h)"}),
            ("--result", {"default": "", "help": "outcome text"}))
        add(g, "bind", agent_bind, ("id", {"help": "running agent id"}), ("--task", {"type": int, "required": True}))
        add(g, "show", agent_show, ("id", {"help": "agent id, e.g. implementer-022-a"}))
