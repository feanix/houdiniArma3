"""Install the Aesir git hooks (commit trailer naming the agent)."""
import shutil
import sys
from pathlib import Path

from . import proc
from .service import AesirError

SRC = Path(__file__).resolve().parent / "githooks"
FILES = ("prepare-commit-msg", "add_trailer.py")


def _git(root, *args):
    r = proc.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if r.returncode:
        raise AesirError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def install(root, copy=False):
    """Point core.hooksPath at the shipped hooks (default) or copy them into the repo's hooks dir."""
    _git(root, "config", "aesir.python", Path(sys.executable).as_posix())
    # Task subjects start with '#NNN'. Git strips '#' lines when it re-edits a message (rebase --continue after a
    # conflict, reword), eating the subject; 'auto' picks a comment char no message line starts with.
    _git(root, "config", "core.commentChar", "auto")
    if copy:
        dest = Path(_git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "hooks"
        dest.mkdir(parents=True, exist_ok=True)
        for f in FILES:
            shutil.copyfile(SRC / f, dest / f)
            (dest / f).chmod(0o755)
        return str(dest)
    _git(root, "config", "core.hooksPath", str(SRC))
    return str(SRC)
