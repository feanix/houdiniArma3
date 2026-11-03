"""Observe/drive enforcement (task #30): [observe] in aesir.toml, the UI-task criteria lint, owner-sensor reports.

aesir.toml [observe]: digest, headless (required), menu, realworld, window, guide (optional; realworld = command that proves the real-world gate, see docs/observe-drive.md; window = real-window cmd, must carry a no-focus flag). Each command entry is
  - a command string (must run and exit 0 for `aesir observe check`),
  - { cmd = "...", pending_until = <task id> } (bootstrap: warns while that task is unmerged, fails once it is
    merged and the entry is still marked pending),
  - "n/a" (declared not applicable; document why in a comment).
A task flagged ui (tasks.extra 'ui': true) needs criteria that mention 'digest' or 'headless' to enter in_review.
"""
import json
import re
import subprocess

from . import proc
from . import gates
from .db import event, now, tx
from .service import AesirError, get_task

REQUIRED = ("digest", "headless")
OPTIONAL = ("menu", "realworld")
NA = "n/a"
CHECK_TIMEOUT = 60
NO_FOCUS = re.compile(r"no[-_]?activate|no[-_]?focus|minimi[sz]ed|offscreen|off[-_]screen|headless|hidden", re.I)
UI_WORDS = re.compile(r"digest|headless", re.I)


def load(root) -> dict:
    """Validated [observe] section ({} when absent)."""
    try:
        sec = gates.load_config(root).get("observe", {})
    except AesirError as e:
        if "no aesir.toml" not in str(e):
            raise
        return {}
    if not isinstance(sec, dict):
        raise AesirError("bad aesir.toml: [observe] must be a table")
    for k in (*REQUIRED, *OPTIONAL):
        v = sec.get(k)
        if v is None:
            continue
        if isinstance(v, dict):
            p = v.get("pending_until")
            if not isinstance(v.get("cmd"), str) or not v["cmd"].strip() or \
                    (p is not None and (not isinstance(p, int) or isinstance(p, bool))):
                raise AesirError(f"bad aesir.toml: [observe] {k} table needs cmd (string), optional pending_until (task id)")
        elif not isinstance(v, str) or not v.strip():
            raise AesirError(f"bad aesir.toml: [observe] {k} must be a command string, a table, or '{NA}'")
    w = sec.get("window")
    if w is not None and (not isinstance(w, str) or not w.strip()):
        raise AesirError("bad aesir.toml: [observe] window must be a command string")
    if "guide" in sec and not isinstance(sec["guide"], str):
        raise AesirError("bad aesir.toml: [observe] guide must be a path string")
    return sec


def _cmd(v):
    """(command or None, pending task id or None) of one entry; 'n/a' gives (None, None)."""
    if isinstance(v, dict):
        return v["cmd"], v.get("pending_until")
    return (None, None) if v.strip() == NA else (v, None)


def context_lines(root) -> list:
    """Lines for an implementer/reviewer/researcher's start context; [] when nothing is configured."""
    sec = load(root)
    if not sec:
        return []
    lines = ["Observe (verify UI/behaviour criteria from this output, never 'looks right'):"]
    for k in (*REQUIRED, *OPTIONAL):
        if k in sec:
            cmd, pend = _cmd(sec[k])
            lines.append(f"  {k}: {cmd or NA}" + (f" (pending until #{pend:03d})" if pend else ""))
    if sec.get("guide"):
        lines.append(f"  guide: {sec['guide']}")
    return lines


def _merged(conn, tid) -> bool:
    r = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
    return bool(r) and r["status"] in ("merged", "verified")


def check(conn, root, run=proc.run):
    """[(key, 'ok'|'warn'|'fail', message)] for each required entry (and menu when set)."""
    sec = load(root)
    res = []
    for k in (*REQUIRED, *[k for k in OPTIONAL if k in sec]):
        if k not in sec:
            res.append((k, "fail", "missing from [observe]"))
            continue
        cmd, pend = _cmd(sec[k])
        if cmd is None:
            res.append((k, "ok", "declared n/a"))
        elif pend and not _merged(conn, pend):
            res.append((k, "warn", f"pending until #{pend:03d} (not merged yet): {cmd}"))
        elif pend:
            res.append((k, "fail", f"#{pend:03d} is merged but the entry is still pending: drop pending_until"))
        else:
            try:
                r = run(cmd, shell=True, cwd=str(root), capture_output=True, text=True, timeout=CHECK_TIMEOUT)
                res.append((k, "ok", cmd) if r.returncode == 0 else
                           (k, "fail", f"exit {r.returncode}: {cmd}\n{(r.stderr or r.stdout or '').strip()[-500:]}"))
            except subprocess.TimeoutExpired:
                res.append((k, "fail", f"timed out after {CHECK_TIMEOUT}s: {cmd}"))
    w = sec.get("window")
    if w and w.strip() != NA:  # real-window command: checked statically, never run (it would open a window)
        res.append(("window", "ok", w) if NO_FOCUS.search(w) else
                   ("window", "fail", f"real-window cmd has no no-focus flag (no-activate/minimized/offscreen): {w}"))
    return res


# ---- UI-flagged tasks ---------------------------------------------------------------

def is_ui(t) -> bool:
    try:
        return bool(json.loads(t.get("extra") or "{}").get("ui"))
    except ValueError:
        return False


def set_ui(conn, actor, tid, on=True):
    with tx(conn):
        t = get_task(conn, tid)
        try:
            extra = json.loads(t["extra"] or "{}")
        except ValueError:
            extra = {}
        old = bool(extra.get("ui"))
        if on:
            extra["ui"] = True
        else:
            extra.pop("ui", None)
        conn.execute("UPDATE tasks SET extra=?, updated=? WHERE id=?", (json.dumps(extra), now(), int(tid)))
        event(conn, actor, "task", tid, "set", {"ui": {"from": old, "to": bool(on)}})


def lint_ui(t):
    """Raise unless a ui-flagged task's criteria mention a digest question or headless scenario."""
    if is_ui(t) and not UI_WORDS.search(t["spec"] or ""):
        raise AesirError(f"task {t['id']} is a UI/behaviour task: its criteria must reference a digest question or "
                         f"headless scenario (mention 'digest' or 'headless'); lead can override with "
                         f"--force-gate-skip --reason")


def owner_sensor(conn, actor, tid, note=""):
    with tx(conn):
        get_task(conn, tid)
        event(conn, actor, "task", tid, "owner-sensor", {"note": note})
