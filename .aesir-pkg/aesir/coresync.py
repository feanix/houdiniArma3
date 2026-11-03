"""Propagate role knowledge from an Aesir checkout's core to a client's core (owner + role scoped rows only).

Scope (core.scope): owner = how this owner works (same across projects), role = general lessons for any
lead/advisor, project = this project only. Project rows never leave their db and are never touched here.
A client row marked project is a local override: sync leaves it alone (that is how a client keeps a reworded row).
Source dbs are opened read-only; nothing in the source checkout changes."""
import sqlite3
from pathlib import Path

from . import db
from .db import tx
from .service import AesirError, SCOPES, _apply_core, event, now

CLIENT_CMD = "python .aesir-pkg/aesir_run.py"
SOURCE_CMD = "python -m aesir"


def source_db(source):
    """DB file of an Aesir checkout (or a db file given directly)."""
    p = Path(source)
    if p.is_file():
        return p
    return db.project_root(p) / ".aesir" / "aesir.db"


def source_rows(source):
    """Owner+role scoped core rows of the source db, read-only. Raises AesirError when unusable."""
    path = source_db(source)
    if not path.exists():
        raise AesirError(f"no Aesir database at {path} (use --from <aesir checkout>)")
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        try:
            rows = conn.execute("SELECT role, section, key, text, rule, scope FROM core"
                                " WHERE scope IN ('owner','role') ORDER BY id").fetchall()
        except sqlite3.OperationalError as e:
            raise AesirError(f"source db at {path} has no core scopes ({e}); upgrade the source checkout first")
    finally:
        conn.close()
    return [dict(r) for r in rows]


def adapt(text):
    """Substitute the client's CLI invocation for the source's."""
    return text.replace(SOURCE_CMD, CLIENT_CMD)


def plan(conn, rows, add_only=False):
    """Diff source rows against the client core. Each item: action in add|update|propose|skip|same."""
    items = []
    for r in rows:
        text = adapt(r["text"])
        cur = conn.execute("SELECT text, rule, scope FROM core WHERE role=? AND section=? AND key=?",
                           (r["role"], r["section"], r["key"])).fetchone()
        item = {"role": r["role"], "section": r["section"], "key": r["key"], "scope": r["scope"],
                "rule": r["rule"], "text": text, "old": cur["text"] if cur else None}
        carried = r["rule"] == "owner" and r["scope"] == "owner"  # owner approved it for all projects
        if cur and cur["scope"] == "project":
            item["action"], item["why"] = "skip", "client row is project scoped (local override)"
        elif cur and cur["text"] == text:
            item["action"] = "same"
        elif cur and add_only:
            item["action"], item["why"] = "skip", "exists (add-only)"
        elif not carried and (r["rule"] == "owner" or (cur and cur["rule"] == "owner")):
            pend = conn.execute("SELECT 1 FROM core_proposals WHERE role=? AND section=? AND key=? AND text=?"
                                " AND status='pending'", (r["role"], r["section"], r["key"], text)).fetchone()
            item["action"] = "skip" if pend else "propose"
            if pend:
                item["why"] = "proposal already pending"
        else:
            item["action"] = "update" if cur else "add"
        items.append(item)
    return items


def apply(conn, actor, items, source):
    """Write a plan. One transaction; every row gets a decision (via _apply_core), proposals get an event."""
    n = {"add": 0, "update": 0, "propose": 0}
    reason = f"core sync from {source}"
    with tx(conn):
        for i in items:
            if i["action"] in ("add", "update"):
                carried = i["rule"] == "owner"
                _apply_core(conn, actor, i["role"], i["section"], i["key"], i["text"], reason, i["rule"],
                            f"{actor} (owner approval carried from source)" if carried else actor,
                            scope=i["scope"])
            elif i["action"] == "propose":
                cur = conn.execute(
                    "INSERT INTO core_proposals (role, section, key, text, reason, proposed_by, ts, scope)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (i["role"], i["section"], i["key"], i["text"], reason + " (owner approval not carried)",
                     actor, now(), i["scope"]))
                event(conn, actor, "core_proposal", cur.lastrowid, "propose",
                      {"role": i["role"], "section": i["section"], "key": i["key"], "reason": reason})
            else:
                continue
            n[i["action"]] += 1
    return n


def set_scope(conn, actor, role, section, key, scope):
    if scope not in SCOPES:
        raise AesirError(f"scope must be one of {', '.join(SCOPES)}")
    with tx(conn):
        row = conn.execute("SELECT id, scope FROM core WHERE role=? AND section=? AND key=?",
                           (role, section, key)).fetchone()
        if not row:
            raise AesirError(f"no such core row: {role}/{section}/{key}")
        conn.execute("UPDATE core SET scope=? WHERE id=?", (scope, row["id"]))
        event(conn, actor, "core", row["id"], "scope",
              {"role": role, "section": section, "key": key, "from": row["scope"], "to": scope})
