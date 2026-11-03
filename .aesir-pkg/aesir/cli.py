"""Command line: python -m aesir [--db PATH] [--as ROLE] [--json] <command>.

Commands live in aesir/commands/, one module each (see its __init__); this file only discovers and dispatches them."""
import argparse
import importlib
import os
import pkgutil
import sqlite3
import sys
from pathlib import Path

from . import commands, db, service as S
from .commands._common import common


def command_modules():
    return [importlib.import_module(f"{commands.__name__}.{m.name}")
            for m in sorted(pkgutil.iter_modules(commands.__path__), key=lambda m: m.name)
            if not m.name.startswith("_")]


def _no_abbrev(parser):
    """No prefix abbreviation of options ('--allow-mig'): the migrate guard matches the flag text (#095)."""
    parser.allow_abbrev = False
    for a in parser._actions:
        if isinstance(a, argparse._SubParsersAction):
            for sp in a.choices.values():
                _no_abbrev(sp)


def build_parser():
    top = argparse.ArgumentParser(prog="aesir", parents=[common(None)])
    sub = top.add_subparsers(dest="cmd", required=True)
    for mod in command_modules():
        mod.register(sub)
    _no_abbrev(top)
    return top


def _special(attr):
    return set().union(*(getattr(m, attr, set()) for m in command_modules()))


def launcher_main(pkg_dir, argv=None):
    """Entry of the client launcher (.aesir-pkg/aesir_run.py). When the cwd holds no project, use the one holding
    pkg_dir (the folder with .aesir-pkg) and say so on stderr. Hooks and explicit --db/AESIR_DB keep their own
    resolution; a cwd inside a project is untouched."""
    argv = sys.argv[1:] if argv is None else argv
    home = Path(pkg_dir).resolve().parent
    explicit = os.environ.get("AESIR_DB") or any(a == "--db" or a.startswith("--db=") for a in argv)
    if not explicit and not set(argv[:1]) & _special("RAW") and (home / ".aesir" / "aesir.db").exists():
        try:
            db.require_project()
        except db.NoProject:
            pass
        else:
            return main(argv)  # the cwd holds a project: unchanged
        print(f"aesir: no project at {Path.cwd()}; using {home} (holds this .aesir-pkg)", file=sys.stderr)
        os.chdir(home)  # config (aesir.toml), git and the db all resolve from the project from here on
    return main(argv)


def main(argv=None):
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    if args.cmd in _special("RAW"):  # Claude Code hook: stdin JSON, never creates a db, never fails
        return args.func(args)
    if args.cmd in _special("PRE_DB"):  # file operations; no database of this checkout involved
        try:
            args.func(args)
        except (S.AesirError, OSError, db.NoProject) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        return 0
    # an agent instance id (AESIR_AGENT) wins over --as: agents cannot pose as another role
    args.actor = os.environ.get("AESIR_AGENT") or args.actor or os.environ.get("AESIR_ROLE") or "unknown"
    try:
        path = db.require_project(args.db)  # #257: never create a db outside a project
        conn = db.connect(path, allow_migrate=True if args.allow_migrate else None)
        try:
            claimed = os.environ.get("AESIR_AGENT")
            if claimed and not conn.execute("SELECT 1 FROM agents WHERE agent_id=? AND ended=''", (claimed,)).fetchone():
                args.actor = f"unverified:{claimed}"
                print(f"warning: AESIR_AGENT={claimed} is not a running agent; recorded as {args.actor}", file=sys.stderr)
            if not claimed:
                from .rolenames import normalise_role
                args.actor = normalise_role(conn, args.actor)
            args.func(conn, args)
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        if "readonly" not in str(e).lower():
            raise
        print("error: db schema is newer than this code; writes refused (update the code checkout)", file=sys.stderr)
        return 1
    except (S.AesirError, ValueError, sqlite3.IntegrityError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0
