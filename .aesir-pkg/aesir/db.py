"""SQLite connection, schema migrations, append-only events."""
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def find_root(start=None) -> Path:
    p = Path(start or Path.cwd()).resolve()
    for d in [p, *p.parents]:
        if (d / "CHARTER.md").exists() or (d / ".git").exists():
            return d
    return p


def project_root(start=None) -> Path:
    """find_root, but a linked git worktree maps to its main checkout (the db lives there)."""
    root = find_root(start)
    g = root / ".git"
    if g.is_file():
        m = re.match(r"gitdir:\s*(.+)", g.read_text(encoding="utf-8", errors="replace").strip())
        if m:  # <main>/.git/worktrees/<name>
            return Path(m.group(1).strip()).parent.parent.parent
    return root


def db_path(arg=None, start=None) -> Path:
    v = arg or os.environ.get("AESIR_DB")
    return Path(v) if v else project_root(start) / ".aesir" / "aesir.db"


class NoProject(ValueError):
    """No Aesir project (and no explicit db) where the command ran (#257)."""


def require_project(arg=None, start=None) -> Path:
    """The db path to use, or NoProject. One rule (#257): a db is opened/created only when it is named (--db / AESIR_DB),
    already exists, or the project root holds aesir.toml or .aesir-pkg (a fresh clone or an install in progress). A bare
    .git is not enough: a scratch folder inside some repo must not get a db at that repo's root."""
    path = db_path(arg, start)
    if arg or os.environ.get("AESIR_DB") or path.exists():
        return path
    root = project_root(start)
    if (root / "aesir.toml").exists() or (root / ".aesir-pkg").exists():
        return path
    raise NoProject(f"no Aesir project at {Path(start or Path.cwd()).resolve()}; run from the project or pass --db")


# Each migration is a list of statements (index + 1 == schema version it produces).
_V1 = [
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)",
    """CREATE TABLE tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open',
        owner TEXT NOT NULL DEFAULT '',
        requester TEXT NOT NULL DEFAULT '',
        scope TEXT NOT NULL DEFAULT '',
        spec TEXT NOT NULL DEFAULT '',
        body TEXT NOT NULL DEFAULT '',
        branch TEXT NOT NULL DEFAULT '',
        gate_profile TEXT NOT NULL DEFAULT '',
        attempts INTEGER NOT NULL DEFAULT 0,
        escalations INTEGER NOT NULL DEFAULT 0,
        wontfix_reason TEXT NOT NULL DEFAULT '',
        extra TEXT NOT NULL DEFAULT '{}',
        created TEXT NOT NULL,
        updated TEXT NOT NULL)""",
    """CREATE TABLE task_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL REFERENCES tasks(id),
        ts TEXT NOT NULL, actor TEXT NOT NULL, text TEXT NOT NULL)""",
    """CREATE TABLE decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL, title TEXT NOT NULL,
        context TEXT NOT NULL DEFAULT '', choice TEXT NOT NULL DEFAULT '',
        alternatives TEXT NOT NULL DEFAULT '', decided_by TEXT NOT NULL DEFAULT '',
        reversible TEXT NOT NULL DEFAULT '',
        raw TEXT NOT NULL DEFAULT '',
        ts TEXT NOT NULL,
        UNIQUE(date, title))""",
    """CREATE TABLE tips (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        topic TEXT NOT NULL DEFAULT '', body TEXT NOT NULL, ts TEXT NOT NULL,
        UNIQUE(topic, body))""",
    """CREATE TABLE links (
        a_type TEXT NOT NULL, a_id TEXT NOT NULL,
        b_type TEXT NOT NULL, b_id TEXT NOT NULL,
        UNIQUE(a_type, a_id, b_type, b_id))""",
    "CREATE TABLE roles (role TEXT PRIMARY KEY, session TEXT NOT NULL DEFAULT '', last_checkin TEXT NOT NULL DEFAULT '')",
    """CREATE TABLE role_notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        role TEXT NOT NULL, ts TEXT NOT NULL, actor TEXT NOT NULL, text TEXT NOT NULL)""",
    """CREATE TABLE events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL, actor TEXT NOT NULL, entity TEXT NOT NULL,
        entity_id TEXT NOT NULL, action TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}')""",
    """CREATE TRIGGER events_no_update BEFORE UPDATE ON events
        BEGIN SELECT RAISE(ABORT, 'events are append-only'); END""",
    """CREATE TRIGGER events_no_delete BEFORE DELETE ON events
        BEGIN SELECT RAISE(ABORT, 'events are append-only'); END""",
]
_V2 = [
    # identity core: terse standing facts about a role. rule = who may change a row.
    """CREATE TABLE core (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        role TEXT NOT NULL, section TEXT NOT NULL, key TEXT NOT NULL,
        text TEXT NOT NULL, rule TEXT NOT NULL CHECK (rule IN ('owner','lead')),
        updated TEXT NOT NULL, updated_by TEXT NOT NULL,
        UNIQUE(role, section, key))""",
    """CREATE TABLE core_proposals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        role TEXT NOT NULL, section TEXT NOT NULL, key TEXT NOT NULL,
        text TEXT NOT NULL, reason TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        proposed_by TEXT NOT NULL, ts TEXT NOT NULL,
        decided_by TEXT NOT NULL DEFAULT '', decided_ts TEXT NOT NULL DEFAULT '',
        decision_note TEXT NOT NULL DEFAULT '')""",
    # a role's own space: generic kind/key/json store. Promote a kind to a table via a new migration.
    """CREATE TABLE space (
        role TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL,
        body TEXT NOT NULL, ts TEXT NOT NULL,
        PRIMARY KEY (role, kind, key))""",
]
_V3 = [
    # the checkin before the current one: 'decisions since last checkin' counts from here
    "ALTER TABLE roles ADD COLUMN previous_checkin TEXT NOT NULL DEFAULT ''",
    # subagents a role has running (or has finished): role agent start/end records them
    """CREATE TABLE agents (
        role TEXT NOT NULL, agent_id TEXT NOT NULL, task_id INTEGER,
        started TEXT NOT NULL, ended TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (role, agent_id))""",
]
_V4 = [
    # which agent instance made the change (nullable: history and non-agent callers have none)
    "ALTER TABLE events ADD COLUMN agent_id TEXT",
    # agents gets its own key so a restarted id adds a row instead of overwriting history
    """CREATE TABLE agents_new (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        role TEXT NOT NULL, agent_id TEXT NOT NULL, task_id INTEGER,
        model TEXT NOT NULL DEFAULT '', session TEXT NOT NULL DEFAULT '',
        started TEXT NOT NULL, ended TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT '')""",
    "INSERT INTO agents_new (role, agent_id, task_id, started, ended, result)"
    " SELECT role, agent_id, task_id, started, ended, result FROM agents ORDER BY started, rowid",
    "DROP TABLE agents",
    "ALTER TABLE agents_new RENAME TO agents",
    "CREATE UNIQUE INDEX agents_running ON agents(role, agent_id) WHERE ended = ''",
]
_V5 = [
    # hook-bound identity: harness agent id, worktree, transcript and token totals per instance
    "ALTER TABLE agents ADD COLUMN agent_type TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE agents ADD COLUMN tool_use_id TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE agents ADD COLUMN harness_agent_id TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE agents ADD COLUMN bound TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE agents ADD COLUMN worktree TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE agents ADD COLUMN transcript_path TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE agents ADD COLUMN tokens_in INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE agents ADD COLUMN tokens_out INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE agents ADD COLUMN tokens_cache_read INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE agents ADD COLUMN tokens_cache_create INTEGER NOT NULL DEFAULT 0",
]
_V6 = [
    # executed gate runs (quick/full) against a commit; task_id null for ad-hoc runs
    """CREATE TABLE gate_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER REFERENCES tasks(id), profile TEXT NOT NULL,
        sha TEXT NOT NULL, branch TEXT NOT NULL DEFAULT '',
        passed INTEGER NOT NULL, duration REAL NOT NULL DEFAULT 0,
        tail TEXT NOT NULL DEFAULT '', agent_id TEXT, ts TEXT NOT NULL)""",
    "CREATE INDEX gate_runs_task ON gate_runs(task_id, sha)",
]
_V7 = [
    # merge queue: ordered approved tasks, and a single-row lock so two runs cannot overlap
    "CREATE TABLE merge_queue (seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL UNIQUE"
    " REFERENCES tasks(id), added TEXT NOT NULL, actor TEXT NOT NULL DEFAULT '')",
    "CREATE TABLE queue_lock (id INTEGER PRIMARY KEY CHECK (id = 1), pid INTEGER NOT NULL, taken REAL NOT NULL)",
]
_V8 = [
    # escalation: reviewer rejections of an approach, the tier ladder history, the failure dossier
    """CREATE TABLE approach_rejections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL REFERENCES tasks(id),
        ts TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL)""",
    """CREATE TABLE escalations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL REFERENCES tasks(id),
        ts TEXT NOT NULL, actor TEXT NOT NULL,
        from_tier TEXT NOT NULL, to_tier TEXT NOT NULL,
        failures INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '',
        gate_mark INTEGER NOT NULL DEFAULT 0, rej_mark INTEGER NOT NULL DEFAULT 0)  -- max ids seen at escalation""",
    # body is generated from the db; notes is what lead appends (kept across regeneration)
    """CREATE TABLE dossiers (
        task_id INTEGER PRIMARY KEY REFERENCES tasks(id),
        body TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '',
        created TEXT NOT NULL, updated TEXT NOT NULL, actor TEXT NOT NULL DEFAULT '')""",
]
_V9 = [
    # messages: ask/reply/note attached to a task or decision (or an announcement channel).
    # visible_to: JSON list of names allowed to read it; NULL = everyone. Sender and recipient are always in it.
    """CREATE TABLE messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL CHECK (kind IN ('ask','reply','note')),
        task_id INTEGER REFERENCES tasks(id), decision_id INTEGER REFERENCES decisions(id),
        channel TEXT NOT NULL DEFAULT '',
        parent_id INTEGER REFERENCES messages(id),
        sender TEXT NOT NULL, recipient TEXT NOT NULL DEFAULT '',
        body TEXT NOT NULL, visible_to TEXT,
        ts TEXT NOT NULL,
        read_ts TEXT NOT NULL DEFAULT '',
        reminded_ts TEXT NOT NULL DEFAULT '',
        handed_to TEXT NOT NULL DEFAULT '', handed_ts TEXT NOT NULL DEFAULT '')""",
    "CREATE INDEX messages_task ON messages(task_id)",
    "CREATE INDEX messages_parent ON messages(parent_id)",
    "CREATE INDEX messages_recipient ON messages(recipient, kind)",
]
_V10 = [
    # core row scope: owner (how this owner works, same across projects), role (general lessons for any lead/advisor),
    # project (this project only). Existing rows default to project: nothing propagates until a lead reviews it.
    "ALTER TABLE core ADD COLUMN scope TEXT NOT NULL DEFAULT 'project' CHECK (scope IN ('owner','role','project'))",
    "ALTER TABLE core_proposals ADD COLUMN scope TEXT NOT NULL DEFAULT 'project'"
    " CHECK (scope IN ('owner','role','project'))",
]
_V11 = [
    # per-criterion evidence (task #55): kind 'executed' (aesir ran the command) or 'claimed' (a note, not runnable);
    # sha = the checkout head it was recorded at, so later code changes make it stale
    """CREATE TABLE evidence (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL REFERENCES tasks(id), criterion INTEGER NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('executed','claimed')),
        cmd TEXT NOT NULL DEFAULT '', exit_code INTEGER, excerpt TEXT NOT NULL DEFAULT '',
        expect TEXT NOT NULL DEFAULT '', output_ref TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT '', ref TEXT NOT NULL DEFAULT '',
        sha TEXT NOT NULL DEFAULT '', branch TEXT NOT NULL DEFAULT '',
        actor TEXT NOT NULL DEFAULT '', ts TEXT NOT NULL)""",
    "CREATE INDEX evidence_task ON evidence(task_id, criterion)",
    # tasks that reached in_review before this moment predate the feature: no missing_evidence anomaly for them
    "INSERT OR REPLACE INTO meta VALUES ('evidence_since', strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
]
_V12 = [
    # tips (#056): tags select where a tip is surfaced; retired = promoted into core/a hook (kept for history);
    # impressions = each time a tip was shown to an agent or at a command.
    "ALTER TABLE tips ADD COLUMN tags TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE tips ADD COLUMN retired TEXT NOT NULL DEFAULT ''",
    """CREATE TABLE tip_impressions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tip_id INTEGER NOT NULL, agent_id TEXT NOT NULL DEFAULT '', task_id INTEGER, ts TEXT NOT NULL)""",
    "CREATE INDEX tip_impressions_tip ON tip_impressions(tip_id)",
]
_V13 = [
    # audit tooling (#067): audits = a plan (sharded review), a toolkit run or a bare import; findings = one row per
    # distinct problem (fingerprint), first_audit/last_audit give re-audit deltas; shards = per-agent file chunks.
    """CREATE TABLE audits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL CHECK (kind IN ('plan','run','import')),
        note TEXT NOT NULL DEFAULT '', actor TEXT NOT NULL DEFAULT '', ts TEXT NOT NULL)""",
    """CREATE TABLE findings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file TEXT NOT NULL, line INTEGER NOT NULL DEFAULT 0,
        severity TEXT NOT NULL CHECK (severity IN ('info','low','medium','high')),
        rule TEXT NOT NULL, source TEXT NOT NULL, message TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','fixed','wontfix','false_positive')),
        task_id INTEGER REFERENCES tasks(id), fingerprint TEXT NOT NULL,
        first_audit INTEGER NOT NULL DEFAULT 0, last_audit INTEGER NOT NULL DEFAULT 0,
        created TEXT NOT NULL, updated TEXT NOT NULL)""",
    "CREATE UNIQUE INDEX findings_fp ON findings(fingerprint)",
    "CREATE INDEX findings_file ON findings(file)",
    """CREATE TABLE audit_shards (
        audit_id INTEGER NOT NULL REFERENCES audits(id), n INTEGER NOT NULL,
        files TEXT NOT NULL, weight INTEGER NOT NULL DEFAULT 0, done TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (audit_id, n))""",
]
_V14 = [
    # findings.status gains 'baselined' (#082): expected for now, to be fixed (distinct from wontfix = never fix).
    # SQLite cannot alter a CHECK, so rebuild the table, keeping rows and indexes.
    """CREATE TABLE findings_new (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file TEXT NOT NULL, line INTEGER NOT NULL DEFAULT 0,
        severity TEXT NOT NULL CHECK (severity IN ('info','low','medium','high')),
        rule TEXT NOT NULL, source TEXT NOT NULL, message TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'open'
            CHECK (status IN ('open','fixed','wontfix','false_positive','baselined')),
        task_id INTEGER REFERENCES tasks(id), fingerprint TEXT NOT NULL,
        first_audit INTEGER NOT NULL DEFAULT 0, last_audit INTEGER NOT NULL DEFAULT 0,
        created TEXT NOT NULL, updated TEXT NOT NULL)""",
    "INSERT INTO findings_new SELECT id, file, line, severity, rule, source, message, status, task_id, fingerprint,"
    " first_audit, last_audit, created, updated FROM findings",
    "DROP TABLE findings",
    "ALTER TABLE findings_new RENAME TO findings",
    "CREATE UNIQUE INDEX findings_fp ON findings(fingerprint)",
    "CREATE INDEX findings_file ON findings(file)",
]
_V15 = [
    # decision amendments (#101): decisions rows stay immutable; a correction is a new row here. New tables only, so an
    # older release reading this db (query-only, as for any newer schema) sees the same decisions columns as before.
    """CREATE TABLE decision_amendments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        decision_id INTEGER NOT NULL REFERENCES decisions(id),
        note TEXT NOT NULL, actor TEXT NOT NULL, ts TEXT NOT NULL)""",
    "CREATE INDEX decision_amendments_dec ON decision_amendments(decision_id)",
    # new_id supersedes old_id; the old decision is not edited
    """CREATE TABLE decision_supersedes (
        new_id INTEGER NOT NULL REFERENCES decisions(id), old_id INTEGER NOT NULL REFERENCES decisions(id),
        actor TEXT NOT NULL, ts TEXT NOT NULL, PRIMARY KEY (new_id, old_id))""",
]
_V16 = [
    # long-run lane (#106): one row per detached long run; status running|passed|failed|timed_out. New table only.
    """CREATE TABLE long_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project TEXT NOT NULL, root TEXT NOT NULL, profile TEXT NOT NULL,
        task_id INTEGER REFERENCES tasks(id), criterion INTEGER,
        sha TEXT NOT NULL, branch TEXT NOT NULL, cwd TEXT NOT NULL, steps TEXT NOT NULL, timeout REAL NOT NULL,
        status TEXT NOT NULL, pid INTEGER, pid_start TEXT, actor TEXT NOT NULL, started TEXT NOT NULL, ended TEXT NOT NULL DEFAULT '',
        exit_code INTEGER, log TEXT NOT NULL DEFAULT '', tail TEXT NOT NULL DEFAULT '', evidence_id INTEGER)""",
    "CREATE INDEX long_runs_status ON long_runs(status)",
]
_V17 = [
    # the persistent role's real session name (#120), recorded at SessionStart when the hook can see it
    "ALTER TABLE roles ADD COLUMN session_name TEXT NOT NULL DEFAULT ''",
]
_V18 = [
    # a finding's triage reason (#130): `finding set --note`, e.g. why it is wontfix
    "ALTER TABLE findings ADD COLUMN note TEXT NOT NULL DEFAULT ''",
]
_V19 = [
    # a steering note to a running subagent (#141) that its agent ended without reading: expired, not delivered
    "ALTER TABLE messages ADD COLUMN expired_ts TEXT NOT NULL DEFAULT ''",
]
_V20 = [
    # scheduled one-off runs (#139): `aesir at`; status armed|running|done|cancelled|missed. The waiter re-reads status at
    # fire time, so a cancelled row never runs even if its waiter process survives. New table only.
    """CREATE TABLE scheduled_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fire_at TEXT NOT NULL, command TEXT NOT NULL, cwd TEXT NOT NULL, actor TEXT NOT NULL, armed TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'armed', pid INTEGER, pid_start TEXT, log TEXT NOT NULL DEFAULT '',
        fired TEXT NOT NULL DEFAULT '', ended TEXT NOT NULL DEFAULT '', exit_code INTEGER)""",
    "CREATE INDEX scheduled_runs_status ON scheduled_runs(status)",
]
_V21 = [
    # supervised rounds (#156): Aesir holds a running round's state so a check reads and updates it; nothing lives only in a
    # cron prompt. state armed|stuck|disarmed; progress_sig = JSON of the signals seen at the last check. New table only.
    """CREATE TABLE rounds (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL REFERENCES tasks(id), label TEXT NOT NULL DEFAULT '', workflow_id TEXT NOT NULL DEFAULT '',
        agent_ids TEXT NOT NULL DEFAULT '', branch TEXT NOT NULL DEFAULT '', started TEXT NOT NULL,
        wall_limit_s INTEGER NOT NULL, last_check TEXT NOT NULL DEFAULT '', last_progress TEXT NOT NULL DEFAULT '',
        progress_sig TEXT NOT NULL DEFAULT '', idle_checks INTEGER NOT NULL DEFAULT 0,
        state TEXT NOT NULL DEFAULT 'armed', ended TEXT NOT NULL DEFAULT '', actor TEXT NOT NULL DEFAULT '')""",
    "CREATE INDEX rounds_state ON rounds(state)",
]
_V22 = [
    # task order (#176): 'task_id runs after after_id'. A separate table so `links` keeps its decision/tip/upstream meaning.
    """CREATE TABLE task_after (
        task_id INTEGER NOT NULL REFERENCES tasks(id), after_id INTEGER NOT NULL REFERENCES tasks(id),
        PRIMARY KEY (task_id, after_id), CHECK (task_id != after_id))""",
    "CREATE INDEX task_after_after ON task_after(after_id)",
]
_V23 = [
    # target release (#228): the release a task is planned for ('' = unplaced). Named target_release because RELEASE is an SQL
    # keyword. Feeds `release plan`, ROADMAP.md and the upstream milestones.
    "ALTER TABLE tasks ADD COLUMN target_release TEXT NOT NULL DEFAULT ''",
]
_V24 = [
    # evidence for a criterion that must FAIL (#243, aesir-reports#163): expect_fail '' = normal run, 'any' = any non-zero
    # exit, 'K' = exactly exit K; fail_met = 1 when the run failed as declared (the one verdict status() reads).
    "ALTER TABLE evidence ADD COLUMN expect_fail TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE evidence ADD COLUMN fail_met INTEGER NOT NULL DEFAULT 0",
]
MIGRATIONS = [_V1, _V2, _V3, _V4, _V5, _V6, _V7, _V8, _V9, _V10, _V11, _V12, _V13, _V14, _V15, _V16, _V17, _V18, _V19, _V20, _V21, _V22, _V23, _V24]  # append new lists here; never edit old ones


def has_fts(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name='tips_fts'").fetchone() is not None


def has_msg_fts(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name='messages_fts'").fetchone() is not None


def _version(conn) -> int:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone():
        return 0
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    return int(row[0]) if row else 0


def migrate(conn):
    if _version(conn) >= len(MIGRATIONS):
        return
    with tx(conn):
        v = _version(conn)  # re-check under the write lock (another process may have won)
        for i, stmts in enumerate(MIGRATIONS, 1):
            if i <= v:
                continue
            for s in stmts:
                conn.execute(s)
            if i == 1:  # optional full-text index for tips
                try:
                    conn.execute("CREATE VIRTUAL TABLE tips_fts USING fts5(topic, body)")
                except sqlite3.OperationalError:
                    pass
            if i == 9:  # optional full-text index for messages (rowid = message id)
                try:
                    conn.execute("CREATE VIRTUAL TABLE messages_fts USING fts5(body)")
                except sqlite3.OperationalError:
                    pass
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (str(i),))
            if v >= 1:  # upgrading an existing db; a brand-new db is simply created at the latest version
                event(conn, "system", "schema", i, "migrate", {"version": i})


class MigrationRefused(ValueError):
    """The connected code must not touch this db's schema (raised before any change)."""


def code_root() -> Path:
    """Root of the checkout this code was imported from (a worktree's own root, not the main one)."""
    root = Path(__file__).resolve().parents[1]
    # vendored client code lives at <project>/.aesir-pkg and belongs to <project> (its worktree's own copy maps to that worktree)
    return root.parent if root.name == ".aesir-pkg" else root


def _db_root(path):
    """Project root a db at <root>/.aesir/aesir.db belongs to; None for any other location (temp dbs)."""
    p = Path(path).resolve()
    return p.parent.parent if p.parent.name == ".aesir" else None


def _guard(conn, path, allow):
    """Return True when the connection must be read-only (db schema newer than this code).
    Raise MigrationRefused when this code would migrate a db that belongs to another checkout."""
    if allow is None:
        allow = os.environ.get("AESIR_ALLOW_MIGRATE") == "1"
    if allow and os.environ.get("AESIR_AGENT"):
        # Scope (D115): stops ACCIDENTAL use of the override by subagents, not a hostile agent. AESIR_AGENT is
        # exported by the agent hook into every subagent shell (lead/advisor sessions and the owner's shell never
        # have it) and the hook refuses commands that unset or override it. The text guard in agenthook catches
        # literal and common split spellings; a deliberately hostile agent can still evade it (out of scope).
        raise MigrationRefused("the migrate override (--allow-migrate / AESIR_ALLOW_MIGRATE) is refused for "
                               "subagents (AESIR_AGENT is set); use a temp db (--db) or ask the lead")
    v = _version(conn)
    if v > len(MIGRATIONS):
        return True
    if 0 < v < len(MIGRATIONS):
        root = _db_root(path)
        if root is not None and not allow and root != code_root():
            raise MigrationRefused(
                f"this code ({code_root()}) would migrate the db of another checkout ({root}) from schema "
                f"{v} to {len(MIGRATIONS)}; merge first, or pass --allow-migrate / AESIR_ALLOW_MIGRATE=1 "
                "(lead only; subagents and worktree branches must not: use a temp db)")
    return False


def connect(path, migrate_db=True, busy_ms=30000, allow_migrate=None) -> sqlite3.Connection:
    """migrate_db=False opens an existing db as is (no create, no journal change, no migration).
    A db with a newer schema than this code opens query-only; a db of another checkout that needs
    migrating is refused unless allow_migrate (or AESIR_ALLOW_MIGRATE=1)."""
    path = Path(path)
    if migrate_db:
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=busy_ms / 1000, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={int(busy_ms)}")
    conn.execute("PRAGMA foreign_keys=ON")
    if migrate_db:
        try:
            ro = _guard(conn, path, allow_migrate)
        except BaseException:
            conn.close()
            raise
        if ro:
            conn.execute("PRAGMA query_only=ON")
        else:
            conn.execute("PRAGMA journal_mode=WAL")
            migrate(conn)
    return conn


def current(conn) -> bool:
    return _version(conn) == len(MIGRATIONS)


@contextmanager
def tx(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def event(conn, actor, entity, entity_id, action, payload=None):
    cols = "ts, actor, entity, entity_id, action, payload"
    vals = [now(), actor, entity, str(entity_id), action, json.dumps(payload or {}, ensure_ascii=False)]
    agent = os.environ.get("AESIR_AGENT")
    if agent and actor == agent:  # only a verified agent id (the CLI downgrades unverified ones)  # system events (migrations) may run before the column exists
        cols, vals = cols + ", agent_id", vals + [agent]
    conn.execute(f"INSERT INTO events ({cols}) VALUES ({','.join('?' * len(vals))})", vals)
