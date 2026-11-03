"""CPU load generator and staged load test (#144, part of #140). Lets the owner (or a gate timing run) see how the machine
behaves at a known share of its logical cores busy, without risking it.

Safety design:
- MAX_SHARE (75%) is a module constant; `clamp` is applied on every input path (CLI levels, LoadSet.start, the worker
  command line, and again inside the worker), so no flag or config can exceed it.
- Few processes with threads (D61: STATUS_DLL_INIT_FAILED scaled with live process count): at most MAX_PROCS workers.
  Threads hash a fixed 64 KiB buffer (hashlib releases the GIL for large buffers, so threads really use separate cores)
  and are duty-cycled in PERIOD-long periods scheduled on absolute times, so a late timer wake (about 15 ms on
  Windows) shortens the next rest instead of lowering the share (#140). Memory: a few MiB per worker.
- Each worker writes its own process CPU time to a small report file about every 0.1 s; the parent sums them to show the
  generator's own share next to the whole-machine CPU (LoadSet.cpu_seconds).
- Every worker has an absolute deadline, itself clamped to now + MAX_RUN_SECS, and exits on its own when it passes or when
  its parent pid disappears. The parent also terminates workers in finally/atexit/signal handlers.
- Measured CPU: system-wide busy share from GetSystemTimes (Windows, ctypes) or /proc/stat (Linux); stdlib only.
- Responsiveness probe: wall time of `python -c pass` (sys.executable). A stage is "badly slow" when the probe takes more
  than PROBE_SLOW_FACTOR (5) x the baseline probe measured first at 0% load; the test then ends all workers at once and
  runs no further stage."""
import atexit
import contextlib
import hashlib
import os
import shutil
import signal
import tempfile
import sys
import threading
import time

from . import proc

MAX_SHARE = 0.75       # hard cap on the share of logical cores; never configurable
MAX_PROCS = 4          # worker processes (D61: keep few); the rest of the load is threads inside them
MAX_RUN_SECS = 900     # no worker outlives this, whatever deadline it is given
PERIOD = 0.25          # duty-cycle period, seconds; well above the ~15 ms Windows timer granularity
BUF = b"\0" * 65536
DEFAULT_LEVELS = (0.25, 0.50, 0.75)
DEFAULT_STAGE_SECS = 30.0
PROBE_SLOW_FACTOR = 5    # "badly slow": a stage probe over this multiple of the 0%-load baseline probe
PROBE_BASE_FLOOR = 0.05  # seconds: a baseline below this is treated as this, so timer noise cannot trip the guard


def clamp(share):
    """A share in [0, MAX_SHARE]; the one gate every input passes through."""
    return max(0.0, min(float(share), MAX_SHARE))


def ncores():
    return os.cpu_count() or 1


def plan(share, cores=None):
    """(processes, threads per process, duty) holding `share` of `cores` logical cores: busy-thread-equivalents
    = share x cores, split over at most MAX_PROCS processes; duty is each thread's busy fraction of PERIOD."""
    share, cores = clamp(share), cores or ncores()
    if share <= 0:
        return 0, 0, 0.0
    need = share * cores
    threads = max(1, -(-int(need * 1000) // 1000))  # ceil(need)
    threads = min(threads, cores)
    procs = min(MAX_PROCS, threads)
    per = -(-threads // procs)
    duty = min(1.0, need / (procs * per))
    return procs, per, duty


# ---------------------------------------------------------------- worker side

def _parent_alive(pid):
    from . import gates
    return gates.pid_alive(pid)


def _burn(stop, duty):
    h = hashlib.sha256
    busy = PERIOD * duty
    start = time.perf_counter()
    while not stop.is_set():
        end = start + busy
        while time.perf_counter() < end and not stop.is_set():
            h(BUF).digest()
        start += PERIOD  # absolute schedule: oversleeping once is made up by a shorter rest next time
        rest = start - time.perf_counter()
        if rest > 0:
            stop.wait(rest)
        elif rest < -PERIOD:  # fell far behind (machine stalled): restart the schedule, do not burst
            start = time.perf_counter()


def _report(path):
    try:
        with open(path, "w") as fh:
            fh.write(repr(time.process_time()))
    except OSError:
        pass


def worker_main(share, threads, deadline, parent_pid, cores=None, report=None):
    """Run in a worker process: busy threads until the deadline or the parent dies, then exit."""
    deadline = min(float(deadline), time.time() + MAX_RUN_SECS)
    _, _, duty = plan(clamp(share), cores)
    stop = threading.Event()
    for _ in range(max(0, min(int(threads), ncores()))):
        threading.Thread(target=_burn, args=(stop, duty), daemon=True).start()
    while time.time() < deadline and _parent_alive(parent_pid):
        if report:
            _report(report)
        time.sleep(0.1)
    stop.set()
    os._exit(0)


# ---------------------------------------------------------------- parent side

class LoadSet:
    """The worker processes for one load level. Use as a context manager; stop() is idempotent."""

    def __init__(self):
        self.procs = []
        self.dir = None
        atexit.register(self.stop)

    def start(self, share, secs, cores=None):
        """Start workers holding `share` (clamped) for at most `secs` seconds (also capped by MAX_RUN_SECS)."""
        self.stop()
        share = clamp(share)
        n, per, _ = plan(share, cores)
        deadline = time.time() + max(0.0, min(float(secs), MAX_RUN_SECS))
        self.dir = tempfile.mkdtemp(prefix="aesir-load-")
        for i in range(n):
            self.procs.append(proc.Popen(
                [sys.executable, "-m", "aesir.load", "worker", repr(share), str(per), repr(deadline), str(os.getpid())]
                + ([str(cores)] if cores else []) + ["--report=" + os.path.join(self.dir, "w%d" % i)],
                stdout=proc.subprocess.DEVNULL, stderr=proc.subprocess.DEVNULL))
        return self.procs

    def cpu_seconds(self):
        """Process CPU seconds of the current workers, summed from their report files (0 when none yet)."""
        total = 0.0
        for i in range(len(self.procs)):
            try:
                with open(os.path.join(self.dir, "w%d" % i)) as fh:
                    total += float(fh.read())
            except (OSError, ValueError, TypeError):
                pass
        return total

    def stop(self):
        """Terminate every worker (kill if it ignores that for 5 s) and remove the report dir; safe to call repeatedly."""
        procs, self.procs = self.procs, []
        d, self.dir = self.dir, None
        gone = (OSError, proc.subprocess.TimeoutExpired)
        for p in procs:
            with contextlib.suppress(*gone):
                if p.poll() is None:
                    p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except gone:
                with contextlib.suppress(*gone):
                    p.kill()
                    p.wait(timeout=5)
        if d:
            shutil.rmtree(d, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()


# ---------------------------------------------------------------- measurement

def cpu_times():
    """(busy, total) cumulative system CPU time, any unit; differences give the busy share."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        idle, kern, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
        if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user)):
            raise OSError("GetSystemTimes failed")
        f = lambda t: (t.dwHighDateTime << 32) | t.dwLowDateTime
        total = f(kern) + f(user)  # kernel time includes idle
        return total - f(idle), total
    with open("/proc/stat") as fh:
        v = [int(x) for x in fh.readline().split()[1:]]
    idle = v[3] + (v[4] if len(v) > 4 else 0)
    total = sum(v[:8])
    return total - idle, total


def busy_share(a, b):
    """Busy share between two cpu_times() samples."""
    dt = b[1] - a[1]
    return (b[0] - a[0]) / dt if dt > 0 else 0.0


def probe():
    """Seconds for a tiny python start."""
    t = time.perf_counter()
    proc.run([sys.executable, "-c", "pass"], capture_output=True)
    return time.perf_counter() - t


def gen_cpu(ls):
    """Worker CPU seconds so far for a LoadSet (0 for anything without cpu_seconds, e.g. a test double)."""
    f = getattr(ls, "cpu_seconds", None)
    v = f() if callable(f) else 0.0
    return v if isinstance(v, (int, float)) else 0.0


def too_slow(secs, baseline):
    return secs > PROBE_SLOW_FACTOR * max(baseline, PROBE_BASE_FLOOR)


@contextlib.contextmanager
def raise_on_signals():
    """SIGINT/SIGTERM/SIGBREAK raise KeyboardInterrupt for the duration, so finally blocks stop children and workers."""
    old = {}

    def on_signal(signum, frame):
        raise KeyboardInterrupt

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                old[sig] = signal.signal(sig, on_signal)
            except ValueError:  # not the main thread
                pass
    try:
        yield
    finally:
        for sig, h in old.items():
            try:
                signal.signal(sig, h)
            except (ValueError, OSError):
                pass


def run_load_test(levels=DEFAULT_LEVELS, stage_secs=DEFAULT_STAGE_SECS, say=print, cores=None,
                  measure=cpu_times, probe_fn=probe, loadset=None):
    """Raise load through `levels` (each clamped to MAX_SHARE), stage_secs each. Returns a dict: baseline probe, stages
    (level, measured cpu share, probe seconds, slow), stopped (None or the level where it stopped early). Load workers
    are always stopped on the way out (normal end, early stop, exception, Ctrl-C)."""
    ls = loadset or LoadSet()
    with raise_on_signals():
        return _load_test(ls, levels, stage_secs, say, cores, measure, probe_fn)


def _load_test(ls, levels, stage_secs, say, cores, measure, probe_fn):
    res = {"cores": cores or ncores(), "max_share": MAX_SHARE, "stage_secs": stage_secs, "baseline_probe": None,
           "stages": [], "stopped": None}
    try:
        res["baseline_probe"] = base = probe_fn()
        say(f"{res['cores']} logical cores; cap {MAX_SHARE:.0%}; idle probe at 0% load {base:.2f}s (stop if a stage probe exceeds {PROBE_SLOW_FACTOR}x)")
        for lvl in levels:
            lvl = clamp(lvl)
            ls.start(lvl, stage_secs + 10, cores)
            try:
                a, g0, t0 = measure(), gen_cpu(ls), time.perf_counter()
                time.sleep(stage_secs / 2)
                p = probe_fn()
                time.sleep(stage_secs / 2)
                share, wall = busy_share(a, measure()), time.perf_counter() - t0
                gen = max(0.0, gen_cpu(ls) - g0) / (wall * res["cores"])
            finally:
                ls.stop()
            slow = too_slow(p, base)
            res["stages"].append({"level": lvl, "cpu": share, "generator": gen, "probe": p, "slow": slow})
            say(f"stage {lvl:.0%}: measured cpu {share:.0%} (generator {gen:.0%}), probe {p:.2f}s{'  SLOW' if slow else ''}")
            if slow:
                res["stopped"] = lvl
                say(f"stopped early: probe {p:.2f}s over {PROBE_SLOW_FACTOR}x the 0% baseline {base:.2f}s")
                break
    finally:
        ls.stop()
    return res


if __name__ == "__main__":
    if len(sys.argv) >= 6 and sys.argv[1] == "worker":
        pos = [a for a in sys.argv[2:] if not a.startswith("--report=")]
        rep_ = [a[len("--report="):] for a in sys.argv[2:] if a.startswith("--report=")]
        worker_main(float(pos[0]), int(pos[1]), float(pos[2]), int(pos[3]),
                    int(pos[4]) if len(pos) > 4 else None, rep_[0] if rep_ else None)
