"""Migrate the Phase 0 `state/` markdown files into the database. Idempotent.

Anything not parsed into a field is kept as raw text (task `body`, decision `raw`,
unknown front matter in `extra`, unparsable log lines as text with empty ts/actor).
"""
import json
import re
from pathlib import Path

from .db import event, has_fts, now, tx

LOG_RE = re.compile(r"^- (\d{4}-\d{2}-\d{2}) ([\w-]+): (.*)$")
KEY_RE = re.compile(r"^- ([A-Za-z][A-Za-z ]*): (.*)$")
TASK_KEYS = {"id", "title", "status", "owner", "requester", "scope", "attempts"}


def _split_front(text):
    m = re.match(r"\A---\r?\n(.*?)\r?\n---\r?\n?(.*)\Z", text, re.S)
    if not m:
        return {}, text
    fm = {}
    for line in m.group(1).splitlines():
        k, _, v = line.partition(":")
        fm[k.strip()] = v.strip()
    return fm, m.group(2)


def _parse_log(text):
    entries = []
    for line in text.splitlines():
        if not line.strip():
            continue
        m = LOG_RE.match(line)
        if m:
            entries.append([m.group(1), m.group(2), m.group(3)])
        elif line.startswith("- ") or not entries:
            entries.append(["", "", line[2:] if line.startswith("- ") else line])
        else:
            entries[-1][2] += "\n" + line  # continuation line
    return entries


def import_task(conn, path: Path):
    fm, body = _split_front(path.read_text(encoding="utf-8"))
    if "id" not in fm:
        raise ValueError(f"{path}: task has no id in front matter")
    tid = int(fm["id"])
    lm = re.search(r"(?m)^## Log[ \t]*\r?\n", body)
    head, log = (body[:lm.start()], body[lm.end():]) if lm else (body, "")
    m = re.search(r"(?ms)^## Acceptance criteria[ \t]*\r?\n(.*?)(?=^## |\Z)", head)
    spec = m.group(1).strip() if m else ""
    extra = {k: v for k, v in fm.items() if k not in TASK_KEYS}
    created = False
    if not conn.execute("SELECT 1 FROM tasks WHERE id=?", (tid,)).fetchone():
        ts = now()
        conn.execute(
            "INSERT INTO tasks (id, title, status, owner, requester, scope, spec, body, attempts, extra, created, updated)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, fm.get("title", ""), fm.get("status", "open"), fm.get("owner", ""), fm.get("requester", ""),
             fm.get("scope", ""), spec, head.strip("\r\n"), int(fm.get("attempts") or 0),
             json.dumps(extra, ensure_ascii=False), ts, ts))
        event(conn, "import", "task", tid, "import", {"file": path.name})
        created = True
    have = {(r["ts"], r["actor"], r["text"]) for r in conn.execute(
        "SELECT ts, actor, text FROM task_log WHERE task_id=?", (tid,))}
    added = 0
    for ts, actor, text in _parse_log(log):
        if (ts, actor, text) not in have:
            conn.execute("INSERT INTO task_log (task_id, ts, actor, text) VALUES (?,?,?,?)", (tid, ts, actor, text))
            have.add((ts, actor, text))
            added += 1
    return created, added


def import_decisions(conn, path: Path):
    text = path.read_text(encoding="utf-8")
    parts = re.split(r"(?m)^## ", text)
    conn.execute("INSERT OR IGNORE INTO meta VALUES ('decisions_preamble', ?)", (parts[0],))
    n = 0
    for sec in parts[1:]:
        heading, _, rest = sec.partition("\n")
        date, sep, title = heading.partition(" -- ")
        if not sep:
            date, title = "", heading
        f = {}
        last = None
        for line in rest.splitlines():
            m = KEY_RE.match(line)
            if m:
                k, v = m.group(1).lower(), m.group(2)
                if k == "decided by":  # "Decided by: X. Reversible: Y."
                    v, _, rev = v.partition(". Reversible: ")
                    if rev:
                        f["reversible"] = rev
                f[k.replace(" ", "_")] = v
                last = k.replace(" ", "_")
            elif line.strip() and last:
                f[last] += "\n" + line.strip()
        if conn.execute("SELECT 1 FROM decisions WHERE date=? AND title=?", (date.strip(), title.strip())).fetchone():
            continue  # check first: INSERT OR IGNORE burns an AUTOINCREMENT id on every re-import
        cur = conn.execute(
            "INSERT INTO decisions (date, title, context, choice, alternatives, decided_by, reversible, raw, ts)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (date.strip(), title.strip(), f.get("context", ""), f.get("choice", ""), f.get("alternatives", ""),
             f.get("decided_by", ""), f.get("reversible", ""), "## " + sec.rstrip("\n"), now()))
        event(conn, "import", "decision", cur.lastrowid, "import", {"title": title.strip()})
        n += 1
    return n


def import_tips(conn, path: Path):
    text = path.read_text(encoding="utf-8")
    parts = re.split(r"(?m)^## ", text)
    conn.execute("INSERT OR IGNORE INTO meta VALUES ('tips_preamble', ?)", (parts[0],))
    n = 0
    for sec in parts[1:]:
        topic, _, rest = sec.partition("\n")
        items = []
        for line in rest.splitlines():
            if line.startswith("- "):
                items.append(line[2:])
            elif line.strip():
                if items:
                    items[-1] += "\n" + line.strip()
                else:
                    items.append(line.strip())
        for body in items:
            if conn.execute("SELECT 1 FROM tips WHERE topic=? AND body=?", (topic.strip(), body)).fetchone():
                continue
            cur = conn.execute("INSERT INTO tips (topic, body, ts) VALUES (?,?,?)", (topic.strip(), body, now()))
            if has_fts(conn):
                conn.execute("INSERT INTO tips_fts (rowid, topic, body) VALUES (?,?,?)",
                             (cur.lastrowid, topic.strip(), body))
            event(conn, "import", "tip", cur.lastrowid, "import", {"topic": topic.strip()})
            n += 1
    return n


def import_state(conn, state_dir: Path):
    state_dir = Path(state_dir)
    res = {"tasks": 0, "task_log_lines": 0, "decisions": 0, "tips": 0}
    with tx(conn):
        for p in sorted((state_dir / "tasks").glob("*.md")):
            created, added = import_task(conn, p)
            res["tasks"] += created
            res["task_log_lines"] += added
        if (state_dir / "decisions.md").exists():
            res["decisions"] = import_decisions(conn, state_dir / "decisions.md")
        if (state_dir / "tips.md").exists():
            res["tips"] = import_tips(conn, state_dir / "tips.md")
    return res
