"""Deterministic text export of the database (JSON lines, one file per table) and restore from it."""
import json
from pathlib import Path

from . import db
from .service import AesirError

STATE_DIR = "aesir-state"  # the export directory under the project root
SUFFIX = ".jsonl"
SEQ = "sqlite_sequence"  # optional in a dump: older dumps lack it


def _tables(conn):
    """Real tables only: no sqlite internals, no FTS virtual table or its shadow tables."""
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    skip = {"tips_fts", "messages_fts", "queue_lock"}  # queue_lock is transient
    return [n for n in names if n not in skip and not n.startswith(("tips_fts_", "messages_fts_"))]


def _order(conn, table):
    info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    pk = [r["name"] for r in sorted(info, key=lambda r: r["pk"]) if r["pk"]]
    return pk or [r["name"] for r in info]  # no primary key (links): sort by every column


def export(conn, out_dir, roadmap_root=None, notes=None) -> dict:
    """Write <table>.jsonl for every table; returns {table: row count}. Same data -> same bytes.
    roadmap_root: the repo root to also write ROADMAP.md into (#228); the one place every state export goes through.
    notes: optional list that gets a line when ROADMAP.md is skipped (a hand-written one is never overwritten)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    counts = {}
    for t in _tables(conn):
        order = ", ".join(f'"{c}"' for c in _order(conn, t))
        rows = conn.execute(f'SELECT * FROM "{t}" ORDER BY {order}').fetchall()
        text = "".join(json.dumps(dict(r), sort_keys=True, ensure_ascii=False) + "\n" for r in rows)
        with open(out / (t + SUFFIX), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        counts[t] = len(rows)
    # AUTOINCREMENT high-water marks, so ids of deleted rows are never reused after a restore
    seqs = conn.execute("SELECT name, seq FROM sqlite_sequence ORDER BY name").fetchall()
    with open(out / (SEQ + SUFFIX), "w", encoding="utf-8", newline="\n") as f:
        f.write("".join(json.dumps(dict(r), sort_keys=True) + "\n" for r in seqs))
    if roadmap_root is not None:
        from . import releases  # late: releases pulls in install, which a plain dump does not need
        if releases.write_roadmap(conn, roadmap_root) is None and notes is not None:
            notes.append("ROADMAP.md exists and is not generated (no generated header); left untouched")
    return counts


def _read(path):
    with open(path, encoding="utf-8", newline="") as f:
        return [json.loads(x) for x in f.read().split("\n") if x]


def restore(conn, src_dir) -> dict:
    """Load a dump into an empty database at the same schema version; returns {table: row count}."""
    src = Path(src_dir)
    if not src.is_dir():
        raise AesirError(f"not a directory: {src}")
    tables = _tables(conn)
    for t in tables:
        q = f'SELECT COUNT(*) FROM "{t}"' + (" WHERE key NOT IN ('schema_version','evidence_since')" if t == "meta" else "")
        if conn.execute(q).fetchone()[0]:
            raise AesirError(f"refusing to restore: database is not empty ({t} has rows)")
    meta_file = src / ("meta" + SUFFIX)
    meta = _read(meta_file) if meta_file.exists() else []
    ver = next((r["value"] for r in meta if r["key"] == "schema_version"), None)
    have = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
    if not (ver or "").isdigit() or int(ver) > int(have):
        raise AesirError(f"schema version mismatch: dump is {ver}, database is {have}")
    # an older dump loads into a newer schema: its rows lack the new columns, which take their defaults
    files = {p.name[:-len(SUFFIX)] for p in src.glob("*" + SUFFIX)} - {SEQ}
    if files - set(tables):
        raise AesirError(f"dump has unknown tables: {', '.join(sorted(files - set(tables)))}")
    counts = {}
    conn.execute("PRAGMA foreign_keys=OFF")  # table order is alphabetical, not dependency order
    try:
        with db.tx(conn):
            for t in tables:
                rows = _read(src / (t + SUFFIX)) if t in files else []
                for r in rows:
                    if t == "meta" and r["key"] == "schema_version":
                        continue  # keep the database's own (possibly newer) version
                    ks = sorted(r)
                    cols = ",".join(f'"{k}"' for k in ks)
                    conn.execute(f'INSERT OR REPLACE INTO "{t}" ({cols}) VALUES ({",".join("?" * len(ks))})',
                                 [r[k] for k in ks])
                counts[t] = len(rows)
            seq_file = src / (SEQ + SUFFIX)
            for r in _read(seq_file) if seq_file.exists() else []:
                if r["name"] not in tables:
                    raise AesirError(f"dump has sequence for unknown table: {r['name']}")
                cur = conn.execute("UPDATE sqlite_sequence SET seq=max(seq, ?) WHERE name=?", (r["seq"], r["name"]))
                if not cur.rowcount:
                    conn.execute("INSERT INTO sqlite_sequence (name, seq) VALUES (?,?)", (r["name"], r["seq"]))
            if db.has_fts(conn):
                conn.execute("DELETE FROM tips_fts")
                conn.execute("INSERT INTO tips_fts (rowid, topic, body) SELECT id, topic, body FROM tips")
            if db.has_msg_fts(conn):
                conn.execute("DELETE FROM messages_fts")
                conn.execute("INSERT INTO messages_fts (rowid, body) SELECT id, body FROM messages")
            if conn.execute("PRAGMA foreign_key_check").fetchall():
                raise AesirError("restored data violates foreign keys")
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
    return counts
