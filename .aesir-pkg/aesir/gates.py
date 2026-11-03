"""Gates: run the commands of an aesir.toml profile, record the result against a commit.
Profiles come from the main checkout so a branch cannot weaken its own gate; the one exception is the
bootstrap gate (resolve_steps): main has no such profile yet, so the branch's own is used and marked."""
import contextlib
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import proc
from . import backup, budget, db
from .db import event, now, tx
from .service import AesirError, in_scope

TAIL = 3000
STEP_TIMEOUT = 900  # seconds; aesir.toml [gate] step_timeout overrides
LOCK_BUSY_EXIT = 75  # distinct exit code for 'gate lock busy' (EX_TEMPFAIL)


class GateLockBusy(AesirError):
    """The gate lock stayed held past --lock-wait. Nothing was started or recorded."""

# infra failures that say nothing about the code: Windows STATUS_DLL_INIT_FAILED under process load, EAGAIN
FLAKY_SIGNATURES = ("0xC0000142", "3221225794", "Resource temporarily unavailable")


def flaky_signature(returncode, output):
    """The known infra signature in a failed step's exit code or output, else None."""
    hay = f"{returncode}\n{output}".lower()
    return next((s for s in FLAKY_SIGNATURES if s.lower() in hay), None)


def load_config(root) -> dict:
    p = Path(root) / "aesir.toml"
    if not p.exists():
        raise AesirError(f"no aesir.toml in {root}")
    try:
        return tomllib.loads(p.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise AesirError(f"bad aesir.toml: {e}")


def audit_step(min_severity="any"):
    """(command, env) of the audit gate step: findings new vs main in the gated tree (cwd), main's [audit] config.
    Runs the code of the aesir that runs the gate (main's), not the branch's: -P keeps cwd off sys.path and PYTHONPATH
    names this package. AESIR_DB "" is filled in by run_profile with the db it records into."""
    sev = "" if min_severity == "any" else f" --min-severity {min_severity}"
    return (f'"{sys.executable}" -P -m aesir audit run --dir .{sev}', {"PYTHONPATH": str(Path(__file__).resolve().parent.parent), "AESIR_DB": ""})


def _db_file(conn):
    """Path of the main database file of `conn`, else '' (in-memory)."""
    row = conn.execute("PRAGMA database_list").fetchone()
    return row[2] if row and row[2] else ""


class Step(tuple):
    """(command, env), equal to that plain tuple; `.when` is the when_changed globs (empty: always runs, #154)."""

    def __new__(cls, cmd, env, when=()):
        self = super().__new__(cls, (cmd, env))
        self.when = tuple(when)
        return self


def _when_changed(e):
    w = e.get("when_changed", ())
    if isinstance(w, str):
        w = [w]
    if not isinstance(w, (list, tuple)) or not w or not all(isinstance(g, str) and g.strip() for g in w):
        raise AesirError("bad gate entry: when_changed must be a non-empty list of path globs")
    return w


def profile_steps(cfg, profile):
    """Resolve a profile to [(command, env)]. An entry is a string, an inline table {cmd, env},
    {profile = "quick"} which expands another profile in place, or {audit = "new", min_severity = "medium"}: the opt-in audit step (#116, #133).
    A {cmd} or {audit} entry may add when_changed = ["glob", ...]: it then runs only when the gated commit changes a matching path (#154)."""
    gates = cfg.get("gates", {})
    if profile not in gates:
        raise AesirError(f"no gate profile '{profile}' (have: {', '.join(gates) or 'none'})")
    steps = []

    def expand(name, seen):
        if name in seen or name not in gates:
            raise AesirError(f"bad gate profile reference: {name}")
        for e in gates[name]:
            if isinstance(e, str):
                steps.append((e, {}))
            elif "audit" in e:
                if e["audit"] != "new":
                    raise AesirError(f'bad gate entry: audit = "{e["audit"]}" (only "new" exists)')
                sev = e.get("min_severity", "any")
                if sev not in ("any", "info", "low", "medium", "high"):  # audit.SEVERITIES; audit imports gates
                    raise AesirError(f'bad gate entry: min_severity = "{sev}" (any, info, low, medium, high)')
                steps.append(Step(*audit_step(sev), _when_changed(e) if "when_changed" in e else ()))
            elif "profile" in e:
                expand(e["profile"], seen | {name})
            else:
                steps.append(Step(e["cmd"], {k: str(v) for k, v in e.get("env", {}).items()},
                                  _when_changed(e) if "when_changed" in e else ()))
    expand(profile, frozenset())
    return steps


BOOTSTRAP_MARK = "bootstrap gate: profile '{}' is not in main's aesir.toml, ran the branch's own"


def resolve_steps(root, cwd, profile):
    """(steps, bootstrap). Main's profile always wins; only when main lacks it does the branch checkout's
    aesir.toml supply it (the first task that adds [gates] could not otherwise be gated before merge)."""
    cfg = load_config(root)
    if profile in cfg.get("gates", {}) or Path(cwd).resolve() == Path(root).resolve():
        return profile_steps(cfg, profile), False
    try:
        branch_cfg = load_config(cwd)
    except AesirError:
        return profile_steps(cfg, profile), False  # no branch config either: report main's error
    if profile not in branch_cfg.get("gates", {}):
        return profile_steps(cfg, profile), False
    return profile_steps(branch_cfg, profile), True


def git(cwd, *args):
    r = proc.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8")
    if r.returncode:
        raise AesirError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout.strip()


# untracked paths that never count as a dirty tree (agent and state scaffolding, the db)
IGNORED_UNTRACKED = (".claude/", backup.STATE_DIR + "/", ".aesir/")


def dirty_files(cwd):
    """Porcelain lines for uncommitted changes. Other untracked files count (they can hide a missing add)."""
    lines = git(cwd, "status", "--porcelain", "--untracked-files=all").splitlines()
    return [ln for ln in lines if not _ignorable(ln)]


def path_of(ln):
    """The file path of one porcelain line (a rename gives the new name, the file on disk)."""
    return ln[3:].strip().strip('"').split(" -> ")[-1].strip('"')


def dirty_paths(cwd, lines=None):
    """Paths of the uncommitted changes in `cwd` (dirty_files), or of the porcelain `lines` already read."""
    return [path_of(ln) for ln in (dirty_files(cwd) if lines is None else lines)]


def dirty_detail(conn, cwd, lines, limit=20):
    """#138: the porcelain lines with, for each file, its modified time (UTC) and the agents the registry says were
    running then, so whoever wrote a stray file can be found. A directory entry ('?? dir/') is stat'ed as is; a file
    that is gone (deleted) has no time. Registry trouble only drops the agent part."""
    out = []
    for ln in lines[:limit]:
        path = path_of(ln)
        try:
            ts = datetime.fromtimestamp((Path(cwd) / path).stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except OSError:
            out.append(f"{ln}  (not on disk)")
            continue
        try:
            who = [f"{r['agent_id']}" + (f" (task {r['task_id']})" if r["task_id"] else "") for r in conn.execute(
                "SELECT agent_id, task_id FROM agents WHERE started<=? AND (ended='' OR ended>=?) ORDER BY id", (ts, ts))]
        except sqlite3.Error:
            who = None
        out.append(f"{ln}  modified {ts}; " + ("agents running then: " + (", ".join(who) if who else "none registered")
                                               if who is not None else "agent registry unavailable"))
    return out + ([f"... and {len(lines) - limit} more"] if len(lines) > limit else [])


def _ignorable(line):
    """Untracked scaffolding, or any __pycache__/ dir or *.pyc file (tracked or not: CLI runs rewrite them)."""
    path = line[3:].strip().strip('"').replace("\\", "/")
    if "__pycache__/" in path or path.endswith(".pyc"):
        return True
    return line.startswith("?? ") and path.startswith(IGNORED_UNTRACKED)


def pid_alive(pid):
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


HOLDER_ENV = "GATE_LOCK_HOLDER_PID"


def gate_lock_path():
    """Machine-wide lockfile (per user temp dir, not the repo). AESIR_GATE_LOCK overrides the path."""
    return Path(os.environ.get("AESIR_GATE_LOCK") or Path(tempfile.gettempdir()) / "aesir-gate.lock")


def project_name(root):
    """[project] name from aesir.toml, else the directory name; used to label the lock."""
    try:
        return load_config(root).get("project", {}).get("name") or Path(root).name
    except AesirError:
        return Path(root).name


def gate_settings(root):
    """(lock_wait or None, step_timeout) from aesir.toml [gate]."""
    try:
        g = load_config(root).get("gate", {})
    except AesirError:
        g = {}
    return g.get("lock_wait"), g.get("step_timeout", STEP_TIMEOUT)


def _describe(held):
    """'pid label @since' (since absent in pre-#70 locks) -> (label, pid, seconds held or None)."""
    pid, _, rest = held.partition(" ")
    label, sep, since = rest.rpartition(" @")
    if sep and since.isdigit():
        return label, pid, max(0, int(time.time() - int(since)))
    return rest or held, pid, None


LOCK_OFFSET = budget.LOCK_OFFSET
_thread_locks: dict = {}
_thread_locks_guard = threading.Lock()
_os_lock = budget.os_lock


def _os_lock_path(path):
    """The OS-lock file sits beside the legacy pid file: older Aesir loops forever on an existing empty file at the
    legacy path, so the legacy path only ever holds a 'pid label @start' file while a gate runs."""
    return path.with_name(path.name + ".os")


def _claim_legacy(path, content):
    """With the OS lock held, create the legacy pid file (O_EXCL) so older Aesir waits. Returns '' on success, else the
    live legacy holder's text. Dead-pid, own-pid and long-empty leftovers are reclaimed, as the old protocol did."""
    for _ in range(50):
        try:
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError:
            held = _read_label(path)
            hpid = held.split(" ", 1)[0]
            if held and hpid.isdigit() and hpid != str(os.getpid()) and pid_alive(hpid):
                return held
            if not held:  # empty: an old holder between create and write, or a leftover; only reclaim when old
                try:
                    if time.time() - path.stat().st_mtime < 5:
                        return "?"
                except OSError:
                    continue
            with contextlib.suppress(OSError):
                path.unlink()
            time.sleep(0.05)
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        return ""
    return "?"


def _release_legacy(path):
    """Remove our legacy pid file; Windows refuses while a waiter has it open, so retry. Never truncate it empty."""
    for _ in range(200):
        try:
            path.unlink()
            return
        except FileNotFoundError:
            return
        except PermissionError:
            time.sleep(0.05)
        except OSError:
            return


def _read_label(path):
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def slot_path(key):
    """Per-working-tree slot lockfile in the gate dir: gates of different projects or worktrees do not share one."""
    tag = hashlib.sha1(os.path.normcase(str(Path(key).resolve())).encode("utf-8")).hexdigest()[:12]
    return budget.gate_dir() / f"gate-{tag}.lock"


def _try_oslock(lockfile, label, claim=None):
    """One non-blocking attempt at the OS lock on `lockfile` (threads of this process serialize on a threading.Lock).
    `claim`, with the lock held, returns '' or the text of a legacy-protocol holder. Returns ((fd, tlock) or None,
    holder text for the waiting message)."""
    with _thread_locks_guard:
        tlock = _thread_locks.setdefault(str(lockfile), threading.Lock())
    held = ""
    if tlock.acquire(blocking=False):
        fd = os.open(str(lockfile), os.O_RDWR | os.O_CREAT)
        if _os_lock(fd, True):
            held = claim() if claim else ""
            if not held:
                os.ftruncate(fd, 0)
                os.lseek(fd, 0, os.SEEK_SET)
                os.write(fd, label().encode("utf-8"))
                return (fd, tlock), ""
            _os_lock(fd, False)
        os.close(fd)
        tlock.release()
    return None, held or _read_label(lockfile)


def _unlock(handle):
    fd, tlock = handle
    with contextlib.suppress(OSError):
        os.ftruncate(fd, 0)  # no stale label for waiters; the OS-lock file itself is never deleted
    _os_lock(fd, False)
    os.close(fd)
    tlock.release()


PRIO_QUEUE, PRIO_GATE = 0, 1  # ticket classes: a queue run is served before plain gate runs; FIFO within a class


class _Line:
    """Arrival-order line for the machine gate lock (aesir-reports#89). Each waiter drops a ticket file in
    <legacy lock>.tickets/ (a NEW machine file; the lock files and the legacy protocol are untouched, so older Aesir
    keeps contending as before and is never blocked by tickets). Only the first live ticket, ordered by (class, arrival
    time), may try the lock. Tickets of dead processes (pid gone, or pid reused: different start token as budget does)
    are skipped and removed; the owner removes its own on acquire, timeout or error."""

    def __init__(self, legacy, what, prio):
        self.dir = legacy.with_name(legacy.name + ".tickets")
        self.prio = prio
        self.name = f"{time.time_ns():020d}-{os.getpid()}-{uuid.uuid4().hex[:8]}.json"
        self.what = what
        self.path = self.dir / self.name

    def join(self):
        pid = os.getpid()
        body = json.dumps({"pid": pid, "start": budget.process_start(pid), "prio": self.prio, "what": self.what,
                           "ts": time.time_ns()})
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / (self.name + ".tmp")  # written whole, then renamed: readers never see a partial ticket
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, self.path)

    def leave(self):
        """Remove our ticket. Windows refuses while a peer has it open (every waiter reads all tickets each poll), and
        our pid stays alive, so a swallowed failure left a live ticket that blocked the line (#229): retry, as
        _release_legacy does."""
        for _ in range(200):
            try:
                self.path.unlink()
                return
            except FileNotFoundError:
                return
            except PermissionError:
                time.sleep(0.05)
            except OSError:
                return

    def position(self):
        """(1-based place of our ticket among live tickets, number of live tickets)."""
        live = []
        try:
            listing = os.listdir(self.dir)
        except OSError:
            return 1, 1
        for n in listing:  # a process that died between writing <name>.tmp and the rename leaves it behind
            if n.endswith(".tmp"):
                with contextlib.suppress(OSError):
                    if time.time() - (self.dir / n).stat().st_mtime > 30:
                        (self.dir / n).unlink()
        names = [n for n in listing if n.endswith(".json")]
        for n in names:
            p = self.dir / n
            try:
                t = json.loads(p.read_text(encoding="utf-8"))
                key = (int(t["prio"]), int(t["ts"]), n)
                ok = budget.process_start(t["pid"]) == t["start"]
            except FileNotFoundError:
                continue
            except (OSError, ValueError, KeyError, TypeError):
                with contextlib.suppress(OSError):  # unreadable: garbage once it is old enough not to be mid-write
                    if time.time() - p.stat().st_mtime > 30:
                        p.unlink()
                continue
            if ok or n == self.name:
                live.append(key)
            else:
                with contextlib.suppress(OSError):
                    p.unlink()
        live.sort()
        mine = [i for i, k in enumerate(live) if k[2] == self.name]
        return (mine[0] + 1 if mine else 1), max(len(live), 1)


def _holder_info(held):
    """Holder of a lock from its 'pid label @since' text as {project, task, pid, held_s}. The label is
    '<project> task N' or '<project> <profile> gate' (see run_profile / queue.run); anything else is kept whole."""
    if not held:
        return {"project": "", "task": None, "pid": None, "held_s": None}
    who, hpid, age = _describe(held)
    m = re.match(r"^(.*?) task (\d+)$", who)
    project, task = (m.group(1), int(m.group(2))) if m else (re.sub(r" \S+ gate$", "", who), None)
    return {"project": project, "task": task, "pid": int(hpid) if hpid.isdigit() else None, "held_s": age}


def record_lock_wait(conn, actor, project, what, info):
    """One event per gate-lock wait or give-up (entity gate/lock, action wait; no schema change). `what` names the
    waiter ('full gate task 12' or 'queue task 12'); `info` is _held_lock's report: waited_s, outcome and the holder.
    Never raises: a locked or full db must not fail the gate that just waited."""
    try:
        with tx(conn):
            event(conn, actor, "gate", "lock", "wait", {"project": project, "what": what, **info})
    except (sqlite3.Error, OSError, AesirError) as e:  # recording is best effort
        print(f"gate lock: wait not recorded ({e})", file=sys.stderr)


def lock_stats(conn, since=""):
    """Per-project summary of the recorded lock waits (events gate/lock/wait) at or after `since` (YYYY-MM-DD or
    timestamp, UTC): waits, give-ups, total and longest wait of the project as a waiter, and the longest hold seen
    of its gate runs (the held_s a waiter saw, so a lower bound of the real hold). Sorted by total wait."""
    rows = conn.execute("SELECT payload FROM events WHERE entity='gate' AND entity_id='lock' AND action='wait' AND ts >= ?",
                        (since,)).fetchall()
    stats = {}

    def of(project):
        return stats.setdefault(project or "?", {"project": project or "?", "waits": 0, "gave_up": 0, "total_wait_s": 0.0,
                                                 "longest_wait_s": 0.0, "longest_hold_s": 0})
    for r in rows:
        try:
            p = json.loads(r["payload"])
        except ValueError:
            continue
        w = of(p.get("project"))
        w["waits"] += 1
        w["gave_up"] += p.get("outcome") == "gave_up"
        w["total_wait_s"] = round(w["total_wait_s"] + float(p.get("waited_s") or 0), 1)
        w["longest_wait_s"] = max(w["longest_wait_s"], float(p.get("waited_s") or 0))
        if p.get("held_s") is not None and p.get("holder_project"):
            h = of(p["holder_project"])
            h["longest_hold_s"] = max(h["longest_hold_s"], int(p["held_s"]))
    return sorted(stats.values(), key=lambda w: -w["total_wait_s"])


@contextlib.contextmanager
def _held_lock(lockfile, what, poll, say, deadline, claim=None, legacy_path=None, line=None, on_wait=None,
               progress_every=None):
    """Wait for the OS lock on `lockfile` (printing the holder once), hold it for the block, then release. Raises
    GateLockBusy at `deadline`. `legacy_path`: the pid file of the legacy protocol, removed on release. `line`: a
    _Line; only the waiter first in it tries the lock, the rest wait their turn. `on_wait(info)`: called once if we
    had to wait (not when the lock was free), on acquire or give-up, with waited_s, outcome and the holder.
    `progress_every`: seconds; say a progress line (place, holder, waited) at most that often, not only on change."""
    told = None
    t_wait = None  # monotonic time of the first refusal; None = never had to wait
    last = None  # holder text last seen
    last_say = None
    if line:
        try:
            line.join()
        except OSError as e:  # tickets dir unusable (read-only, full, AV lock): fall back to old unordered contention
            print(f"gate lock: cannot queue in arrival order ({e}); contending unordered", file=sys.stderr)
            line.leave()
            line = None

    def report(outcome):
        if on_wait and t_wait is not None:
            h = _holder_info(last)
            on_wait({"outcome": outcome, "waited_s": round(time.monotonic() - t_wait, 1), "holder_project": h["project"],
                     "holder_task": h["task"], "holder_pid": h["pid"], "held_s": h["held_s"]})

    try:
        while True:
            pos, total = line.position() if line else (1, 1)
            if pos == 1:
                handle, held = _try_oslock(lockfile, lambda: f"{os.getpid()} {what} @{int(time.time())}", claim)
                if handle:
                    break
            else:
                handle, held = None, _read_label(lockfile) or _read_label(legacy_path or lockfile)
            if t_wait is None:
                t_wait = time.monotonic()
            last = held or last
            who, hpid, age = _describe(held) if held else ("", "", None)
            if deadline is not None and time.monotonic() >= deadline:
                report("gave_up")
                raise GateLockBusy(f"gate lock busy: held by {who} (pid {hpid})" + (f" for {age}s" if age is not None else "")
                                   if held else "gate lock busy")
            where = f"; position {pos} of {total}" if line else ""
            mark = (held, pos) if line else held
            now = time.monotonic()
            if progress_every is not None:
                if last_say is None or now - last_say >= progress_every:
                    last_say = now
                    say("waiting for gate lock" + (f": position {pos} of {total}" if line else "") + "; "
                        + (f"held by {who} (pid {hpid}{'' if age is None else f', held {age}s'})" if held else "holder unknown")
                        + f"; waited {int(now - t_wait)}s")
            elif held and mark != told:
                told = mark
                say(f"waiting for gate lock held by {who} (pid {hpid}{'' if age is None else f', held {age}s'})" + where)
            time.sleep(poll)
    finally:
        if line:
            line.leave()
    report("acquired")
    try:
        yield
    finally:
        if legacy_path is not None:
            _release_legacy(legacy_path)
        _unlock(handle)


@contextlib.contextmanager
def gate_lock(label, poll=0.5, say=None, wait=None, key=None, prio=PRIO_GATE, on_wait=None, progress_every=None):
    """Admission for one gate run. Takes, in order: the slot lock of the working tree `key` (default cwd; one gate per
    tree), the legacy machine lock %TEMP%/aesir-gate.lock when [machine] legacy_lock is true (the default: one gate
    at a time per machine, what older Aesir releases expect), then a place in the machine worker budget (budget.py).
    With legacy_lock false, gates of different trees run at once while the budget allows.
    Every lock is an OS lock (msvcrt.locking / fcntl.flock) on an open handle of a never-deleted file with
    'pid label @start' written for waiters; a dead holder drops it with its handle, nothing is reclaimed or unlinked
    (that read+unlink raced on Windows: aesir-reports#20). After `wait` seconds raises GateLockBusy.
    Waiters for the machine lock are served in arrival order via tickets in <lock>.tickets/ (class `prio`: the merge
    queue passes PRIO_QUEUE and goes first; see _Line); older Aesir ignores tickets and contends as before (#180).
    `on_wait(info)` hears every wait and give-up on the tree or machine lock (see _held_lock; callers record it);
    `progress_every` makes waiting say a progress line at most that often (the queue: once a minute).
    Concurrent runs exhaust Windows process resources (STATUS_DLL_INIT_FAILED): hence the budget."""
    say = say or (lambda m: print(m, flush=True))
    key = Path(key or os.getcwd())
    cfg = budget.settings(db.find_root(key))
    legacy = gate_lock_path()
    deadline = None if wait is None else time.monotonic() + float(wait)
    # Nested run: an ancestor holds a gate and exported its pid (deliberately not AESIR_-prefixed, so env filters that
    # strip AESIR_* keep it). Never wait on ourselves; the ancestor releases it.
    anc = os.environ.get(HOLDER_ENV, "")
    if anc.isdigit() and anc != str(os.getpid()) and pid_alive(anc):  # own pid: sibling threads must still serialize
        if _read_label(_os_lock_path(legacy)).startswith(f"{anc} ") or budget.holds_gate(anc):
            yield
            return
    legacy.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.ExitStack() as stack:
        slot = slot_path(key)
        slot.parent.mkdir(parents=True, exist_ok=True)
        stack.enter_context(_held_lock(slot, label, poll, say, deadline, on_wait=on_wait,
                                          progress_every=progress_every))
        if cfg["legacy_lock"]:
            stack.enter_context(_held_lock(
                _os_lock_path(legacy), label, poll, say, deadline,
                claim=lambda: _claim_legacy(legacy, f"{os.getpid()} {label} @{int(time.time())}"), legacy_path=legacy,
                line=_Line(legacy, label, prio), on_wait=on_wait, progress_every=progress_every))
        try:
            stack.enter_context(budget.gate_slot(label, cfg, deadline, poll, say))
        except budget.BudgetBusy as e:
            raise GateLockBusy(str(e)) from None
        prev = os.environ.get(HOLDER_ENV)
        os.environ[HOLDER_ENV] = str(os.getpid())
        try:
            yield
        finally:
            if prev is None:
                os.environ.pop(HOLDER_ENV, None)
            else:
                os.environ[HOLDER_ENV] = prev


# ---- step process tree: job object (Windows) / process group (POSIX), watchdog ----------------------------------

_SITECUSTOMIZE = (
    "import faulthandler, os\n"
    "_t = os.environ.get('GATE_STEP_DUMP_AFTER')\n"
    "if _t:\n"
    "    faulthandler.dump_traceback_later(float(_t), exit=False)\n"
)


def _make_job():
    """Windows job object that kills everything in it when its last handle closes (i.e. when the gate dies)."""
    import ctypes
    from ctypes import wintypes as w
    k = ctypes.windll.kernel32
    k.CreateJobObjectW.restype = w.HANDLE
    k.CreateJobObjectW.argtypes = [w.LPVOID, w.LPCWSTR]
    k.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD]
    k.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
    k.CloseHandle.argtypes = [w.HANDLE]

    class Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", w.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", w.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", w.DWORD), ("SchedulingClass", w.DWORD)]

    class Ext(ctypes.Structure):
        _fields_ = [("Basic", Basic), ("Io", ctypes.c_ulonglong * 6), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]
    job = k.CreateJobObjectW(None, None)
    if not job:
        return None
    info = Ext()
    info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not k.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
        k.CloseHandle(job)
        return None
    return job


class StepProc:
    """A step subprocess whose whole tree dies together: on kill(), on close(), and (Windows) when the gate dies."""

    def __init__(self, cmd, shell=True, **kw):
        self.job = _make_job() if os.name == "nt" else None
        if os.name != "nt":
            kw["start_new_session"] = True
        self.p = proc.Popen(cmd, shell=shell, **kw)
        if self.job:
            import ctypes
            from ctypes import wintypes as w
            k = ctypes.windll.kernel32
            k.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
            if not k.AssignProcessToJobObject(self.job, int(self.p._handle)):
                self._close_job()  # in a non-nestable job already: fall back to taskkill /T

    def _close_job(self):
        if self.job:
            import ctypes
            ctypes.windll.kernel32.CloseHandle(self.job)
            self.job = None

    def kill(self):
        if self.job:
            import ctypes
            ctypes.windll.kernel32.TerminateJobObject(self.job, 1)
        elif os.name == "nt":
            proc.run(["taskkill", "/F", "/T", "/PID", str(self.p.pid)], capture_output=True)
        else:
            import signal
            with contextlib.suppress(OSError):
                os.killpg(self.p.pid, signal.SIGKILL)
        with contextlib.suppress(OSError):
            self.p.kill()

    def close(self):
        self._close_job()  # kills stragglers of a finished step


def _pump(stream, lines, sink):
    """Reader thread for a streamed step: collect each output line and hand it to `sink` as it arrives."""
    for line in iter(stream.readline, ""):
        lines.append(line)
        sink(line)


def run_step(cmd, cwd, env, timeout, sink=None):
    """Run one step under a watchdog. Returns (returncode, output, timed_out). On expiry the step's python processes
    dump tracebacks (faulthandler, via a sitecustomize on PYTHONPATH) into the output, then the whole tree is killed.
    sink: optional callable(line) fed each output line as it is produced (long runs stream to their log, #112); the
    returned output is the same either way."""
    with tempfile.TemporaryDirectory(prefix="aesir-step-") as sc:
        Path(sc, "sitecustomize.py").write_text(_SITECUSTOMIZE, encoding="utf-8")
        env = dict(env)
        env["PYTHONPATH"] = sc + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env["GATE_STEP_DUMP_AFTER"] = f"{max(timeout - 1.5, timeout * 0.5):.2f}"
        sp = StepProc(cmd, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                      stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        lines, reader = [], None
        if sink:
            reader = threading.Thread(target=_pump, args=(sp.p.stdout, lines, sink), daemon=True)
            reader.start()
        try:
            try:
                if reader:
                    sp.p.wait(timeout=timeout)
                    reader.join(10)  # an escapee holding the pipe must not hang the step
                    return sp.p.returncode, "".join(lines), False
                out, _ = sp.p.communicate(timeout=timeout)
                return sp.p.returncode, out, False
            except subprocess.TimeoutExpired as e:
                partial = e.output or ""
                if isinstance(partial, bytes):
                    partial = partial.decode("utf-8", "replace")
                sp.kill()
                if reader:
                    reader.join(10)
                    return -1, "".join(lines), True
                try:
                    rest, _ = sp.p.communicate(timeout=10)
                except subprocess.TimeoutExpired:  # an escapee holds the pipe; give up on the rest
                    rest = ""
                return -1, partial + (rest or ""), True
        except BaseException:  # interrupted (e.g. the long-run supervisor got SIGTERM): do not orphan the step's tree
            sp.kill()
            raise
        finally:
            if reader and not reader.is_alive():  # communicate() closes the pipe on the plain path; do it here
                sp.p.stdout.close()
            sp.close()


def changed_paths(cwd, base=None):
    """Paths the commit at `cwd` HEAD changes versus `base` (default: merge-base with main), or None when that cannot
    be determined (no main, no merge base): a step gated on it then runs rather than being skipped unseen."""
    try:
        if base is None:
            base = git(cwd, "merge-base", "HEAD", "main")
        out = git(cwd, "-c", "core.quotepath=off", "diff", "--name-only", "--no-renames", base, "HEAD")
    except AesirError:
        return None
    return out.splitlines()


def _label(cmd, n=70):
    one = " ".join(cmd.split())
    return one if len(one) <= n else one[:n - 3] + "..."


def format_steps(steps):
    return "; ".join(f"{s['result']}: {s['step']}" + (f" ({s['why']})" if s.get("why") else "") for s in steps)


def timing_line(waited, steps, run):
    """'lock wait 812s; steps: fmt 4.1s, test 690.0s; run 995.3s': where a gate run's wall time went (lock wait apart
    from run time; `steps` is [(label, seconds)] of the steps that ran). One line, also in the tail and the task log."""
    def name(c):  # '"C:\x\python.exe" -m foo' -> 'python.exe -m foo': the directory is noise in a one-line summary
        return _label(re.sub(r'^"?(?:[^"\s]*[\\/])?([^"\s]+)"?', r"\1", c.strip()), 40)
    parts = ", ".join(f"{name(c)} {sec:.1f}s" for c, sec in steps)
    return f"lock wait {int(round(waited))}s; steps: {parts or 'none'}; run {run:.1f}s"


def run_profile(conn, actor, profile, cwd, task=None, agent_id=None, config_root=None, lock_wait=None,
                locked=False, step_timeout=None, changed_base=None, waited=0.0):
    """Run every step (stop at first failure), record one gate_runs row, return it as a dict.
    A dirty tree records a FAIL ('dirty tree') without running anything: the sha would not describe what ran.
    lock_wait: give up with GateLockBusy (nothing recorded) after that many seconds; default from aesir.toml [gate].
    locked: the caller already holds the gate lock (the merge queue does); `waited` is then the seconds it waited.
    A step with when_changed runs only if the commit changes a matching path vs changed_base (default: merge-base with
    main); else it is reported skipped. The result's `steps` lists every step as pass, FAIL, skipped or not run (#154).
    A step running over step_timeout (default [gate] step_timeout) is killed with its tree and the gate records FAIL."""
    cwd = Path(cwd).resolve()
    root = config_root or db.find_root(cwd)
    steps, bootstrap = resolve_steps(root, cwd, profile)
    cfg_wait, cfg_timeout = gate_settings(root)
    lock_wait = cfg_wait if lock_wait is None else lock_wait
    step_timeout = float(cfg_timeout if step_timeout is None else step_timeout)
    sha = git(cwd, "rev-parse", "HEAD")
    branch = git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    if task is not None and not conn.execute("SELECT 1 FROM tasks WHERE id=?", (int(task),)).fetchone():
        raise AesirError(f"no such task: {task}")
    dirty = dirty_files(cwd)
    base = {k: v for k, v in os.environ.items() if not k.startswith("AESIR_")}
    out, ok, flaky = "", True, []
    report = [{"step": _label(c), "result": "not run", "why": "tree not clean" if dirty else "earlier step failed"}
              for c, _ in steps]
    changed = changed_paths(cwd, changed_base) if not dirty and any(getattr(st, "when", ()) for st in steps) else None
    if dirty:
        ok = False
        out = "dirty tree: commit or stash first\n" + "\n".join(dirty_detail(conn, cwd, dirty)) + "\n"
    what = f"task {task}" if task is not None else f"{profile} gate"
    t_lock = time.monotonic()
    step_secs = []  # (command, wall seconds incl. an infra retry) of each step that ran
    with contextlib.nullcontext() if dirty or locked else gate_lock(
            f"{project_name(root)} {what}", wait=lock_wait, key=cwd,
            on_wait=lambda i: record_lock_wait(conn, actor, project_name(root), what if task is None else f"{profile} gate {what}", i)):
        t0 = time.monotonic()  # after the lock wait: duration is run time only
        waited = waited if locked else t0 - t_lock
        base[budget.GATE_DIR_ENV] = str(budget.gate_dir())  # steps (paratest) lease from the same budget
        if HOLDER_ENV in os.environ:  # set by gate_lock after `base` was built; nested gates in steps need it
            base[HOLDER_ENV] = os.environ[HOLDER_ENV]
        for i, st in enumerate([] if dirty else steps):
            cmd, env = st
            when = getattr(st, "when", ())
            if when and changed is not None and not any(in_scope(p, ",".join(when)) for p in changed):
                report[i] = {"step": _label(cmd), "result": "skipped", "why": "no matching change: " + ", ".join(when)}
                continue
            report[i] = {"step": _label(cmd), "result": "FAIL", "why": ""}
            if env.get("AESIR_DB") == "":  # placeholder of audit_step: read the findings of the db being gated
                env = {**env, "AESIR_DB": _db_file(conn)}
            t_step = time.monotonic()
            for attempt in (0, 1):
                rc, text, timed_out = run_step(cmd, cwd, {**base, **env}, step_timeout)
                sig = flaky_signature(rc, text) if rc and not timed_out else None
                if sig and not attempt:  # retry an infra failure once, keep the first attempt's output
                    flaky.append(sig)
                    out += f"$ {cmd}\n{text}[exit {rc}] infra failure ({sig}), retrying once\n"
                    continue
                break
            step_secs.append((cmd, time.monotonic() - t_step))
            if timed_out:
                ok = False
                report[i]["why"] = "timeout"
                out += f"$ {cmd}\n{text}\nFAIL: step timeout {cmd} (over {step_timeout:g}s; process tree killed)\n"
                break
            out += f"$ {cmd}\n{text}"
            if rc:
                ok = False
                report[i]["why"] = f"exit {rc}"
                out += f"[exit {rc}]\n"
                break
            report[i] = {"step": _label(cmd), "result": "pass", "why": ""}
        for sig in flaky:  # last lines, so the tail cut keeps them
            out += f"flaky-retry: {sig}\n"
        dur = round(time.monotonic() - t0, 1)
        timing = timing_line(waited, step_secs, dur)
        out += timing + "\n"  # last line, so the tail cut keeps it
    tail = out[-TAIL:]
    # recorded in the existing tail text and task log: no schema change
    stored = BOOTSTRAP_MARK.format(profile) + "\n" + tail if bootstrap else tail
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO gate_runs (task_id, profile, sha, branch, passed, duration, tail, agent_id, ts)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (task, profile, sha, branch, int(ok), dur, stored, agent_id, now()))
        event(conn, actor, "gate", cur.lastrowid, "run",
              {"task": task, "profile": profile, "sha": sha, "passed": ok, "duration": dur})
        for sig in flaky:
            event(conn, actor, "gate", cur.lastrowid, "flaky", {"task": task, "profile": profile, "signature": sig})
        if task is not None:
            conn.execute("INSERT INTO task_log (task_id, ts, actor, text) VALUES (?,?,?,?)",
                         (int(task), now(), actor, f"gate {profile} {'PASS' if ok else 'FAIL'} {sha[:8]} {dur}s ({timing})"
                         + (" (bootstrap gate: branch profile)" if bootstrap else "")
                         + (f" | steps: {format_steps(report)}" if len(report) > 1 or any(r["result"] == "skipped" for r in report) else "")))
    if ok or flaky:
        from . import friction  # lazy: friction reads this module's config loader
        with tx(conn):
            for sig in flaky:  # #34's infra-retry marker: a red that went green on retry
                friction.record(conn, actor, "false_red", f"gate {profile} infra retry ({sig})", task=task)
            if ok:
                friction.false_red(conn, actor, task, profile, sha)
    return {"id": cur.lastrowid, "task": task, "profile": profile, "sha": sha, "branch": branch,
            "passed": ok, "duration": dur, "tail": tail, "bootstrap": bootstrap, "steps": report,
            "lock_wait": round(waited, 1), "step_secs": [[c, round(x, 1)] for c, x in step_secs], "timing": timing}


# profiles that satisfy a requirement (full includes quick)
SATISFIES = {"quick": ("quick", "full"), "full": ("full",)}


def passed_at_head(conn, task_id, sha, tier):
    """The newest passing gate_runs row of the task at exactly `sha` whose profile is `tier` or higher (a passing full
    gate satisfies a quick requirement), as a sqlite Row; None when there is none. Matches the sha exactly, so a
    later commit never reuses an older pass. Callers decide what a hit means (skip a re-run, accept a tier)."""
    ps = SATISFIES[tier]
    return conn.execute(
        f"SELECT * FROM gate_runs WHERE task_id=? AND sha=? AND passed=1 AND profile IN ({','.join('?' * len(ps))}) "
        "ORDER BY id DESC LIMIT 1", (int(task_id), sha, *ps)).fetchone()


def has_pass(conn, task, profile, sha) -> bool:
    return passed_at_head(conn, task, sha, profile) is not None


REUSE_SCAN = 60  # newest passing runs looked at for a tree match (one git call each)


def passing_run_on_tree(conn, profile, tree, cwd, info=None):
    """#244: the newest recorded passing gate run that satisfies `profile` (same or higher tier, SATISFIES) and whose
    sha has git tree `tree`, else None. A gate result is a function of the tree it ran on, so the queue need not
    rerun it for a merge commit with an identical tree. Bootstrap runs (they used the branch's own profile) and shas
    git cannot resolve are not candidates. When the scan reads REUSE_SCAN rows without a match and `info` is a dict,
    sets info["capped"] = REUSE_SCAN so the caller can say older runs were not looked at."""
    ps = SATISFIES[profile]
    rows = conn.execute(
        f"SELECT id, profile, sha FROM gate_runs WHERE passed=1 AND profile IN ({','.join('?' * len(ps))})"
        " AND tail NOT LIKE ? ORDER BY id DESC LIMIT ?",
        (*ps, BOOTSTRAP_MARK.split("{")[0] + "%", REUSE_SCAN)).fetchall()
    seen = {}
    for r in rows:
        if r["sha"] not in seen:
            try:
                seen[r["sha"]] = git(cwd, "rev-parse", r["sha"] + "^{tree}")
            except AesirError:
                seen[r["sha"]] = None
        if seen[r["sha"]] == tree:
            return dict(r)
    if info is not None and len(rows) >= REUSE_SCAN:
        info["capped"] = REUSE_SCAN
    return None


def record_reused(conn, actor, profile, task, sha, branch, prior, tree, waited=0.0, elapsed=0.0):
    """Record a passing `profile` run on `sha` that reused run `prior` (same tree): has_pass and --carried-by see it like
    a real run. Returns the run dict in run_profile's shape, `steps` empty and `reused` naming the prior run."""
    timing = (f"lock wait {int(round(waited))}s; reused gate run {prior['id']} ({prior['profile']}, "
              f"tree {tree[:12]}); run {elapsed:.1f}s")
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO gate_runs (task_id, profile, sha, branch, passed, duration, tail, agent_id, ts)"
            " VALUES (?,?,?,?,1,?,?,NULL,?)", (task, profile, sha, branch, round(elapsed, 1), timing, now()))
        event(conn, actor, "gate", cur.lastrowid, "reused",
              {"task": task, "profile": profile, "sha": sha, "tree": tree, "reused_run": prior["id"]})
        conn.execute("INSERT INTO task_log (task_id, ts, actor, text) VALUES (?,?,?,?)",
                     (int(task), now(), actor, f"gate {profile} PASS {sha[:8]} reused ({timing})"))
    return {"id": cur.lastrowid, "task": task, "profile": profile, "sha": sha, "branch": branch, "passed": True,
            "duration": round(elapsed, 1), "tail": timing, "bootstrap": False, "steps": [], "lock_wait": round(waited, 1),
            "step_secs": [], "timing": timing, "reused": prior["id"], "tree": tree}
