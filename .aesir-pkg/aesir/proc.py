"""The one place aesir starts subprocesses (#117). Aesir often runs with no console (hooks, detached gates); a console-less
parent that spawns a console program gives that child its own visible window, so every spawn flashed one. On Windows
these wrappers OR CREATE_NO_WINDOW into creationflags; the child gets a hidden console that its own children inherit,
so a whole step tree (shell, paratest workers, toolkit, cargo, git) stays invisible. Non-interactive children also get
stdin=DEVNULL unless the caller gave stdin or input. Caller flags are kept (OR-ed). DETACHED_PROCESS cannot combine
with CREATE_NO_WINDOW (Windows ignores NO_WINDOW then); a detached caller already has no console, which is the
no-window result, so NO_WINDOW is dropped in that case. Nothing else changes: same args, return values, exceptions.
Everything else in aesir/ calls these, never subprocess.run/Popen directly (tests/test_117_no_console_windows.py scans);
the only exception is aesir/githooks/add_trailer.py, which runs standalone and sets the flag itself."""
import os
import subprocess
import sys

CREATE_NO_WINDOW = 0x08000000  # subprocess.CREATE_NO_WINDOW, which exists only on win32
DETACHED_PROCESS = 0x00000008


def _flags(kw):
    """Fold the no-window flag into kw['creationflags'] (win32 only), in place."""
    if sys.platform == "win32":
        flags = kw.get("creationflags", 0)
        if not flags & DETACHED_PROCESS:
            kw["creationflags"] = flags | CREATE_NO_WINDOW
    return kw


def _stdin(kw, input_ok=False):
    if "stdin" not in kw and not (input_ok and kw.get("input") is not None):
        kw["stdin"] = subprocess.DEVNULL
    return kw


def run(*args, **kw):
    return subprocess.run(*args, **_flags(_stdin(kw, input_ok=True)))


def Popen(*args, **kw):
    return subprocess.Popen(*args, **_flags(_stdin(kw)))


def check_output(*args, **kw):
    return subprocess.check_output(*args, **_flags(_stdin(kw, input_ok=True)))


def spawn_detached(cmd, env, logfile, breakaway_warning=""):
    """Start `cmd` detached from this process, its console and (where the calling tool's job allows) its job, stdout and
    stderr to `logfile`. Returns (Popen, breakaway_refused). Windowless (#117): the child is DETACHED (no console), so its own
    children, spawned through this module, get NO_WINDOW. Used for the long-run supervisor and scheduled-run waiters."""
    err = open(logfile, "wb")
    kw = dict(env=env, stdin=subprocess.DEVNULL, stdout=err, stderr=err, close_fds=True)
    refused = False
    try:
        if os.name == "nt":
            base = DETACHED_PROCESS | 0x00000200  # | CREATE_NEW_PROCESS_GROUP
            try:  # leave a calling tool's kill-on-close job, where the job allows it
                p = Popen(cmd, creationflags=base | 0x01000000, **kw)  # CREATE_BREAKAWAY_FROM_JOB
            except OSError:  # the job forbids breakaway: the child may die with the calling tool
                refused = True
                err.write(f"{breakaway_warning}\n".encode())
                err.flush()
                p = Popen(cmd, creationflags=base, **kw)
        else:
            p = Popen(cmd, start_new_session=True, **kw)
    finally:
        err.close()
    p._child_created = False  # detached on purpose (the child outlives us): no ResourceWarning at collection
    return p, refused
