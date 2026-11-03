"""gate run."""
import json
import os
import sys
from pathlib import Path

from .. import db, gates, load, longrun, timing
from ..db import event, tx
from .. import service as S
from ._common import out, add, grp, client_cmd, require_dir, warn_branch


def gate_run(c, a):
    cwd = require_dir(a.dir) if a.dir else Path.cwd()
    if a.task is None:  # credit the task whose branch head is this checkout's HEAD (reports#139); stderr keeps --json clean
        tid = S.task_for_head(c, cwd, db.project_root())
        if tid is not None:
            a.task = tid
            print(f"crediting task {tid}: its branch head equals the HEAD of {cwd}", file=sys.stderr)
    if a.task is not None:
        warn_branch(c, a.task, db.project_root())
    root = db.project_root() if a.dir else db.find_root(cwd)
    if longrun.resolve(root, cwd, a.profile):  # marked [long]: detached, never the merge-gate slot (#106)
        r = longrun.start(c, a.actor, root, cwd, a.profile, a.task, a.criterion, wait=a.wait)
        out(a, r, render_long)
        if r["status"] not in ("running", "passed"):
            raise SystemExit(1)
        return
    if a.criterion is not None or a.wait:
        raise S.AesirError("--criterion and --wait are for long-run profiles ([long] in aesir.toml)")
    try:
        r = gates.run_profile(c, a.actor, a.profile, cwd, a.task, os.environ.get("AESIR_AGENT"),
                              config_root=db.project_root() if a.dir else None, lock_wait=a.lock_wait)
    except gates.GateLockBusy as e:
        print(e, file=sys.stderr)
        raise SystemExit(gates.LOCK_BUSY_EXIT)

    def render(r):
        print(f"gate {r['profile']} {'PASS' if r['passed'] else 'FAIL'} {r['duration']}s {r['sha'][:8]}")
        if r["bootstrap"]:
            print(gates.BOOTSTRAP_MARK.format(r["profile"]))
        if len(r["steps"]) > 1 or any(x["result"] == "skipped" for x in r["steps"]):
            for x in r["steps"]:
                print(f"  step {x['result']}: {x['step']}" + (f" ({x['why']})" if x["why"] else ""))
        if not r["passed"]:
            print(r["tail"])
    out(a, r, render)
    if not r["passed"]:
        raise SystemExit(1)


def render_long(r):
    print(f"long run L{r['id']} {r['profile']} {r['status']} (timeout {r['timeout']:g}s, sha {r['sha'][:8]})")
    if r["status"] == "running":
        print("  follow: " + client_cmd(f"python -m aesir gate long {r['id']}"))
        if r["tail"]:
            print("  output so far:\n" + r["tail"].rstrip("\n"))
    elif r["status"] != "passed":
        print(r["tail"])


def gate_long(c, a):
    """gate long: running and recent long runs; gate long <id>: one run with its output tail."""
    if a.id is None:
        rows = longrun.listing(c)
        out(a, rows, lambda rs: [print(f"L{r['id']}  {r['profile']}  {r['status']}  task {r['task_id'] or '-'}  {r['started']}"
                                       f"  {r['sha'][:8]}") for r in rs] or print("no long runs"))
        return
    longrun.reap(c, a.actor)
    r = longrun.get(c, a.id)
    if r["status"] == "running":  # streamed log so far (#112)
        r["log"], r["tail"] = str(longrun.log_path(c, r["id"])), longrun.partial(c, r)
    out(a, r, lambda r: (render_long(r), print(f"log: {r['log']}") if r["log"] else None))


def _parse_levels(text):
    try:
        return [float(x) / 100 for x in text.split(",") if x.strip()]
    except ValueError:
        raise S.AesirError("--levels takes percentages like 25,50,75")


def gate_timing(c, a):
    """gate timing --with <path>: cross-project timing pair (#140). --load-test: staged CPU load only (#144).
    --last: show the last recorded pair."""
    if a.last:
        row = c.execute("SELECT ts, payload FROM events WHERE entity='gate' AND entity_id='timing' AND action='pair'"
                        " ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            raise S.AesirError("no gate timing pair recorded yet")
        res = json.loads(row["payload"])
        out(a, res, lambda r: (print(f"recorded {row['ts']}"), [print(x) for x in timing.table(r)]))
        return
    if a.load_test:
        levels = _parse_levels(a.levels) if a.levels else list(load.DEFAULT_LEVELS)
        if not levels or a.stage_secs <= 0:
            raise S.AesirError("--levels and --stage-secs need positive values")
        r = load.run_load_test(levels, a.stage_secs, say=(lambda m: None) if a.json else (lambda m: print(m, flush=True)))
        out(a, r, lambda r: print("stopped early at %d%%" % round(r["stopped"] * 100) if r["stopped"] else "load test complete"))
        return
    if not a.with_:
        raise S.AesirError("gate timing needs --with <other project path> (or --load-test, or --last)")
    level = a.load / 100
    if a.load < 0:
        raise S.AesirError("--load takes a percentage like 50")
    roots = [timing.tree_root(Path.cwd()), timing.tree_root(a.with_)]
    if roots[0] == roots[1]:
        raise S.AesirError("--with must name a different project than the current directory")
    timing.check_clean(roots)
    say = (lambda m: None) if a.json else (lambda m: print(m, flush=True))
    if level > load.MAX_SHARE:
        say(f"--load {a.load:g} is above the cap; using {load.MAX_SHARE:.0%}")
    res = timing.run_pair(roots, a.profile, level, say=say)
    with tx(c):
        event(c, a.actor, "gate", "timing", "pair", res)
    out(a, res, lambda r: [print(x) for x in timing.table(r)])
    say("recorded in the DB: python -m aesir gate timing --last")


def register(sub):
        gt = grp(sub, "gate")
        add(gt, "run", gate_run, ("profile", {}), ("--task", {"type": int}),
            ("--lock-wait", {"type": float, "help": "give up (exit 75, nothing recorded) if the gate lock is held this many seconds"}),
            ("--criterion", {"type": int, "help": "long run: record the result as evidence for this criterion of --task"}),
            ("--wait", {"action": "store_true", "help": "long run: stay in the foreground until it ends (default: detached); blocks the caller, so only under a Monitor (docs/supervision.md); otherwise follow with `wait --task N`"}),
            ("--dir", {"help": "run in this checkout (e.g. a worktree) instead of the cwd; config comes from the main checkout"}))
        add(gt, "long", gate_long, ("id", {"nargs": "?", "type": int}))
        add(gt, "timing", gate_timing,
            ("--with", {"dest": "with_", "help": "timing pair: path of the other project (this directory is the first)"}),
            ("--profile", {"default": "full", "help": "gate profile for the pair (default full)"}),
            ("--load", {"type": float, "default": 0.0, "help": "background CPU load during the pair, percent of cores (cap 75)"}),
            ("--last", {"action": "store_true", "help": "show the last recorded pair"}),
            ("--load-test", {"action": "store_true", "help": "raise CPU load in stages (capped at 75 percent) and probe responsiveness"}),
            ("--levels", {"help": "percent per stage, comma separated (default 25,50,75; each clamped to 75)"}),
            ("--stage-secs", {"type": float, "default": load.DEFAULT_STAGE_SECS, "help": "seconds per stage (default 30)"}))
