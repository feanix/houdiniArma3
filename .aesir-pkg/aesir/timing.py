"""One-command cross-project gate timing pair (#140). Two projects' full gates are started together twice: run 1 with the
machine gate lock on, run 2 with AESIR_LEGACY_LOCK=0 (set in the child processes' environment only, so nothing persists).
Each project runs through its own launcher (`python .aesir-pkg/aesir_run.py` in a client, `python -m aesir` otherwise).
Optional background load (load.py, capped at load.MAX_SHARE) is held during both runs. Every child gate and load worker
is stopped on any exit (normal, error, Ctrl-C, SIGTERM). The result is returned as a dict and recorded as a DB event."""
import contextlib
import os
import re
import sys
import threading
import time
from pathlib import Path

from . import gates, load, proc
from .service import AesirError

PKG_PARENT = str(Path(__file__).resolve().parent.parent)
GATE_LINE = re.compile(r"^gate \S+ (PASS|FAIL) ([0-9.]+)s", re.M)
STRIP = ("AESIR_DB", "AESIR_AGENT", "AESIR_ROLE", "AESIR_LEGACY_LOCK")  # the child resolves its own project
RUNS = (("lock on", "1"), ("lock off", "0"))


def tree_root(path):
    """The git working tree containing `path`."""
    p = Path(path)
    if not p.is_dir():
        raise AesirError(f"no such directory: {path}")
    return Path(gates.git(p, "rev-parse", "--show-toplevel")).resolve()


def launcher(root):
    """(argv prefix, extra env) running aesir in `root`: the client's vendored launcher, else this package."""
    run = Path(root) / ".aesir-pkg" / "aesir_run.py"
    if run.is_file():
        return [sys.executable, str(Path(".aesir-pkg") / "aesir_run.py")], {}
    return [sys.executable, "-m", "aesir"], {"PYTHONPATH": PKG_PARENT + os.pathsep + os.environ.get("PYTHONPATH", "")}


def check_clean(roots):
    """Raise AesirError naming every tree with uncommitted changes."""
    bad = []
    for r in roots:
        dirty = gates.dirty_files(r)
        if dirty:
            bad.append(f"{r}: {len(dirty)} uncommitted change(s), e.g. {dirty[0].strip()}")
    if bad:
        raise AesirError("gate timing needs clean trees (a dirty tree fails its gate at once); commit or stash first:\n  "
                         + "\n  ".join(bad))


def kill_tree(p):
    """Stop a child gate and everything it started (idempotent)."""
    if p.poll() is not None:
        return
    if os.name == "nt":
        proc.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
    else:
        import signal
        with contextlib.suppress(OSError):
            os.killpg(p.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        p.kill()
    with contextlib.suppress(Exception):
        p.wait(timeout=10)


def _clock(t):
    return time.strftime("%H:%M:%S", time.localtime(t))


def parse_gate(text):
    """(passed, gate seconds) from the `gate <profile> PASS 12.3s <sha>` line, else (None, None)."""
    m = GATE_LINE.search(text or "")
    return (m.group(1) == "PASS", float(m.group(2))) if m else (None, None)


def run_once(roots, profile, legacy, env_extra=None):
    """Start `gate run <profile>` in every root at once; wait for all. Returns one dict per root. Children are killed
    (as trees) if this raises or is interrupted."""
    procs, rows = [], []

    def drain(p, box):
        box["out"] = p.communicate()[0]
        box["end"] = time.time()

    try:
        for r in roots:
            argv, extra = launcher(r)
            env = {k: v for k, v in os.environ.items() if k not in STRIP}
            env.update(extra)
            env.update(env_extra or {})
            env["AESIR_LEGACY_LOCK"] = legacy
            kw = {} if os.name == "nt" else {"start_new_session": True}
            p = proc.Popen(argv + ["gate", "run", profile], cwd=str(r), env=env, stdout=proc.subprocess.PIPE,
                           stderr=proc.subprocess.STDOUT, text=True, errors="replace", **kw)
            box = {"start": time.time()}
            t = threading.Thread(target=drain, args=(p, box), daemon=True)
            t.start()
            procs.append((r, p, t, box))
        for r, p, t, box in procs:
            while t.is_alive():
                t.join(0.25)  # short joins so Ctrl-C lands promptly
            text = box.get("out") or ""
            passed, gate = parse_gate(text)
            wall = box["end"] - box["start"]
            rows.append({"project": r.name, "root": str(r), "start": box["start"], "end": box["end"], "wall": round(wall, 1),
                         "gate": gate, "lock_wait": round(max(0.0, wall - gate), 1) if gate is not None else None,
                         "passed": bool(passed) and p.returncode == 0, "exit": p.returncode, "tail": text[-600:]})
    finally:
        for _, p, _, _ in procs:
            kill_tree(p)
    return rows


def run_pair(roots, profile="full", level=0.0, say=print, cores=None, measure=load.cpu_times, loadset=None, runner=run_once):
    """The whole timing pair. level: background load share (clamped to load.MAX_SHARE; 0 = none)."""
    level = load.clamp(level)
    ls = loadset or load.LoadSet()
    cores = cores or load.ncores()
    res = {"profile": profile, "projects": [str(r) for r in roots], "load": level, "cores": cores, "runs": []}
    with load.raise_on_signals():
        try:
            for name, legacy in RUNS:
                say(f"run {len(res['runs']) + 1} ({name}): starting {', '.join(r.name for r in roots)} together...")
                if level > 0:
                    ls.start(level, load.MAX_RUN_SECS, cores)
                    time.sleep(1.0)  # let the load settle before the gates start
                try:
                    a, g0, t0 = measure(), load.gen_cpu(ls), time.perf_counter()
                    rows = runner(roots, profile, legacy)
                    wall = time.perf_counter() - t0
                    cpu = load.busy_share(a, measure())
                    gen = max(0.0, load.gen_cpu(ls) - g0) / (wall * cores) if level > 0 else 0.0
                finally:
                    ls.stop()
                res["runs"].append({"name": name, "legacy_lock": legacy == "1", "cpu": cpu, "generator": gen, "rows": rows})
        finally:
            ls.stop()
    return res


def table(res):
    """The result as printable lines."""
    head = ["run", "project", "start", "end", "wall", "gate", "lock wait", "result", "load", "machine cpu", "generator cpu"]
    body = []
    for i, run in enumerate(res["runs"], 1):
        for row in run["rows"]:
            f = lambda v: "-" if v is None else f"{v:g}s"
            body.append([f"{i} {run['name']}", row["project"], _clock(row["start"]), _clock(row["end"]), f(row["wall"]),
                         f(row["gate"]), f(row["lock_wait"]), "PASS" if row["passed"] else "FAIL",
                         f"{res['load']:.0%}", f"{run['cpu']:.0%}", f"{run['generator']:.0%}"])
    widths = [max(len(r[c]) for r in [head] + body) for c in range(len(head))]
    line = lambda r: "  ".join(v.ljust(w) for v, w in zip(r, widths)).rstrip()
    out = [line(head)] + [line(r) for r in body]
    out.append(f"profile {res['profile']}, {res['cores']} logical cores, background load {res['load']:.0%} (cap {load.MAX_SHARE:.0%});"
               " lock wait = wall minus gate duration (includes process start-up)")
    return out
