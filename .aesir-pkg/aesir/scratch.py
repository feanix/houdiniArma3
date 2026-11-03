"""#138: per-agent scratch folder, so scratch output has a home that is neither bare /tmp (shared, T65) nor the main
checkout (a stray file there dirties the tree and blocks every gate).

Form: <system temp>/aesir/<project name>/<agent id>/. Created at SubagentStart and lazily by `spawn-context`.
Removed at SubagentStop when the agent succeeded; kept (marked with .aesir-kept) when it failed, so the lead can
look, and swept once older than [scratch] keep_failed_hours (default 24) by the next spawn/stop. Everything here is
best-effort: callers wrap it so a problem never blocks the agent (D115)."""
import os
import shutil
import tempfile
import time
from pathlib import Path

from . import gates
from .service import AesirError

KEEP_HOURS = 24
MARK = ".aesir-kept"


def base(root):
    """AESIR_SCRATCH_ROOT (tests) replaces <system temp>/aesir."""
    top = os.environ.get("AESIR_SCRATCH_ROOT") or Path(tempfile.gettempdir()) / "aesir"
    return Path(top) / gates.project_name(root)


def path(root, agent_id):
    return base(root) / agent_id


def ensure(root, agent_id):
    """Create (idempotent) and return the agent's folder."""
    p = path(root, agent_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


def keep_hours(root):
    """[scratch] keep_failed_hours from aesir.toml (hours a failed agent's folder is kept); default 24 when absent/bad."""
    try:
        v = gates.load_config(root).get("scratch", {}).get("keep_failed_hours", KEEP_HOURS)
    except (AesirError, OSError, ValueError, AttributeError):
        return KEEP_HOURS
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else KEEP_HOURS


def succeeded(last_message):
    """What the hook knows: SubagentStop carries no exit status, only the final message. An agent that reached its
    required report ending (a `deviation:` line, see the agent defs) succeeded; none (empty, cut off, crashed) did not."""
    return any(ln.strip().lower().lstrip("`*- ").startswith("deviation:") for ln in (last_message or "").splitlines())


def finish(root, agent_id, ok):
    """Agent ended: delete the folder on success (or when empty); on failure keep it and stamp the keep time."""
    p = path(root, agent_id)
    if not p.is_dir():
        return
    if ok or not any(x.name != MARK for x in p.iterdir()):
        shutil.rmtree(p, ignore_errors=True)
        return
    (p / MARK).write_text(str(int(time.time())), encoding="utf-8")


def sweep(root, running=()):
    """Remove kept (failed) folders past the keep time, and any other folder untouched that long whose agent is not
    running (an agent that never stopped). Returns the removed ids."""
    cutoff = time.time() - keep_hours(root) * 3600
    gone = []
    b = base(root)
    if not b.is_dir():
        return gone
    for d in b.iterdir():
        if not d.is_dir() or d.name in running:
            continue
        try:
            m = d / MARK
            if (m if m.exists() else d).stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                gone.append(d.name)
        except OSError:
            continue
    return gone


def context_line(p, hours=KEEP_HOURS):
    """The spawn-context line naming the folder `p` and the rule for it."""
    return (f"Scratch folder: {p} -- put scratch files, logs and temp scripts here. Not bare /tmp (shared with other "
            "sessions) and not the main checkout (a stray file there blocks every gate). It is deleted when you finish "
            f"cleanly and kept {hours:g}h if you fail.")
