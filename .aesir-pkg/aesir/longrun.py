"""Long-run lane (task #106): soak profiles that run detached and never take the merge-gate slot or the legacy lock.
A profile is an ordinary [gates] profile; `[long] <profile> = <timeout seconds>` in aesir.toml marks it long (true =
DEFAULT_TIMEOUT). `gate run <profile>` then inserts a long_runs row and starts a detached supervisor
(`python -m aesir.longrun <db> <id>`), which:
  - holds the per-project+profile long-run lock (an OS lock on longrun-<hash>.lock in the gate dir, so a killed
    supervisor drops it with its handle: nothing stale),
  - leases workers from the machine budget in the long lane (min 1, never waits, cap B/2; budget.py),
  - runs the steps under one overall timeout (steps run through gates.run_step: whole tree killed on expiry; on
    Windows each step is in a kill-on-close job, so killing the supervisor kills the steps),
  - writes the log beside the db, sets the final status, and, when started with --task/--criterion, records executed
    evidence for that criterion at the run's sha.
A row still 'running' whose supervisor is gone is marked failed by reap()."""
import calendar
import contextlib
import hashlib
import json
import os
import sys
import time
from pathlib import Path

from . import budget, db, gates, proc
from .db import event, now, tx
from .service import AesirError, get_task

DEFAULT_TIMEOUT = 1800  # seconds, for `[long] soak = true`
TAIL = 3000
EXCERPT = 600
START_GRACE = 60  # seconds a row may have no supervisor pid yet before it counts as dead
ACTIVE = "running"
DONE = ("passed", "failed", "timed_out")


BREAKAWAY_WARNING = ("could not leave the calling tool's job object; this long run may die with the calling tool "
                     "(start it from a shell outside the tool's job, e.g. a Monitor or an OS scheduler)")


def long_timeout(cfg, profile):
    """Seconds allowed for `profile` if [long] marks it long, else None."""
    v = cfg.get("long", {}).get(profile)
    if v is None or v is False:
        return None
    if v is True:
        return float(DEFAULT_TIMEOUT)
    if isinstance(v, (int, float)) and v > 0:
        return float(v)
    raise AesirError(f"bad aesir.toml: [long] {profile} must be true or a timeout in seconds > 0")


def resolve(root, cwd, profile):
    """(timeout, steps) if `profile` is a long profile, else None. Same lookup as gates.resolve_steps: main's config
    wins, the branch's only supplies a profile main lacks (bootstrap)."""
    cfg = gates.load_config(root)
    t = long_timeout(cfg, profile)
    if t is None and profile not in cfg.get("gates", {}) and Path(cwd).resolve() != Path(root).resolve():
        with contextlib.suppress(AesirError):
            t = long_timeout(gates.load_config(cwd), profile)
    if t is None:
        return None
    steps, _ = gates.resolve_steps(root, cwd, profile)
    return t, steps


def lock_path(root, profile):
    """Per project+profile long-run lock in the gate dir (not the merge slot, not the legacy lock)."""
    key = os.path.normcase(str(Path(root).resolve())) + "\0" + profile
    return budget.gate_dir() / f"longrun-{hashlib.sha1(key.encode('utf-8')).hexdigest()[:12]}.lock"


def _db_file(conn):
    for _, name, file in conn.execute("PRAGMA database_list"):
        if name == "main" and file:
            return file
    raise AesirError("a long run needs a database file (not an in-memory db)")


def _alive(row):
    if row["pid"] is None:
        return time.time() - _ts(row["started"]) < START_GRACE
    return str(budget.process_start(row["pid"])) == row["pid_start"]


def _ts(s):
    return calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ"))


def reap(conn, actor="system"):
    """Mark 'running' rows whose supervisor is gone as failed (it was killed; its lock is already free)."""
    n = 0
    for r in conn.execute("SELECT * FROM long_runs WHERE status=?", (ACTIVE,)).fetchall():
        if not _alive(r):
            with tx(conn):
                cur = conn.execute("UPDATE long_runs SET status='failed', ended=?, exit_code=-1, tail=? WHERE id=? AND status=?",
                                   (now(), "supervisor died before finishing (killed?); no result", r["id"], ACTIVE))
                if cur.rowcount:
                    event(conn, actor, "long_run", r["id"], "reap", {"profile": r["profile"]})
                    n += 1
    return n


def start(conn, actor, root, cwd, profile, task=None, criterion=None, wait=False, say=print):
    """Start a long run of `profile` (see resolve). Detached unless wait=True (then it runs here, to the end).
    Returns the long_runs row as a dict."""
    cwd = Path(cwd).resolve()
    timeout, steps = resolve(root, cwd, profile)
    reap(conn, actor)
    if task is not None:
        t = get_task(conn, task)
        if criterion is not None:
            from . import evidence
            evidence._check_criterion(t, int(criterion))
    elif criterion is not None:
        raise AesirError("--criterion needs --task")
    dirty = gates.dirty_files(cwd)
    if dirty:
        raise AesirError("dirty tree: commit or stash first\n" + "\n".join(gates.dirty_detail(conn, cwd, dirty)))
    lock = lock_path(root, profile)
    probe = gates._try_oslock(lock, lambda: f"{os.getpid()} probe @{int(time.time())}")[0]
    if probe is None:
        raise AesirError(f"a long run of '{profile}' is already running for this project")
    gates._unlock(probe)
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO long_runs (project, root, profile, task_id, criterion, sha, branch, cwd, steps, timeout, status,"
            " actor, started) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (gates.project_name(root), str(Path(root).resolve()), profile, task, criterion,
             gates.git(cwd, "rev-parse", "HEAD"), gates.git(cwd, "rev-parse", "--abbrev-ref", "HEAD"), str(cwd),
             json.dumps(steps), timeout, ACTIVE, actor, now()))
        rid = cur.lastrowid
        event(conn, actor, "long_run", rid, "start", {"profile": profile, "task": task, "timeout": timeout})
    dbf = _db_file(conn)
    if wait:
        _claim_pid(conn, rid, os.getpid())
        supervise(dbf, rid, say=say)
    else:
        if _spawn(conn, dbf, rid):
            say(f"warning: L{rid}: {BREAKAWAY_WARNING}")
            event(conn, actor, "long_run", rid, "breakaway_refused", {})
    return get(conn, rid)


def _claim_pid(conn, rid, pid):
    conn.execute("UPDATE long_runs SET pid=?, pid_start=? WHERE id=?", (pid, str(budget.process_start(pid)), rid))


def _spawn(conn, dbf, rid):
    """Start the supervisor detached from this process, console and (where allowed) job."""
    env = dict(os.environ)
    pkg = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = pkg + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    logdir = Path(dbf).parent / "longruns"
    logdir.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "aesir.longrun", dbf, str(rid)]
    p, breakaway_refused = proc.spawn_detached(cmd, env, logdir / f"L{rid}.supervisor.log", BREAKAWAY_WARNING)
    _claim_pid(conn, rid, p.pid)
    return breakaway_refused


def get(conn, rid):
    r = conn.execute("SELECT * FROM long_runs WHERE id=?", (int(rid),)).fetchone()
    if not r:
        raise AesirError(f"no such long run: {rid}")
    return dict(r)


def listing(conn, limit=10):
    """Running runs first, then the latest finished ones. Dead supervisors are reaped first."""
    reap(conn)
    run = [dict(r) for r in conn.execute("SELECT * FROM long_runs WHERE status=? ORDER BY id", (ACTIVE,))]
    done = [dict(r) for r in conn.execute("SELECT * FROM long_runs WHERE status!=? ORDER BY id DESC LIMIT ?", (ACTIVE, limit))]
    return run + done


def log_path(conn, rid):
    return Path(_db_file(conn)).parent / "longruns" / f"L{rid}.log"


def partial(conn, r, lines=40):
    """The last `lines` lines of a running run's streamed log ('' if nothing yet)."""
    try:
        with open(log_path(conn, r["id"]), encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-lines:])
    except OSError:
        return ""


def last_line(conn, r):
    """Last non-blank line streamed so far by a running run, else ''. Reads the log's tail only."""
    try:
        with open(log_path(conn, r["id"]), "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 4096))
            rows = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return ""
    return next((x.strip() for x in reversed(rows) if x.strip()), "")


def _finish(conn, r, status, code, out, dur):
    log = log_path(conn, r["id"])
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(f"$ gate run {r['profile']} (long run L{r['id']}, {status}, {dur}s)\n{out}\n[exit {code}]\n", encoding="utf-8")
    tail = out[-TAIL:]
    with tx(conn):
        eid = None
        if r["task_id"] is not None and r["criterion"] is not None:
            cur = conn.execute(
                "INSERT INTO evidence (task_id, criterion, kind, cmd, exit_code, excerpt, expect, sha, branch, actor, ts,"
                " output_ref) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["task_id"], r["criterion"], "executed", f"gate run {r['profile']} (long run L{r['id']})", code,
                 out.strip()[-EXCERPT:], "", r["sha"], r["branch"], r["actor"], now(), str(log)))
            eid = cur.lastrowid
            event(conn, r["actor"], "evidence", eid, "run", {"task": r["task_id"], "criterion": r["criterion"],
                                                             "exit": code, "sha": r["sha"], "long_run": r["id"]})
        conn.execute("UPDATE long_runs SET status=?, ended=?, exit_code=?, log=?, tail=?, evidence_id=? WHERE id=?",
                     (status, now(), code, str(log), tail, eid, r["id"]))
        event(conn, r["actor"], "long_run", r["id"], "end", {"profile": r["profile"], "status": status, "duration": dur})
        if r["task_id"] is not None:
            conn.execute("INSERT INTO task_log (task_id, ts, actor, text) VALUES (?,?,?,?)",
                         (r["task_id"], now(), r["actor"],
                          f"long run L{r['id']} {r['profile']} {status.upper()} {r['sha'][:8]} {dur}s"))


def supervise(dbf, rid, say=print):
    """Run long run `rid` to the end in this process; the final status is always written."""
    conn = db.connect(dbf)
    try:
        r = get(conn, rid)
        if r["status"] != ACTIVE:
            return
        if r["pid"] is None:
            _claim_pid(conn, rid, os.getpid())
        handle = gates._try_oslock(lock_path(r["root"], r["profile"]),
                                   lambda: f"{os.getpid()} {r['project']} {r['profile']} L{rid} @{int(time.time())}")[0]
        if handle is None:
            _finish(conn, r, "failed", 1, f"another long run of '{r['profile']}' holds the long-run lock\n", 0)
            return
        t0, out, code, status = time.monotonic(), "", 0, "passed"
        slog = log_path(conn, rid)
        slog.parent.mkdir(parents=True, exist_ok=True)
        # Streamed as produced (no db writes): gate long / status read this file while the run is going; _finish
        # rewrites it with the header and exit line once the run ends.
        live = open(slog, "w", encoding="utf-8", buffering=1)

        def sink(line):
            live.write(line)
            live.flush()
        try:
            with budget.lease(budget.settings(r["root"])["workers"] // 2 or 1, root=r["root"], lane="long", say=say) as n:
                base = {k: v for k, v in os.environ.items() if not k.startswith("AESIR_")}
                base[budget.GATE_DIR_ENV] = str(budget.gate_dir())
                base["GATE_LONG_WORKERS"] = str(n)  # what the lease granted; steps may use up to this many
                for cmd, env in json.loads(r["steps"]):
                    left = r["timeout"] - (time.monotonic() - t0)
                    sink(f"$ {cmd}\n")
                    if left <= 0:
                        rc, text, timed = -1, "", True
                    else:
                        rc, text, timed = gates.run_step(cmd, r["cwd"], {**base, **env}, left, sink)
                    out += f"$ {cmd}\n{text}"
                    if timed:
                        out += f"\nFAIL: long run timeout (over {r['timeout']:g}s; process tree killed)\n"
                        status, code = "timed_out", 124
                        break
                    if rc:
                        out += f"[exit {rc}]\n"
                        status, code = "failed", rc
                        break
        except BaseException as e:  # always leave a final status; the lock drops with the handle
            status, code = "failed", 1
            out += f"\nsupervisor error: {type(e).__name__}: {e}\n"
        finally:
            gates._unlock(handle)
            live.close()
        _finish(conn, r, status, code, out, round(time.monotonic() - t0, 1))
    finally:
        conn.close()


def main(argv=None):
    a = sys.argv[1:] if argv is None else argv
    if os.name != "nt":  # turn a polite kill into a normal exit so supervise's cleanup kills the step's process group
        import signal
        for sig in (signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, lambda n, f: sys.exit(128 + n))
    supervise(a[0], int(a[1]), say=lambda m: None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
