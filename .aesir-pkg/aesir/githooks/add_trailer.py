"""prepare-commit-msg helper: python add_trailer.py <msg-file> [source] [sha]. Never fails a commit."""
import os
import subprocess
import sys
from pathlib import Path


def _run(*a, **kw):
    """subprocess.run without a console window (#117). Standalone script: cannot import aesir.proc before sys.path is set."""
    if sys.platform == "win32":
        kw["creationflags"] = kw.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
    return subprocess.run(*a, **kw)


def lookup(cwd, env=os.environ):
    """(agent_id, model) for this commit, or None. AESIR_AGENT first, else the bound agent owning this worktree."""
    agent, model = env.get("AESIR_AGENT", "").strip(), env.get("AESIR_MODEL", "").strip()
    if agent and model:
        return agent, model
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    try:
        from aesir import db, service
        path = db.db_path(start=cwd)
        if not path.exists():
            return (agent, model) if agent else None
        conn = db.connect(path)
        try:
            if agent:
                row = conn.execute("SELECT model FROM agents WHERE agent_id=? ORDER BY id DESC LIMIT 1",
                                   (agent,)).fetchone()
                return agent, model or (row[0] if row else "")
            row = service.agent_for_worktree(conn, cwd)
            return (row["agent_id"], row["model"]) if row else None
        finally:
            conn.close()
    except Exception:
        return (agent, model) if agent else None


def trailer(found):
    agent, model = found
    return f"Aesir-Agent: {agent} ({model})" if model else f"Aesir-Agent: {agent}"


def rebasing(cwd):
    """True while a rebase is in progress (git-dir is per worktree, so this works in linked worktrees)."""
    try:
        out = _run(["git", "rev-parse", "--git-dir"], cwd=cwd, capture_output=True, text=True).stdout.strip()
        gd = Path(cwd) / out
        return (gd / "rebase-merge").exists() or (gd / "rebase-apply").exists()
    except Exception:
        return False


def has_trailer(msg_file):
    try:
        return any(l.startswith("Aesir-Agent:") for l in Path(msg_file).read_text(encoding="utf-8").splitlines())
    except Exception:
        return False


def main(argv):
    if len(argv) < 2:
        return 0
    source = argv[2] if len(argv) > 2 else ""
    existing = has_trailer(argv[1])
    # Amend / rebase reuse ('commit') and rebase picks never replace an original author's trailer.
    if existing and source == "commit":
        return 0
    found = lookup(os.getcwd())
    if not found:
        return 0
    if existing or (rebasing(os.getcwd()) and source != "merge"):
        if existing:
            extra = ["--trailer", f"Aesir-Rebased-By: {found[0]}"] if rebasing(os.getcwd()) else []
            if extra:
                _run(["git", "interpret-trailers", "--in-place", "--if-exists", "replace", *extra,
                      argv[1]], check=False)
            return 0
    _run(["git", "interpret-trailers", "--in-place", "--if-exists", "doNothing",
          "--trailer", trailer(found), argv[1]], check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
