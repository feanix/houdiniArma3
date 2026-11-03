"""Schema skew (#178, reports#121, D183): hook code older than the db's schema.

A worktree cut before an upgrade runs its own (old) hook code against the db the upgrade migrated. That code cannot
read the newer schema, so the hook used to return nothing: no scope guard, no notes, no agent rows, no trace. Now the
hook says so (to the agent and in the db), and `status` shows it.

Two sides, only one of which exists for code already deployed:
  * the hook of the OLD code (this module, once it ships) names the skew, records it and fails closed on edits;
  * the NEW code (`status`) looks at running agents' worktrees and flags one whose own aesir/db.py has fewer
    migrations than the db (`worktree_flags`). That works even if the old hook is a release that predates this module.
"""
import ast
import json
import sys
from pathlib import Path

from . import db

ENTITY, ID = "hook", "schema_skew"
PKG_DIR = ".aesir-pkg"


def message(code_schema, db_schema, code_path):
    """The text the agent sees. Names both schema versions, the code that ran, and the fix."""
    return (f"Aesir hook code is OLDER than the database: this code ({code_path}) knows schema {code_schema}, the db is "
            f"at schema {db_schema}. The Aesir guards are not running in this state (no scope guard, steering notes or "
            f"agent tracking). Fix: stop and tell the lead to re-spin this worktree from the current main head "
            f"(a worktree cut before an upgrade keeps the old Aesir code).")


def _recorded(conn, who, code_path):
    for r in conn.execute("SELECT payload FROM events WHERE entity=? AND entity_id=? ORDER BY id DESC", (ENTITY, ID)):
        try:
            p = json.loads(r[0])
        except ValueError:
            continue
        if p.get("who") == who and p.get("code_path") == code_path:
            return True
    return False


def record(conn, d, db_schema):
    """Write one event per (agent or session, code path) so `status` can name them; returns True when it is new.
    A plain insert: the events table is the same in every schema, so the old code can write it. Never raises."""
    who = d.get("agent_id") or f"session:{d.get('session_id') or '?'}"
    code_path = str(db.code_root())
    try:
        if _recorded(conn, who, code_path):
            return False
        payload = {"who": who, "agent_type": d.get("agent_type") or "", "code_path": code_path,
                   "code_schema": len(db.MIGRATIONS), "db_schema": db_schema, "hook_event": d.get("hook_event_name") or "",
                   "cwd": d.get("cwd") or ""}
        conn.execute("BEGIN IMMEDIATE")
        try:
            db.event(conn, "aesir-hook", ENTITY, ID, "record", payload)
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        return True
    except Exception as e:  # noqa: BLE001 -- record() must never raise out of a hook; stderr line and agent message still go out
        print(f"aesir hook: schema skew not recorded: {e}", file=sys.stderr)
        return False


def flags(c):
    """(who, payload) for each recorded skew, latest per (who, code path)."""
    seen = {}
    for r in c.execute("SELECT payload FROM events WHERE entity=? AND entity_id=? ORDER BY id", (ENTITY, ID)):
        try:
            p = json.loads(r[0])
        except ValueError:
            continue
        seen[(p.get("who"), p.get("code_path"))] = p
    return [(k[0], p) for k, p in seen.items()]


DB_PATHS = (("aesir", "db.py"), (PKG_DIR, "aesir", "db.py"))


def migrations_in_text(text):
    """Number of entries in the MIGRATIONS list of db.py source `text`, read without importing it; None when absent
    or unparsable. Shared by the checkout reader below and the queue's read of a branch (#266)."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    for n in tree.body:
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "MIGRATIONS" for t in n.targets) \
                and isinstance(n.value, ast.List):
            return len(n.value.elts)
    return None


def code_migrations(root):
    """Number of migrations in the aesir/db.py of the checkout at `root` (or its vendored .aesir-pkg), read without
    importing it; None when it cannot be found or read."""
    for rel in DB_PATHS:
        f = Path(root, *rel)
        if not f.is_file():
            continue
        try:
            return migrations_in_text(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
    return None


def worktree_flags(c, target=None):
    """[(agent id, worktree, its migrations, target)] for running agents whose worktree code has fewer migrations than
    `target` (default: the db's schema version). The queue passes the count a merge would leave main with (#266)."""
    v = db._version(c) if target is None else target
    res = []
    for g in c.execute("SELECT agent_id, worktree FROM agents WHERE ended='' AND worktree!='' ORDER BY id"):
        n = code_migrations(g["worktree"])
        if n is not None and n < v:
            res.append((g["agent_id"], g["worktree"], n, v))
    return res
