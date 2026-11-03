"""Lessons log (#126, D133): what happened and what to do differently, kept in the generic space store
(role 'lessons', kind = scope, key = zero-padded number), so no schema change and export/restore carry it.
Scope 'aesir' entries are bundled into one upstream report (`lesson send`); 'project' entries stay local and are
reviewed with the owner at project close (`lesson review`)."""
from . import service as S, upstream

ROLE = "lessons"
SCOPES = ("aesir", "project")
QUEUED = "queued"


def _all(conn):
    """All entries oldest first; the store returns keys in no promised order, so sort by id."""
    rows = [r["body"] for r in S.space_list(conn, ROLE)]
    return sorted(rows, key=lambda b: b["id"])


def add(conn, actor, scope, what, lesson, evidence=""):
    """Store a new entry with the next number; rejects unknown scopes and empty text so the log stays useful."""
    if scope not in SCOPES:
        raise S.AesirError(f"--scope must be one of {', '.join(SCOPES)}")
    if not (what or "").strip() or not (lesson or "").strip():
        raise S.AesirError("what happened and --lesson must not be empty")
    n = max((b["id"] for b in _all(conn)), default=0) + 1
    body = {"id": n, "scope": scope, "what": what.strip(), "lesson": lesson.strip(),
            "evidence": (evidence or "").strip(), "added_by": actor, "ts": S.now(), "reviewed": "", "report": ""}
    S.space_put(conn, actor, ROLE, scope, f"{n:06d}", body)
    return n


def _save(conn, actor, b):
    """Write an entry back under its original key, used when reviewed or report fields change."""
    S.space_put(conn, actor, ROLE, b["scope"], f"{b['id']:06d}", b)


def sync(conn, actor):
    """Replace the 'queued' placeholder with the real issue URL once the queued bundle was filed (retry_queue records
    ref -> url). Entries without a filed ref are left alone, so it stays queued and is still never resent."""
    db_file = upstream._conn_file(conn)
    for b in _all(conn):
        if b["report"] == QUEUED and b.get("ref"):
            url = upstream.filed_url(db_file, b["ref"])
            if url:
                b["report"] = url
                _save(conn, actor, b)


def list_(conn, scope=None, actor="system"):
    """Listing, optionally limited to one scope; first picks up links of queued reports filed since."""
    sync(conn, actor)
    return [b for b in _all(conn) if not scope or b["scope"] == scope]


def review(conn, actor):
    """Unreviewed entries grouped by scope; marks them reviewed. Returns {scope: [entries as shown]}."""
    sync(conn, actor)
    groups = {s: [] for s in SCOPES}
    for b in _all(conn):
        if b["reviewed"]:
            continue
        groups[b["scope"]].append(dict(b))
        b["reviewed"] = S.now()
        _save(conn, actor, b)
    return groups


def render(b):
    """One-line text form of an entry, with evidence and report link shown only when present."""
    ev = f" [evidence: {b['evidence']}]" if b["evidence"] else ""
    rep = f" [report: {b['report']}]" if b["report"] else ""
    return f"L{b['id']} ({b['scope']}) {b['what']} -> {b['lesson']}{ev}{rep}"


def bundle_body(items):
    """Markdown body for the single upstream report, one section per lesson so maintainers can triage each."""
    parts = [f"Lessons from a project ({len(items)}), scope aesir.", ""]
    for b in items:
        parts.append(f"### L{b['id']}: {b['what']}")
        parts.append(f"Lesson: {b['lesson']}")
        if b["evidence"]:
            parts.append(f"Evidence: {b['evidence']}")
        parts.append("")
    return "\n".join(parts)


def send(conn, actor, dry_run=False, root=None):
    """File every unsent aesir-scope entry as ONE upstream report; record the link on each. Idempotent: sent
    entries (report set) are skipped, so a second run with nothing new files nothing. Returns the report result or None."""
    sync(conn, actor)
    items = [b for b in _all(conn) if b["scope"] == "aesir" and not b["report"]]
    if not items:
        return None
    ref = f"lessons-{S.now()}-L{items[0]['id']}"
    r = upstream.report(conn, actor, f"Lessons log: {len(items)} lesson(s) from a project", bundle_body(items),
                        dry_run=dry_run, root=root, kind="suggestion", ref=ref)
    if dry_run:
        return r
    link = r.get("url") or (QUEUED if r.get("queued") else "")
    if link:
        for b in items:
            b["report"] = link
            if link == QUEUED:
                b["ref"] = ref
            _save(conn, actor, b)
    r["count"] = len(items)
    return r
