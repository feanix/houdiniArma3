"""install-githooks."""
from .. import db, hooks
from ._common import add


def install_githooks(c, a):
    where = hooks.install(db.find_root(), a.copy)
    print(f"git hooks installed ({'copied to' if a.copy else 'core.hooksPath ='} {where})")


def register(sub):
        add(sub, "install-githooks", install_githooks,
            ("--copy", {"action": "store_true", "help": "copy into .git/hooks instead of setting core.hooksPath"}))
