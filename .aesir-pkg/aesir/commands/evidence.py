"""evidence run/add/list: per-criterion evidence for in_review and handoff claims (task #55)."""
from pathlib import Path

from .. import db, evidence
from ._common import out, add, grp, hint, line, require_dir, warn_branch


def fmt(e):
    """One evidence entry as text lines."""
    head = f"E{e['id']} c{e['criterion']} {e['kind']} {e['sha'][:8] or '-'} {e['actor']} {e['ts']}"
    if e["kind"] == "claimed":
        return [head, f"    note: {e['note']}" + (f" (ref {e['ref']})" if e["ref"] else "")]
    ls = [head, f"    cmd: {e['cmd']}  exit: {e['exit_code']}"]
    if e.get("expect_fail"):
        ls[-1] += f"  must fail ({e['expect_fail']}): " + ("MET" if e["fail_met"] else "NOT MET")
    if e["expect"]:
        ls.append(f"    expect: {e['expect']}")
    ls.append("    excerpt: " + e["excerpt"].replace("\n", "\n             ")[:400])
    if e["output_ref"]:
        ls.append(f"    full output: {e['output_ref']}")
    return ls


def evidence_run(c, a):
    cmd = [x for i, x in enumerate(a.command) if not (i == 0 and x == "--")]
    if not cmd:
        raise ValueError("give the command after --, e.g. evidence run 5 --criterion 1 -- python -m unittest")
    text = cmd[0] if len(cmd) == 1 else cmd  # 2+ arguments run as given, no shell (#191)
    cwd = require_dir(a.dir) if a.dir else Path.cwd()
    warn_branch(c, a.task, db.project_root())
    # --dir: run and record in that checkout (e.g. a worktree); config, db and output files stay with the invoking checkout
    if a.expect_exit is not None and a.expect_exit <= 0:
        raise ValueError("--expect-exit must be a non-zero exit code")
    fail = "" if not (a.expect_fail or a.expect_exit is not None) else "any" if a.expect_exit is None else str(a.expect_exit)
    e = evidence.run(c, a.actor, a.task, a.criterion, text, cwd, a.expect or "", a.timeout,
                     root=db.project_root() if a.dir else None, expect_fail=fail)
    out(a, e, lambda e: print("\n".join(fmt(e))))
    if not evidence.holds(e):
        raise SystemExit(1)


def evidence_add(c, a):
    e = evidence.add(c, a.actor, a.task, a.criterion, a.note, a.ref or "", Path.cwd())
    out(a, e, lambda e: print(f"E{e['id']} recorded (claimed, not executed)"))


def evidence_list(c, a):
    rows = evidence.listing(c, a.task)
    out(a, rows, lambda rs: [print(ln) for e in rs for ln in fmt(e)] if rs else print("no evidence"))
    hint(a, f"python -m aesir task show {a.task}")


def register(sub):
    g = grp(sub, "evidence")
    add(g, "run", evidence_run, ("task", {"type": int}), ("--criterion", {"type": int, "required": True}),
        ("--expect", {"help": "text the output must contain (required for ui tasks: an assertion on rendered output)"}),
        ("--expect-fail", {"action": "store_true", "help": "mutation check: MET only when the command runs and exits non-zero (a missing tool, refusal or timeout is NOT MET); with --expect, that text must be in the output"}),
        ("--expect-exit", {"type": int, "help": "implies --expect-fail: the command must exit with exactly this code"}),
        ("--timeout", {"type": int, "default": evidence.TIMEOUT}),
        ("--dir", {"help": "run in this checkout (e.g. a worktree) and record its head sha; db and config come from the invoking checkout"}),
        ("command", {"nargs": "*", "help": "-- <command to execute>: 2+ arguments run directly with no shell (^ ( ) & < > | stay literal); ONE argument is a command string run through the shell (cmd.exe on Windows, /bin/sh elsewhere). To keep shell features (&&, |, >, ;) pass the whole command as ONE quoted string. On Windows bash, sh and .sh run through Git for Windows bash"}))
    add(g, "add", evidence_add, ("task", {"type": int}), ("--criterion", {"type": int, "required": True}),
        ("--note", {"required": True, "help": "what was observed, for evidence that cannot be executed"}),
        ("--ref", {"help": "pointer: file, url, message id"}))
    add(g, "list", evidence_list, ("task", {"type": int}))
