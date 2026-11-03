"""release, install, upgrade (file operations; no database involved)."""
import json
import sys
from pathlib import Path

from .. import db, install, releases, upstream_pickup
from ._common import add, client_cmd, text_arg as _text_arg


def plan_cmd(a):
    """`release plan [--json]` (#228): planned releases, missed and unplaced tasks. Read-only; opens the db itself because
    `release` is a PRE_DB command."""
    root = db.project_root()
    c = db.connect(db.require_project(a.db), allow_migrate=True if a.allow_migrate else None)
    try:
        p = releases.plan(c, releases.tagged_versions(root))
    finally:
        c.close()
    if a.json:
        print(json.dumps(p, indent=2, ensure_ascii=False))
    else:
        print("\n".join(releases.format_plan(p)))


def release_cmd(a):
    if a.version == "plan":
        return plan_cmd(a)
    tag = install.release(db.find_root(), a.version, _text_arg(a.notes))
    print(f"released {tag} (CHANGELOG.md updated, changes/ fragments folded in and removed, committed)")
    # No upstream comment here (#233): the tag is still local and the full gate has not run at it. D193 order is
    # gate PASS, push, then `upstream fixed-in`. Show what that step will do so the lead can comment by hand.
    try:
        nums, skipped = upstream_pickup.plan_release(db.find_root(), tag, a.db)
        for n in nums:
            print(f"will announce: #{n} fixed in {tag}")
        for line in upstream_pickup.skip_lines(skipped, a.all):
            print(line)
    except Exception as e:
        print(f"warning: could not plan upstream announcements: {e}")
    print(client_cmd(f"next: run the full gate at the tag commit, push main and {tag}, then "
                     f"`python -m aesir upstream fixed-in {tag}` posts the 'fixed in' comments, then message aesir-advisor "
                     f"(release-notify-advisor): tag, contents, client actions, fixed issues"))


def install_cmd(a):
    r = install.install(a.client, a.version, a.source, a.project, a.session_prefix)
    print(f"installed Aesir {r['version']} in {r['client']} ({len(r['agents'])} agents, hooks, db); "
          f"run it as: python {install.PKG_DIR}/{install.LAUNCHER} <command>")


def upgrade_cmd(a):
    if not a.version and not a.repair:
        sys.exit("upgrade: --version is required (or --repair)")
    r = install.upgrade(a.client, a.version, a.source, a.allow_major, a.allow_downgrade, a.repair)
    print(f"{'repaired' if a.repair else 'upgraded'} {r['client']}: {r['from']} -> {r['version']}")


def install_setup_cmd(a):
    """Run by `upgrade`/`install` through the vendored (new) code: the setup steps of THIS version. Last line: agents JSON."""
    agents = install.finish_setup(Path(a.client), a.source, a.version, a.tree)
    print(json.dumps(agents))


def releases_cmd(a):
    source, pinned, found = releases.listing(db.project_root())
    print(f"source: {source}")
    for v, head in found:
        print(f"  {v}{' *pinned' if v == pinned else ''}{'  ' + head if head else ''}")
    if not found:
        print("  (no release tags)")
    elif pinned:
        print(releases.classify(pinned, found[0][0]) or f"pinned {pinned} is the newest")


def register(sub):
        add(sub, "release", release_cmd, ("version", {"help": "X.Y.Z, or 'plan' to show the release plan (task set N --release X.Y.Z places a task)"}),
            ("--notes", {"help": "agent-facing changelog text; @file reads a file"}),
            ("--all", {"action": "store_true", "help": "list every skipped issue, not counts for the routine reasons"}))
        add(sub, "releases", releases_cmd)
        add(sub, "install", install_cmd, ("client", {"help": "client project directory"}),
            ("--version", {"required": True, "help": "X.Y.Z (tag vX.Y.Z)"}),
            ("--from", {"dest": "source", "help": "Aesir repo to install from (default: this checkout)"}),
            ("--project", {"help": "project name for aesir.toml (default: directory name)"}),
            ("--session-prefix", {"help": "[project] session_prefix for aesir.toml, when sessions are named <prefix>-lead etc. "
                                          "and the prefix differs from the project name"}))
        add(sub, "upgrade", upgrade_cmd, ("client", {"nargs": "?", "default": ".", "help": "client directory (default: .)"}),
            ("--version", {}), ("--from", {"dest": "source"}),
            ("--allow-major", {"action": "store_true"}), ("--allow-downgrade", {"action": "store_true"}),
            ("--repair", {"action": "store_true", "help": "re-run setup with the pinned code, no re-vendor"}))
        add(sub, "install-setup", install_setup_cmd, ("--tree", {"required": True}), ("--from", {"dest": "source", "required": True}),
            ("--version", {"required": True}), ("--client", {"default": "."}))


PRE_DB = {"release", "releases", "install", "upgrade", "install-setup"}  # run before any database connect
