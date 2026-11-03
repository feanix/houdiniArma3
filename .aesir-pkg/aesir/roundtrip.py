"""Gate check: a database survives export -> restore -> export byte for byte."""
import sys
import tempfile
from pathlib import Path

from . import backup, db, service as S


def main():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        a = db.connect(d / "a.db")
        tid = S.new_task(a, "lead", "round trip", "1. x")
        S.log_task(a, "lead", tid, "note")
        S.add_decision(a, "lead", "d", "ctx", "choice")
        S.add_tip(a, "lead", "tip body", "t")
        backup.export(a, d / "one")
        b = db.connect(d / "b.db")
        backup.restore(b, d / "one")
        backup.export(b, d / "two")
        a.close()
        b.close()
        for f in sorted((d / "one").iterdir()):
            if f.read_bytes() != (d / "two" / f.name).read_bytes():
                print(f"round trip differs: {f.name}")
                return 1
    print("export round trip ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
