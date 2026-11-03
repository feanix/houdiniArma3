"""Role-name normalisation shared by the hook (session_role) and the CLI/service write paths (task #38).
A session or actor name is '<role>' or '<prefix>-<role>'; '<role>' is advisor, lead or a checked-in role."""
import re
import tomllib
from pathlib import Path

ROLE_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


def _project(root):
    try:
        proj = tomllib.loads((Path(root) / "aesir.toml").read_text(encoding="utf-8")).get("project") or {}
    except (OSError, ValueError, TypeError):
        proj = {}
    return proj if isinstance(proj, dict) else {}


def prefix_configured(root):
    """True when aesir.toml [project] session_prefix is set (else the prefix is derived from [project] name)."""
    v = _project(root).get("session_prefix")
    return isinstance(v, str) and bool(v.strip())


def session_prefix(root):
    """Session-name prefix for the project at `root`: aesir.toml [project] session_prefix, else [project] name,
    else 'aesir'."""
    proj = _project(root)
    v = proj.get("session_prefix") or proj.get("name")
    return v.strip().lower() if isinstance(v, str) and v.strip() else "aesir"


def role_ok(conn, name):
    return bool(ROLE_NAME_RE.match(name)) and (name in ("advisor", "lead") or
                                               bool(conn.execute("SELECT 1 FROM roles WHERE role=?", (name,)).fetchone()))


def resolve_role(conn, raw, prefix):
    """The known role named by `raw` ('<role>', '<prefix>-<role>', or '<anything>-<role>'), else ''."""
    name = (raw or "").strip().lower()
    cands = [name]
    if name.startswith(prefix + "-"):
        cands.append(name[len(prefix) + 1:])
    cands += [name[i + 1:] for i, ch in enumerate(name) if ch == "-"]
    for c in cands:
        if role_ok(conn, c):
            return c
    return ""


def normalise_role(conn, name, root=None):
    """'<project>-<role>' -> '<role>' when <role> is a known role; anything else (agent ids, empty, unknown) is
    returned unchanged."""
    if not name or not isinstance(name, str) or not ROLE_NAME_RE.match(name.strip().lower()):
        return name  # empty, 'unverified:<id>' and other non-role shapes are left alone
    try:
        from . import db
        prefix = session_prefix(root or db.project_root())
    except OSError:  # cwd gone or .git unreadable; anything else is a bug and propagates
        prefix = ""  # no prefix: resolve_role still tries every '-' suffix; never guess 'aesir' for a client project
    return resolve_role(conn, name, prefix) or name


AGENT_TYPES = ("implementer", "reviewer", "runner", "researcher")  # the same set as tracks.BASES (no import: cycle)


def agent_role(row):
    """The agent's own type ('implementer', 'reviewer'...) from a row or dict (#248, reports#153): the recorded
    agent_type when it is a known type, else the id prefix (old rows, or a row whose harness type is general-purpose;
    for those service.name_allowed keeps the prefix equal to the role, so it still says what the agent is). The id
    prefix is free text for a typed agent ('checker-261-a' for a reviewer) and must never decide role, scope or limits."""
    try:
        t = row["agent_type"] or ""
    except (KeyError, IndexError):
        t = ""
    if t in AGENT_TYPES:
        return t
    try:
        aid = row["agent_id"] or ""
    except (KeyError, IndexError):
        aid = ""
    return aid.split("-")[0]


def reserved_prefix(conn, prefix):
    """True when `prefix` names a persistent role (advisor, lead, a checked-in role) or an agent type: no agent of
    another type may borrow it in its id (#248): a reviewer named 'lead-261-a' would read as the lead."""
    return (prefix in ("advisor", "lead") or prefix in AGENT_TYPES
            or bool(conn.execute("SELECT 1 FROM roles WHERE role=?", (prefix,)).fetchone()))


def id_role(conn, agent_id):
    """agent_role for an agent id: the latest row of that id supplies the type; an id with no row (an event's history,
    a transcript with no row) falls back to its prefix (#248)."""
    r = conn.execute("SELECT agent_id, agent_type FROM agents WHERE agent_id=? ORDER BY id DESC LIMIT 1",
                     (agent_id or "",)).fetchone()
    return agent_role(r) if r else (agent_id or "").split("-")[0]
