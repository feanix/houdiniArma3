"""Monitor-ready waiter (task #156, rule 11 in docs/supervision.md): wait for ONE event and print ONE final line.
Events: a marker line in a log (`[rounds] end_markers` / `failure_markers` in aesir.toml, or --end / --fail), a
process that is gone, a task's long run ending. Every end state is covered (success line, failure line, process
gone, timeout), so a Claude Code Monitor running this never hangs and the session never blocks on it.
Final lines and exit codes:
  wait: end <line>      0   an end marker matched
  wait: fail <line>     1   a failure marker matched (checked before the end marker on the same line)
  wait: gone pid N      0   the process ended and no end marker was required; 1 if one was required and never seen
  wait: timeout <s>     3   nothing happened within the timeout
The log is scanned from its start, so wait on a fresh log (or one whose old markers cannot match)."""
import re
import time

from . import budget

LINE_MAX = 200


def pid_alive(pid):
    """True while `pid` is a live process (Windows and POSIX: budget.process_start)."""
    return budget.process_start(pid) is not None


class LogScan:
    """Incremental scan of a growing log: only complete new lines are looked at; a truncated file restarts."""

    def __init__(self, path, end, fail):
        self.path, self.pos, self.buf = path, 0, b""
        self.end = [re.compile(x) for x in end]
        self.fail = [re.compile(x) for x in fail]

    def poll(self):
        """('fail'|'end', line) for the first marker line in the new data, else None."""
        try:
            with open(self.path, "rb") as f:
                f.seek(0, 2)
                if f.tell() < self.pos:
                    self.pos, self.buf = 0, b""
                f.seek(self.pos)
                data = f.read()
        except OSError:
            return None  # not there yet
        self.pos += len(data)
        self.buf += data
        *lines, self.buf = self.buf.split(b"\n")
        for raw in lines:
            ln = raw.decode("utf-8", "replace").rstrip("\r")
            for kind, pats in (("fail", self.fail), ("end", self.end)):
                if any(p.search(ln) for p in pats):
                    return kind, ln.strip()[:LINE_MAX]
        return None


def wait_for(log=None, pid=None, probe=None, end=(), fail=(), poll=5.0, timeout=900.0,
             alive=pid_alive, clock=time.monotonic, sleep=time.sleep):
    """Wait for the first event; returns (final line, exit code). `probe()` returns None while the awaited thing
    runs, else (kind, text) with kind end|fail (used for a task's long run)."""
    scan = LogScan(log, end, fail) if log else None
    start = clock()
    while True:
        if scan:
            hit = scan.poll()
            if hit:
                return f"wait: {hit[0]} {hit[1]}", 0 if hit[0] == "end" else 1
        if probe:
            hit = probe()
            if hit:
                return f"wait: {hit[0]} {hit[1]}", 0 if hit[0] == "end" else 1
        if pid is not None and not alive(pid):
            if scan and (hit := scan.poll()):  # a marker written just before the process exited still counts
                return f"wait: {hit[0]} {hit[1]}", 0 if hit[0] == "end" else 1
            return f"wait: gone pid {pid}", 1 if scan else 0
        left = timeout - (clock() - start)
        if left <= 0:
            return f"wait: timeout {timeout:g}", 3
        sleep(min(poll, left))
