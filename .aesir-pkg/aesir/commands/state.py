"""import-state, export, restore."""
from pathlib import Path

from .. import backup, db, importer
from ._common import out, add


def import_state(c, a):
    state = Path(a.state) if a.state else db.find_root() / "state"
    res = importer.import_state(c, state)
    out(a, res, lambda r: print("imported:", ", ".join(f"{v} {k}" for k, v in r.items())))


def export_state(c, a):
    root = db.find_root()
    # ROADMAP.md goes with the repo's own state dump; an --out dump elsewhere is a copy and leaves the repo alone
    notes = []
    res = backup.export(c, Path(a.out) if a.out else root / backup.STATE_DIR, roadmap_root=None if a.out else root, notes=notes)
    for n in notes:
        print("note:", n)
    out(a, res, lambda r: print("exported:", ", ".join(f"{v} {k}" for k, v in r.items())))


def restore_state(c, a):
    res = backup.restore(c, a.dir)
    out(a, res, lambda r: print("restored:", ", ".join(f"{v} {k}" for k, v in r.items())))


def register(sub):
        add(sub, "import-state", import_state, ("--state", {"help": "state dir (default <root>/state)"}))
        add(sub, "export", export_state, ("--out", {"help": "output dir (default <root>/aesir-state)"}))
        add(sub, "restore", restore_state, ("dir", {"help": "dump directory written by export"}))
