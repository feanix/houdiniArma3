"""Scheduled one-off runs (task #139): `python -m aesir at <time> -- <cmd>`.
A row in scheduled_runs plus one detached waiter (`python -m aesir.sched <db> <id>`) that sleeps until the fire time,
then atomically flips the row armed -> running and runs the command. A cancelled row is no longer 'armed', so a waiter
that survives its cancel (T64: a stopped Monitor left its sleeper script alive) finds nothing to do. Cancel also tries to
kill the waiter, but correctness rests on the row, not the kill. Output goes to <db dir>/scheduled/S<id>.log."""
import calendar
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import budget, db, gates, proc
from .db import event, now, tx
from .service import AesirError

TIMEOUT = 4 * 3600  # seconds a fired command may run
ACTIVE = ("armed", "running")


def parse_when(text, ref=None):
    """UTC fire time (epoch seconds) from 'HH:MM[:SS]Z' (today) or an ISO UTC 'YYYY-MM-DDTHH:MM[:SS]Z'. Must be in the future."""
    ref = time.time() if ref is None else ref
    t = text.strip()
    m = re.fullmatch(r"(\d{1,2}):(\d\d)(?::(\d\d))?Z", t)
    try:
        if m:
            d = datetime.fromtimestamp(ref, timezone.utc)
            when = d.replace(hour=int(m[1]), minute=int(m[2]), second=int(m[3] or 0), microsecond=0)
        elif re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d(:\d\d)?Z", t):
            when = datetime.strptime(t[:-1], "%Y-%m-%dT%H:%M:%S" if t.count(":") == 2 else "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)
        else:
            raise ValueError
    except ValueError:
        raise AesirError(f"bad time {text!r}: use HH:MM[:SS]Z (UTC, today) or YYYY-MM-DDTHH:MM[:SS]Z") from None
    epoch = when.timestamp()
    if epoch <= ref:
        raise AesirError(f"{text} is not in the future (now {datetime.fromtimestamp(ref, timezone.utc):%Y-%m-%dT%H:%M:%SZ});"
                         " give a full ISO time for another day")
    return epoch


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _epoch(s):
    return calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ"))


def _db_file(conn):
    for _, name, file in conn.execute("PRAGMA database_list"):
        if name == "main" and file:
            return file
    raise AesirError("a scheduled run needs a database file (not an in-memory db)")


def log_path(conn, rid):
    return Path(_db_file(conn)).parent / "scheduled" / f"S{rid}.log"


def get(conn, rid):
    """The scheduled_runs row as a dict; AesirError if there is none."""
    r = conn.execute("SELECT * FROM scheduled_runs WHERE id=?", (int(rid),)).fetchone()
    if r is None:
        raise AesirError(f"no such scheduled run: {rid}")
    return dict(r)


def arm(conn, actor, when, command, cwd=None, spawn=True):
    """Arm `command` for `when` (epoch seconds); starts the detached waiter unless spawn=False. Returns the row as a dict."""
    if not command.strip():
        raise AesirError("give the command after --, e.g. at 03:30Z -- python -m aesir gate run full")
    dbf = _db_file(conn)
    cwd = str(Path(cwd or Path.cwd()).resolve())
    with tx(conn):
        rid = conn.execute("INSERT INTO scheduled_runs (fire_at, command, cwd, actor, armed) VALUES (?,?,?,?,?)",
                           (_iso(when), command, cwd, actor, now())).lastrowid
        log = log_path(conn, rid)
        conn.execute("UPDATE scheduled_runs SET log=? WHERE id=?", (str(log), rid))
        event(conn, actor, "scheduled_run", rid, "arm", {"fire_at": _iso(when), "command": command})
    if spawn:
        log.parent.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        pkg = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = pkg + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        p, _ = proc.spawn_detached([sys.executable, "-m", "aesir.sched", dbf, str(rid)], env,
                                   log.with_name(f"S{rid}.waiter.log"))
        conn.execute("UPDATE scheduled_runs SET pid=?, pid_start=? WHERE id=?",
                     (p.pid, str(budget.process_start(p.pid)), rid))
    return get(conn, rid)


def _kill(pid):
    if os.name == "nt":
        proc.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
    else:
        os.kill(pid, signal.SIGTERM)


def cancel(conn, actor, rid):
    """Cancel an armed run. The row flips to 'cancelled' first (that alone stops it); killing the waiter is best effort."""
    with tx(conn):
        cur = conn.execute("UPDATE scheduled_runs SET status='cancelled', ended=? WHERE id=? AND status='armed'", (now(), int(rid)))
        if cur.rowcount:
            event(conn, actor, "scheduled_run", rid, "cancel", {})
    r = get(conn, rid)
    if not cur.rowcount:
        raise AesirError(f"scheduled run {rid} is {r['status']}, not armed: nothing to cancel")
    if r["pid"] and str(budget.process_start(r["pid"])) == r["pid_start"]:
        try:
            _kill(r["pid"])
        except (OSError, subprocess.SubprocessError):
            pass  # the waiter finds the row cancelled and does nothing
    return r


def reap(conn, actor="system"):
    """Mark armed rows whose fire time passed and whose waiter is gone as 'missed' (the waiter was killed)."""
    n = 0
    for r in conn.execute("SELECT * FROM scheduled_runs WHERE status='armed'").fetchall():
        if _epoch(r["fire_at"]) + 30 < time.time() and (r["pid"] is None or str(budget.process_start(r["pid"])) != r["pid_start"]):
            with tx(conn):
                if conn.execute("UPDATE scheduled_runs SET status='missed', ended=? WHERE id=? AND status='armed'",
                                (now(), r["id"])).rowcount:
                    event(conn, actor, "scheduled_run", r["id"], "reap", {})
                    n += 1
    return n


def listing(conn, limit=10):
    """Armed and running runs first, then the latest finished ones."""
    act = [dict(r) for r in conn.execute("SELECT * FROM scheduled_runs WHERE status IN ('armed','running') ORDER BY fire_at, id")]
    done = [dict(r) for r in conn.execute("SELECT * FROM scheduled_runs WHERE status NOT IN ('armed','running') ORDER BY id DESC LIMIT ?",
                                          (limit,))]
    return act + done


def armed(conn):
    return [r for r in listing(conn, 0) if r["status"] == "armed"]


def fire(dbf, rid, sleep=time.sleep):
    """Waiter body: sleep until the fire time, then run the command only if the row is still armed."""
    conn = db.connect(dbf)
    try:
        r = get(conn, rid)
        while r["status"] == "armed":
            left = _epoch(r["fire_at"]) - time.time()
            if left <= 0:
                break
            sleep(min(left, 5))
            r = get(conn, rid)  # also notices a cancel early; the check at fire time below is what counts
        with tx(conn):  # the cancel flag, checked and claimed atomically at fire time
            cur = conn.execute("UPDATE scheduled_runs SET status='running', fired=? WHERE id=? AND status='armed'", (now(), int(rid)))
            if cur.rowcount:
                event(conn, r["actor"], "scheduled_run", rid, "fire", {"command": r["command"]})
        if not cur.rowcount:
            return None
        env = dict(os.environ)
        log = Path(r["log"])
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "w", encoding="utf-8", buffering=1) as f:
            f.write(f"$ {r['command']}  (scheduled run S{rid}, fired {now()})\n")
            rc, text, timed = gates.run_step(r["command"], r["cwd"], env, TIMEOUT, lambda line: (f.write(line), f.flush()))
            code = 124 if timed else rc
            f.write(f"[exit {code}]\n")
        with tx(conn):
            conn.execute("UPDATE scheduled_runs SET status='done', ended=?, exit_code=? WHERE id=?", (now(), code, int(rid)))
            event(conn, r["actor"], "scheduled_run", rid, "end", {"exit": code})
        return code
    finally:
        conn.close()


def main(argv=None):
    """Waiter entry point: `python -m aesir.sched <db> <id>`."""
    a = sys.argv[1:] if argv is None else argv
    if os.name != "nt":
        signal.signal(signal.SIGTERM, lambda n, f: sys.exit(128 + n))
    fire(a[0], int(a[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
