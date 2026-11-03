"""at <time> -- <cmd> | at list | at cancel <id>: scheduled one-off runs (#139)."""
import subprocess

from .. import sched
from ..service import AesirError
from ._common import out, line, add, client_cmd

USAGE = "usage: at <HH:MM[:SS]Z | YYYY-MM-DDTHH:MM[:SS]Z> -- <command...> | at list | at cancel <id>"


def render(rows):
    """One line per run: id, status, fire time, who armed it, command, exit code once run."""
    for r in rows:
        tail = f"exit {r['exit_code']}" if r["exit_code"] is not None else ""
        line(f"S{r['id']}", f"{r['status']:<9}", r["fire_at"], f"by {r['actor']}", r["command"], tail)
    if not rows:
        print("no scheduled runs")


def at(c, a):
    """Dispatch: `list`, `cancel <id>`, or `<time> -- <command>` (arm)."""
    toks = list(a.args)
    if toks and toks[0] == "list":
        sched.reap(c, a.actor)
        return out(a, sched.listing(c), render)
    if toks and toks[0] == "cancel":
        if len(toks) != 2 or not toks[1].lstrip("S").isdigit():
            raise AesirError(USAGE)
        r = sched.cancel(c, a.actor, int(toks[1].lstrip("S")))
        return out(a, r, lambda r: print(f"cancelled S{r['id']} ({r['fire_at']}: {r['command']})"))
    cmd = toks[1:]  # argparse has already consumed the "--" separator; tolerate one that survived
    if cmd[:1] == ["--"]:
        cmd = cmd[1:]
    if not cmd:
        raise AesirError(USAGE)
    text = cmd[0] if len(cmd) == 1 else subprocess.list2cmdline(cmd)
    r = sched.arm(c, a.actor, sched.parse_when(toks[0]), text)
    out(a, r, lambda r: (print(f"armed S{r['id']}: {r['command']} at {r['fire_at']}"),
                         print(f"  output: {r['log']}"),
                         print("  cancel: " + client_cmd(f"python -m aesir at cancel {r['id']}"))))


def register(sub):
    add(sub, "at", at, ("args", {"nargs": "*", "help": USAGE}))
