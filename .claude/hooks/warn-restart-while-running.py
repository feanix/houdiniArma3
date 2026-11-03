"""Restart guard: no lead or advisor restart while Workflow builds are running.

A Workflow lives inside the session that started it, so restarting that session kills its builds mid-run
(Voxel Terrarium lost two builds this way on 2026-10-15). Claude Code locks each Workflow worktree with the owning
session's pid; a locked worktree whose pid is alive is a running build. A lock whose pid is gone is reported too,
as stale, so a crash errs towards a warning, never a false all-clear.

Two hook events, one rule:
- Stop: holds back a reply that asks the owner for a restart while builds run.
- PreToolUse (Bash/PowerShell): refuses running the restart script (restart-session.ps1 or
  armaHou-restart-<role>.bat) while builds run, since armaHou sessions may restart themselves. A -DryRun passes.
It checks every live build in the project, not only the restarting session's: it errs towards waiting.

Adapted from VoxelTerrarium/.claude/hooks/warn-restart-while-running.py (Stop part); the PreToolUse part is new.
"""
import ctypes
import json
import re
import subprocess
import sys

# A request or go for a restart, not a mention: replies that only describe this guard or an incident named
# "restart" next to "session" and were held back for nothing.
RESTART = re.compile(
    r"\b(please|go ahead and|time to|ready (for|to)|safe to|ask(ing)? (for|you to)|"
    r"can (you|we)|should|let's|you can)\b[^.?!\n]{0,40}\b(restart|fresh session|new session)"
    r"|\brestart (now|both|the lead|the advisor|this session|your session|my session|me|us)\b",
    re.IGNORECASE,
)
# Sentences about the guard itself are reports, never requests.
ABOUT_GUARD = re.compile(r"\bhook\b|\bguard\b|aesir-reports", re.IGNORECASE)
# A sentence that holds the restart back already says what the guard wants said.
HOLDS_BACK = re.compile(r"\b(not|isn't|is not|never|no)\s+safe\b|\buntil\b|\bafter\b.*\b(ends?|finish\w*)", re.IGNORECASE)
# Quoted phrases are examples of a request, not the request itself.
QUOTED = re.compile(r"\"[^\"\n]*\"|“[^”\n]*”|`[^`\n]*`")
# The restart launchers in E:\Projects\launch.
RESTART_CMD = re.compile(r"restart-session\.ps1|restart-(lead|advisor)\.bat", re.IGNORECASE)
DRY_RUN = re.compile(r"-DryRun\b", re.IGNORECASE)
LOCK = re.compile(r"\(pid (\d+)\)")


def asks_restart(text):
    sentences = re.split(r"(?<=[.?!])\s+|\n", QUOTED.sub("", text))
    return any(
        RESTART.search(s) and not ABOUT_GUARD.search(s) and not HOLDS_BACK.search(s) for s in sentences
    )


def pid_alive(pid):
    # os.kill(pid, 0) would terminate the process on Windows, so ask the kernel instead.
    still_active = 259
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    code = ctypes.c_ulong()
    ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(handle)
    return code.value == still_active


def last_reply(payload):
    if payload.get("last_assistant_message"):
        return str(payload["last_assistant_message"])
    texts = []
    try:
        with open(payload["transcript_path"], encoding="utf-8") as f:
            lines = f.readlines()
    except (KeyError, OSError):
        return ""
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("type") == "user" and not _is_tool_result(entry):
            break
        if entry.get("type") == "assistant":
            for block in entry.get("message", {}).get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(block.get("text", ""))
    return "\n".join(reversed(texts))


def _is_tool_result(entry):
    content = entry.get("message", {}).get("content")
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    )


def locked_builds(root):
    out = subprocess.run(
        ["git", "-C", root, "worktree", "list", "--porcelain"],
        capture_output=True, text=True, check=False,
    ).stdout
    builds = []
    path = ""
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line.startswith("locked") and "/.claude/worktrees/wf_" in path.replace("\\", "/"):
            m = LOCK.search(line)
            alive = bool(m) and pid_alive(int(m.group(1)))
            builds.append((path.rsplit("/", 1)[-1], "running" if alive else "stale lock"))
    return builds


def listing(builds):
    return ", ".join(f"{name} ({state})" for name, state in builds)


STALE_NOTE = "A 'stale lock' means the owning session is gone: check, then remove the worktree."

try:
    payload = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
event = payload.get("hook_event_name", "")
root = payload.get("cwd") or "."

if event == "PreToolUse":
    command = str((payload.get("tool_input") or {}).get("command", ""))
    if not RESTART_CMD.search(command) or DRY_RUN.search(command):
        sys.exit(0)
    builds = locked_builds(root)
    if not builds:
        sys.exit(0)
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"Workflow builds are live: {listing(builds)}. A restart kills the builds of the session it "
                f"restarts. Wait until they end, then restart. {STALE_NOTE}"
            ),
        }
    }))
    sys.exit(0)

# Stop. A second stop after a warning goes through, so the guard can never loop.
if payload.get("stop_hook_active"):
    sys.exit(0)
if not asks_restart(last_reply(payload)):
    sys.exit(0)
builds = locked_builds(root)
if not builds:
    sys.exit(0)
print(json.dumps({
    "decision": "block",
    "reason": (
        f"Workflow builds are live: {listing(builds)}. Restarting the session that started them kills them. "
        "Do not ask for a lead or advisor restart now: say the restart waits until these builds end, and "
        f"supervise until then. {STALE_NOTE} If your reply did not ask for a restart, end the turn unchanged."
    ),
}))
