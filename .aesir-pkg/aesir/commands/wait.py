"""wait: Monitor-ready waiter for a process, a log marker or a task's long run (#156). Run it under a Claude Code
Monitor, never in the foreground (docs/supervision.md, rule 11). `--print-monitor` prints the command to arm.
`wait <message>` (block until a reply to an ask, exit 3 on timeout) is the older form and keeps working; it blocks, so
use it only under a Monitor too."""
import shlex
import sys

from .. import db, longrun, rounds, waiter
from ..service import AesirError
from .. import messages
from ._common import add, client_cmd
from .msg import msg_wait


def _task_probe(c, task):
    """probe() for the task's long run: the running one, else its latest (so a run that already ended is reported)."""
    longrun.reap(c)
    row = c.execute("SELECT id FROM long_runs WHERE task_id=? AND status=? ORDER BY id DESC LIMIT 1",
                    (task, longrun.ACTIVE)).fetchone() or \
        c.execute("SELECT id FROM long_runs WHERE task_id=? ORDER BY id DESC LIMIT 1", (task,)).fetchone()
    if not row:
        raise AesirError(f"task {task} has no long run (start one: gate run <profile> --task {task})")
    rid = row["id"]

    def probe():
        longrun.reap(c)
        r = longrun.get(c, rid)
        if r["status"] == longrun.ACTIVE:
            return None
        return ("end" if r["status"] == "passed" else "fail"), f"long run L{rid} {r['status']}"
    return probe


def _monitor_cmd(a):
    parts = ["python -m aesir wait"]
    for flag, v in (("--pid", a.pid), ("--log", a.log), ("--task", a.task)):
        if v is not None:
            parts.append(f"{flag} {shlex.quote(str(v))}")
    for flag, vs in (("--end", a.end), ("--fail", a.fail)):
        parts += [f"{flag} {shlex.quote(v)}" for v in vs or []]
    parts.append(f"--poll {a.poll:g} --timeout {a.timeout:g}")
    return client_cmd(" ".join(parts))


def wait(c, a):
    if a.message is not None:
        if any(x is not None for x in (a.pid, a.log, a.task)) or a.end or a.fail or a.print_monitor:
            raise AesirError("wait <message> takes no other target; use --pid, --log or --task without a message")
        a.timeout = messages.WAIT_TIMEOUT if a.timeout is None else a.timeout
        return msg_wait(c, a)
    if a.timeout is None:
        a.timeout = 900.0
    if sum(x is not None for x in (a.pid, a.log, a.task)) == 0 or (a.task is not None and (a.pid or a.log)):
        raise AesirError("wait needs --pid N, --log PATH (optionally with --pid) or --task N")
    if a.pid is not None and a.log is None and (a.end or a.fail):
        raise AesirError("--end/--fail apply to --log")
    if a.poll <= 0 or a.timeout <= 0:
        raise AesirError("--poll and --timeout must be positive")
    if a.print_monitor:
        print(_monitor_cmd(a))
        print(f"arm with the Claude Code Monitor tool, timeout_ms >= {int((a.timeout + 30) * 1000)} "
              "(it prints one final 'wait: ...' line); re-arm on 'wait: timeout'", file=sys.stderr)
        return
    cfg = rounds.load_config(db.project_root())
    end = a.end or cfg["end_markers"]
    fail = a.fail or cfg["failure_markers"]
    probe = _task_probe(c, a.task) if a.task is not None else None
    line, code = waiter.wait_for(log=a.log, pid=a.pid, probe=probe, end=end, fail=fail, poll=a.poll, timeout=a.timeout)
    print(line, flush=True)
    if code:
        raise SystemExit(code)


def register(sub):
    add(sub, "wait", wait,
        ("message", {"nargs": "?", "help": "M12: block until a reply to this ask arrives (default timeout 600)"}),
        ("--pid", {"type": int, "help": "wait for this process to end"}),
        ("--log", {"help": "wait for an end/failure marker line in this log (with --pid: also stop when it is gone)"}),
        ("--task", {"type": int, "help": "wait for this task's long run to end"}),
        ("--end", {"action": "append", "help": "end-marker regex (repeatable; default [rounds] end_markers)"}),
        ("--fail", {"action": "append", "help": "failure-marker regex (repeatable; default [rounds] failure_markers)"}),
        ("--poll", {"type": float, "default": 5.0, "help": "seconds between checks (default 5)"}),
        ("--timeout", {"type": float, "help": "give up after this many seconds, exit 3 (default 900; 600 for a message)"}),
        ("--print-monitor", {"action": "store_true", "help": "print the command to arm under a Monitor instead of waiting"}))
