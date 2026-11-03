"""Escalation ladder (charter s7): count failures, suggest the next tier, build the failure dossier."""
from . import gates
from .db import event, now, tx
from .service import AesirError, TERMINAL, get_task, show_task

LADDER = ["sonnet", "opus", "fable"]
DEFAULT_FAILURES = 2
TAILS = 5  # failing gate runs quoted in a dossier


def failures_per_tier(root) -> int:
    try:
        cfg = gates.load_config(root)
    except AesirError as e:
        if "no aesir.toml" in str(e):
            return DEFAULT_FAILURES
        raise
    n = cfg.get("escalation", {}).get("failures_per_tier", DEFAULT_FAILURES)
    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        raise AesirError("bad aesir.toml: [escalation] failures_per_tier must be a positive integer")
    return n


def reject_approach(conn, actor, tid, reason):
    if not reason or not reason.strip():
        raise AesirError("reject-approach requires --reason")
    with tx(conn):
        get_task(conn, tid)
        conn.execute("INSERT INTO approach_rejections (task_id, ts, actor, reason) VALUES (?,?,?,?)",
                     (int(tid), now(), actor, reason))
        conn.execute("INSERT INTO task_log (task_id, ts, actor, text) VALUES (?,?,?,?)",
                     (int(tid), now(), actor, f"approach rejected: {reason}"))
        event(conn, actor, "task", tid, "reject_approach", {"reason": reason})


def _last_escalation(conn, tid):
    return conn.execute("SELECT * FROM escalations WHERE task_id=? ORDER BY id DESC LIMIT 1", (int(tid),)).fetchone()


def _tier_marks(conn, tid):
    """(tier, gate_runs id mark, rejection id mark): only newer rows count toward the current tier."""
    e = _last_escalation(conn, tid)
    if not e:
        return LADDER[0], 0, 0
    return e["to_tier"], e["gate_mark"], e["rej_mark"]


def red_runs(conn, tid, after_id=0):
    """Red gate runs of the task after a gate_runs id, one per commit (re-running the same
    commit is not a new attempt). A 'dirty tree' refusal is not a failed attempt."""
    rows = conn.execute(
        "SELECT * FROM gate_runs WHERE task_id=? AND passed=0 AND id>? AND tail NOT LIKE 'dirty tree%' ORDER BY id",
        (int(tid), after_id)).fetchall()
    seen, out = set(), []
    for r in rows:
        if r["sha"] not in seen:
            seen.add(r["sha"])
            out.append(dict(r))
    return out


def failures(conn, tid):
    """{tier, gate_reds, rejections, failures}: failures = red commits + rejections // 2, this tier only."""
    get_task(conn, tid)
    tier, g, j = _tier_marks(conn, tid)
    reds = len(red_runs(conn, tid, g))
    rej = conn.execute("SELECT COUNT(*) FROM approach_rejections WHERE task_id=? AND id>?", (int(tid), j)).fetchone()[0]
    return {"tier": tier, "gate_reds": reds, "rejections": rej, "failures": reds + rej // 2}


def escalate(conn, actor, tid, root, dry_run=False, klass=False, reason=""):
    """Suggest and record the next tier. Fable needs a recorded dossier. Below the failure
    threshold it refuses unless klass (the charter's class trigger, needs a reason)."""
    t = get_task(conn, tid)
    if t["status"] in TERMINAL:
        raise AesirError(f"task is {t['status']}; nothing to escalate")
    f = failures(conn, tid)
    need = failures_per_tier(root)
    i = LADDER.index(f["tier"])
    if i == len(LADDER) - 1:
        raise AesirError(f"already at {f['tier']}, the top tier; going further is an owner decision")
    nxt = LADDER[i + 1]
    if klass and not reason:
        raise AesirError("--class requires --reason")
    if f["failures"] < need and not klass:
        raise AesirError(f"{f['failures']}/{need} failures on {f['tier']}; not yet "
                         "(--class --reason for a clearly long-horizon problem)")
    if nxt == "fable" and not conn.execute("SELECT 1 FROM dossiers WHERE task_id=?", (int(tid),)).fetchone():
        raise AesirError(f"no dossier for task {tid}: run 'aesir dossier {tid}' and complete its gaps before fable")
    res = {"task": int(tid), "from": f["tier"], "to": nxt, "failures": f["failures"], "needed": need,
           "class": klass, "recorded": not dry_run}
    if dry_run:
        return res
    why = f" (class: {reason})" if klass else (f" ({reason})" if reason else "")
    with tx(conn):
        conn.execute("INSERT INTO escalations (task_id, ts, actor, from_tier, to_tier, failures, reason, gate_mark, rej_mark)"
                     " VALUES (?,?,?,?,?,?,?,"
                     "(SELECT COALESCE(MAX(id),0) FROM gate_runs WHERE task_id=?),"
                     "(SELECT COALESCE(MAX(id),0) FROM approach_rejections WHERE task_id=?))",
                     (int(tid), now(), actor, f["tier"], nxt, f["failures"], reason, int(tid), int(tid)))
        conn.execute("UPDATE tasks SET escalations=escalations+1, status='escalated', updated=? WHERE id=?",
                     (now(), int(tid)))
        conn.execute("INSERT INTO task_log (task_id, ts, actor, text) VALUES (?,?,?,?)",
                     (int(tid), now(), actor, f"escalate {f['tier']} -> {nxt}, {f['failures']}/{need} failures{why}"))
        event(conn, actor, "task", tid, "escalate", {"from": f["tier"], "to": nxt, "failures": f["failures"],
                                                      "class": klass, "reason": reason})
    return res


def _files_touched(root, branch):
    if not branch:
        return None
    try:
        return gates.git(root, "diff", "--name-only", f"main...{branch}").splitlines()
    except AesirError:
        return None


def _generate(conn, tid, root):
    t = show_task(conn, tid)
    f = failures(conn, tid)
    L = [f"# Dossier: task #{t['id']:03d} {t['title']}", "",
         f"status {t['status']}, tier {f['tier']}, attempts {t['attempts']}, escalations {t['escalations']}, "
         f"failures this tier {f['failures']} ({f['gate_reds']} red commits, {f['rejections']} approach rejections)", "",
         "## Spec", t["spec"].strip() or "(none)", "",
         "## Scope", t["scope"] or "(none)", "", "## Attempts and log"]
    L += [f"- {e['ts']} {e['actor']}: {e['text']}" for e in t["log"]] or ["(none)"]
    esc = conn.execute("SELECT * FROM escalations WHERE task_id=? ORDER BY id", (int(tid),)).fetchall()
    if esc:
        L += ["", "## Escalations"]
        L += [f"- {e['ts']} {e['from_tier']} -> {e['to_tier']} after {e['failures']} failures {e['reason']}".rstrip()
              for e in esc]
    rej = conn.execute("SELECT * FROM approach_rejections WHERE task_id=? ORDER BY id", (int(tid),)).fetchall()
    if rej:
        L += ["", "## Approaches rejected"]
        L += [f"- {r['ts']} {r['actor']}: {r['reason']}" for r in rej]
    reds = conn.execute("SELECT * FROM gate_runs WHERE task_id=? AND passed=0 ORDER BY id", (int(tid),)).fetchall()
    L += ["", f"## Failing gates (last {TAILS}, verbatim)"]
    for r in reds[-TAILS:]:
        L += [f"### gate {r['profile']} {r['sha'][:8]} {r['ts']}", "```", r["tail"].rstrip(), "```"]
    if not reds:
        L += ["(none)"]
    files = _files_touched(root, t["branch"])
    L += ["", f"## Files touched (git diff main...{t['branch'] or '?'})"]
    L += ["(unavailable)"] if files is None else [f"- {x}" for x in files] or ["(none)"]
    decs = conn.execute(
        "SELECT d.id, d.title, d.choice FROM links l JOIN decisions d ON d.id=CAST(l.a_id AS INTEGER)"
        " WHERE l.a_type='decision' AND l.b_type='task' AND l.b_id=? ORDER BY d.id", (str(int(tid)),)).fetchall()
    L += ["", "## Linked decisions"]
    L += [f"- D{d['id']} {d['title']}: {d['choice']}" for d in decs] or ["(none)"]
    L += ["", "## Gaps for lead (fill with: aesir dossier <task> --append TEXT)",
          "- minimal repro (command + seed)", "- what each attempt ruled out", "- relevant contracts",
          "- what solved means (done-definition)"]
    return "\n".join(L)


def dossier(conn, actor, tid, root, append=""):
    """Regenerate the db-derived body, keep lead's appended notes, store, return the full markdown."""
    get_task(conn, tid)
    body = _generate(conn, tid, root)
    with tx(conn):
        old = conn.execute("SELECT notes FROM dossiers WHERE task_id=?", (int(tid),)).fetchone()
        notes = old["notes"] if old else ""
        if append:
            notes = (notes + "\n" if notes else "") + f"- {now()} {actor}: {append.strip()}"
        ts = now()
        conn.execute(
            "INSERT INTO dossiers (task_id, body, notes, created, updated, actor) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(task_id) DO UPDATE SET body=excluded.body, notes=excluded.notes,"
            " updated=excluded.updated, actor=excluded.actor", (int(tid), body, notes, ts, ts, actor))
        event(conn, actor, "task", tid, "dossier", {"appended": bool(append)})
    return body + ("\n\n## Lead notes\n" + notes if notes else "")
