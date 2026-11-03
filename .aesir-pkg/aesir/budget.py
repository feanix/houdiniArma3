"""Machine-wide gate admission and worker budget (task #104), stdlib only.
Several gates may run at once (different projects, different worktrees); what they share is a budget of test workers,
because the failure seen under load (STATUS_DLL_INIT_FAILED, D61) tracks the number of live processes, not CPU.
State lives in the gate dir (AESIR_GATE_DIR, else beside AESIR_GATE_LOCK, else the temp dir):
  aesir-budget.lock  short OS lock guarding the state file (never deleted: a killed holder drops it with its handle)
  aesir-budget.json  {"leases": [...]}: one entry per running gate (kind gate, n 0) and per worker lease (kind workers)
A lease records pid + process creation time; an entry whose process is gone, or whose pid was reused, is dropped on
the next access, so a crash leaks nothing. Settings come from aesir.toml [machine]: workers (total budget B, default
min(cpu-2, 24)), max_gates (default 3), legacy_lock (default true, see gates.gate_lock)."""
import contextlib
import json
import os
import subprocess
import tempfile
import time
import tomllib
import uuid
from pathlib import Path
from . import proc

LOCK_OFFSET = 1 << 20  # OS-locked byte, far past any label text: Windows byte-range locks are mandatory for readers
GATE_DIR_ENV = "GATE_BUDGET_DIR"  # not AESIR_-prefixed: gate steps run with AESIR_* stripped but must share the dir
MAX_WORKERS = 24
MAX_GATES = 3
MERGE_MIN = 2  # a merge-lane gate needs at least this many workers free to start (waits otherwise)
STATE_WAIT = 30  # seconds to get the short state lock before giving up


class BudgetBusy(Exception):
    """Admission or a lease did not come within the wait."""


# lane -> how a lease behaves. The long lane (#106) plugs in here: starts with whatever is free (min 1), never waits,
# capped at half the budget. No command uses it yet.
LANES = {
    "merge": {"min": MERGE_MIN, "waits": True, "cap": lambda total: total},
    "long": {"min": 1, "waits": False, "cap": lambda total: max(1, total // 2)},
}


def os_lock(fd, lock):
    """Non-blocking OS lock on byte LOCK_OFFSET of an open file; False if another holder has it."""
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK if lock else msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, (fcntl.LOCK_EX | fcntl.LOCK_NB) if lock else fcntl.LOCK_UN)
    except OSError:
        return False
    return True


def gate_dir():
    d = os.environ.get("AESIR_GATE_DIR") or os.environ.get(GATE_DIR_ENV)
    if d:
        return Path(d)
    lock = os.environ.get("AESIR_GATE_LOCK")
    return Path(lock).parent if lock else Path(tempfile.gettempdir())


def settings(root=None):
    """{'workers': B, 'max_gates', 'legacy_lock'} from <root>/aesir.toml [machine]; env AESIR_LEGACY_LOCK overrides."""
    m = {}
    if root:
        try:
            m = tomllib.loads((Path(root) / "aesir.toml").read_text(encoding="utf-8")).get("machine", {})
        except (OSError, tomllib.TOMLDecodeError):
            m = {}
    legacy = m.get("legacy_lock", True)
    env = os.environ.get("AESIR_LEGACY_LOCK")
    if env:
        legacy = env.strip().lower() not in ("0", "false", "no", "off")
    return {"workers": max(MERGE_MIN, int(m.get("workers") or default_budget())),
            "max_gates": max(1, int(m.get("max_gates", MAX_GATES))), "legacy_lock": bool(legacy)}


def default_budget():
    return min((os.cpu_count() or 4) - 2, MAX_WORKERS)


def process_start(pid):
    """An opaque token for when `pid` started (None if no such live process). Same token = same process: guards pid reuse."""
    pid = int(pid)
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes as w
        k = ctypes.windll.kernel32
        k.OpenProcess.restype = w.HANDLE
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return None
        try:
            code = w.DWORD()
            if not k.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != 259:  # STILL_ACTIVE
                return None
            c, e, kt, ut = (w.FILETIME() for _ in range(4))
            if not k.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(kt), ctypes.byref(ut)):
                return None
            return (c.dwHighDateTime << 32) | c.dwLowDateTime
        finally:
            k.CloseHandle(h)
    try:  # Linux: field 22 of /proc/<pid>/stat (clock ticks since boot), parsed after the ')' closing the comm field
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        pass
    r = proc.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True)
    return r.stdout.strip() or None if r.returncode == 0 else None


def count_python():
    """Live python processes on the machine (0 if it cannot be counted). Replaced in tests."""
    try:
        if os.name == "nt":
            r = proc.run(["tasklist", "/FI", "IMAGENAME eq python.exe", "/NH"], capture_output=True, text=True,
                               timeout=10)
            return sum(1 for ln in r.stdout.splitlines() if ln.lower().startswith("python"))
        r = proc.run(["ps", "-A", "-o", "comm="], capture_output=True, text=True, timeout=10)
        return sum(1 for ln in r.stdout.splitlines() if ln.strip().rsplit("/", 1)[-1].startswith("python"))
    except (OSError, subprocess.SubprocessError):
        return 0


def _alive(e):
    return process_start(e["pid"]) == e["start"]


@contextlib.contextmanager
def _state(d=None):
    """The state under the short OS lock: dead entries already dropped; saved on a normal exit of the block.
    `d`: the gate dir; holders pin it at start, because importing the test sandbox later moves AESIR_GATE_DIR."""
    d = d or gate_dir()
    d.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(d / "aesir-budget.lock"), os.O_RDWR | os.O_CREAT)
    deadline = time.monotonic() + STATE_WAIT
    try:
        while not os_lock(fd, True):
            if time.monotonic() >= deadline:
                raise BudgetBusy("budget state lock stayed held")
            time.sleep(0.02)
        try:
            path = d / "aesir-budget.json"
            try:
                st = json.loads(path.read_text(encoding="utf-8"))
                leases = st["leases"] if isinstance(st.get("leases"), list) else []
            except (OSError, ValueError, AttributeError):
                leases = []
            st = {"leases": [e for e in leases if isinstance(e, dict) and _alive(e)]}
            yield st
            path.write_text(json.dumps(st), encoding="utf-8")
        finally:
            os_lock(fd, False)
    finally:
        os.close(fd)


def snapshot():
    """Current live entries (for status and tests)."""
    with _state() as st:
        return list(st["leases"])


def _used(st):
    return sum(e["n"] for e in st["leases"])


def holds_gate(pid):
    """True if `pid` is a live process with a registered gate (a nested gate under it needs no admission)."""
    with _state() as st:
        return any(e["kind"] == "gate" and e["pid"] == int(pid) for e in st["leases"])


def _entry(kind, n, lane, label):
    pid = os.getpid()
    return {"id": uuid.uuid4().hex, "pid": pid, "start": process_start(pid), "kind": kind, "n": n, "lane": lane,
            "label": label, "ts": int(time.time())}


def _drop(entry, d):
    with _state(d) as st:
        st["leases"] = [e for e in st["leases"] if e["id"] != entry["id"]]


@contextlib.contextmanager
def gate_slot(label, cfg, deadline=None, poll=0.5, say=print):
    """Admit one gate: fewer than max_gates running and at least MERGE_MIN workers unleased. Raises BudgetBusy at deadline."""
    told, d = None, gate_dir()
    while True:
        with _state(d) as st:
            running = [e for e in st["leases"] if e["kind"] == "gate"]
            free = cfg["workers"] - _used(st)
            if len(running) < cfg["max_gates"] and free >= MERGE_MIN:
                entry = _entry("gate", 0, "merge", label)
                st["leases"].append(entry)
                break
            why = (f"{len(running)} of {cfg['max_gates']} gates running" if len(running) >= cfg["max_gates"]
                   else f"only {free} of {cfg['workers']} workers free")
        if deadline is not None and time.monotonic() >= deadline:
            raise BudgetBusy(f"gate budget busy: {why}")
        if why != told:
            told = why
            say(f"waiting for gate budget: {why}")
        time.sleep(poll)
    try:
        yield entry
    finally:
        _drop(entry, d)


def grant(st, cfg, want, lane, python_procs=0):
    """Workers to lease now (0 = not yet). Fair share between gates that started together, keeping MERGE_MIN per empty slot."""
    spec = LANES[lane]
    free = cfg["workers"] - _used(st)
    leases = [e for e in st["leases"] if e["kind"] == "workers"]
    if not leases:  # alone: stray processes may shrink the grant but never below the minimum
        free = max(free - max(0, python_procs - 4), spec["min"])
    else:
        used = _used(st)
        free -= max(0, python_procs - 3 * used - 2 * len(st["leases"]) - 4)
    if not spec["waits"]:
        return max(1, min(want, spec["cap"](cfg["workers"]), free))
    gates = [e for e in st["leases"] if e["kind"] == "gate"]
    merge = [e for e in leases if e["lane"] == "merge"]
    unleased = max(1, len(gates) - len(merge))
    reserve = spec["min"] * max(0, cfg["max_gates"] - (len(merge) + unleased))
    floor = min(spec["min"], want)
    if free < floor:
        return floor if not leases else 0  # alone: never wait for what no one holds
    return max(floor, min(want, (free - reserve) // unleased, spec["cap"](cfg["workers"])))


@contextlib.contextmanager
def lease(want, root=None, lane="merge", wait=120, poll=1.0, say=print):
    """Lease up to `want` workers from the machine budget for the duration of the block; yields the number granted.
    The merge lane waits (up to `wait` s) for MERGE_MIN to be free, then proceeds with MERGE_MIN rather than failing a
    gate for lack of a slot. The long lane never waits."""
    cfg, d = settings(root), gate_dir()
    deadline, told, n = time.monotonic() + wait, None, 0
    while True:
        procs = count_python()  # outside the state lock: listing processes is slow
        with _state(d) as st:
            n = grant(st, cfg, want, lane, procs)
            if not n and time.monotonic() >= deadline:
                n = min(LANES[lane]["min"], want)
                say(f"worker budget still full after {wait}s: running with {n}")
            if n:
                entry = _entry("workers", n, lane, "")
                st["leases"].append(entry)
                break
        if told is None:
            told = True
            say("waiting for worker budget")
        time.sleep(poll)
    try:
        yield n
    finally:
        _drop(entry, d)
