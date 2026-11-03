"""Tips in use (task #56): surfacing by context, impression tracking, promotion into core rules; and the brief's
Related section (#157): bounded pointers to prior decisions, tasks and design docs matching a task's subject.
A tip's tags (plus its topic) say where it applies: an agent type (implementer), a track name, a scope path
(aesir/), a command tag (tasks, commits), or free words. Surfaced tips print as pointers (id + one line)."""
import os
import re
from pathlib import Path

from . import db, friction, gates, tracks
from .invoke import cmd as client_cmd
from .rolenames import agent_role
from .db import event, now, tx
from .service import AesirError, CORE_SECTIONS, SCOPES, _retire_tip, _tip, propose_core, set_core

DEFAULTS = {"surface_n": 5, "promote_min": 3}
LINE_MAX = 110


def load_config(root):
    """[tips] in aesir.toml: surface_n (tips per agent context), promote_min (impressions before a still-recurring
    linked friction makes the tip a promotion candidate)."""
    try:
        sec = gates.load_config(root).get("tips", {})
    except (AesirError, OSError, ValueError):
        sec = {}
    cfg = {**DEFAULTS, **sec}
    for k in DEFAULTS:
        if isinstance(cfg[k], bool) or not isinstance(cfg[k], int) or cfg[k] < 1:
            raise AesirError(f"bad aesir.toml: [tips] {k} must be an integer >= 1")
    return cfg


def pointer(t):
    """One line: id, topic, start of the text (cut)."""
    first = " ".join(t["body"].split())
    if len(first) > LINE_MAX:
        first = first[:LINE_MAX - 3].rstrip() + "..."
    return f"T{t['id']}" + (f" [{t['topic']}]" if t["topic"] else "") + f" {first}"


def _active(conn):
    return [dict(r) for r in conn.execute("SELECT id, topic, body, tags FROM tips WHERE retired='' ORDER BY id")]


def _tagset(t):
    return {x for x in t["tags"].split(",") if x} | ({t["topic"].lower()} if t["topic"] else set())


def _path_match(tag, pat):
    """A path-like tag matches a scope entry when one is a prefix of the other ('aesir/' ~ 'aesir/db.py')."""
    if not ("/" in tag or "/" in pat):
        return False
    a, b = tag.rstrip("*/").replace("\\", "/"), pat.rstrip("*/").replace("\\", "/")
    return bool(a) and bool(b) and (a.startswith(b) or b.startswith(a))


def _friction_recency(conn):
    """{tip id: newest friction record event id for a key currently linked to that tip}."""
    links = {}
    for r in conn.execute("SELECT entity_id, json_extract(payload,'$.target') t FROM events "
                          "WHERE entity='friction' AND action='link' ORDER BY id"):
        links[r["entity_id"]] = r["t"] or ""
    out = {}
    for key, tgt in links.items():
        m = re.match(r"tip:(\d+)$", tgt)
        if m:
            last = conn.execute("SELECT MAX(id) FROM events WHERE entity='friction' AND action='record' AND "
                                "json_extract(payload,'$.key')=?", (key,)).fetchone()[0]
            out[int(m.group(1))] = max(out.get(int(m.group(1)), 0), last or 0)
    return out


def match(conn, terms, scope="", limit=5):
    """Top `limit` active tips for terms (agent type, track, words) and scope paths, best first: more matching
    tags, then more recent linked friction, then newer tip. Tips matching nothing are never returned."""
    terms = {x.lower() for x in terms if x}
    pats = [p.strip() for p in re.split(r"[,\s]+", scope or "") if p.strip()]
    rec = _friction_recency(conn)
    ranked = []
    for t in _active(conn):
        tags = _tagset(t)
        score = len(tags & terms) + sum(1 for g in tags if any(_path_match(g, p) for p in pats))
        if score:
            ranked.append((-score, -rec.get(t["id"], 0), -t["id"], t))
    return [r[3] for r in sorted(ranked, key=lambda r: r[:3])[:limit]]


def impress(conn, tips, agent="", task=None):
    """Record one impression per tip (tip, agent id, task, ts)."""
    if not tips:
        return
    with tx(conn):
        for t in tips:
            conn.execute("INSERT INTO tip_impressions (tip_id, agent_id, task_id, ts) VALUES (?,?,?,?)",
                         (t["id"], agent or "", task, now()))


def _surface_n(root):
    try:
        return load_config(root)["surface_n"]
    except AesirError:
        return DEFAULTS["surface_n"]


def agent_lines(conn, row, task, root, track=""):
    """Context lines for SubagentStart: pointers to the top tips for this agent's task. Records impressions."""
    terms = [agent_role(row), track] + re.findall(r"[a-z0-9_-]+", task["title"].lower())
    got = match(conn, terms, tracks.effective_scope(task, root), _surface_n(root))
    impress(conn, got, row["agent_id"], task["id"])
    if not got:
        return []
    return [client_cmd("Tips (full text: `python -m aesir tip search <words>`):")] + [f"  {pointer(t)}" for t in got]


# ---- related prior work (#157) -----------------------------------------------------------------------------------
RELATED_MAX = 5  # lines in the brief's Related section: bounded cost, pointers only
RELATED_MIN_SCORE = 4  # a title hit counts 2, a body hit 1: one stray body word is not a match
DOC_BYTES = 200_000  # a design doc is scanned up to this size, never pasted
_STOP = frozenset("""about after again also and are because been before being but can cannot does each for from has have
into its not off only other out over per should some such than that the their then there these they this those use used
uses using via was were what when where which while will with without would your task tasks add adds new make makes
test spec scope criterion acceptance source aesir py md docs claude change brief include match name one line lines code
project owner session lead agent file files work run read before first same any all not get set see key list help way
need needs like must may more most just now two three both either else still yet very well case cases top within""".split())


def _words(text):
    """Lowercase word tokens, trailing plural s dropped (decisions ~ decision)."""
    return [_stem(w) for w in re.findall(r"[a-z0-9_]{3,}", (text or "").lower())]


def _stem(w):
    if len(w) <= 4 or not w.endswith("s") or w.endswith(("ss", "is", "us")):
        return w
    return w[:-2] if w.endswith(("ches", "shes", "xes", "sses")) else w[:-1]


def related_terms(task, root):
    """Search words for a task: its title and spec words plus the stems of its scope paths (agenthook.py ->
    agenthook), so a task is found by what it is about and where it works, not by one title word."""
    words = _words(task["title"]) + _words(task["spec"])
    for pat in re.split(r"[,\s]+", tracks.effective_scope(task, root) or ""):
        words += _words(re.sub(r"\.[A-Za-z0-9]+$", "", pat.replace("\\", "/").rstrip("*/").rsplit("/", 1)[-1]))
    seen = []
    for w in words:
        if w not in _STOP and not w.isdigit() and w not in seen:
            seen.append(w)
    return seen[:40]


DOC_BODY_CAP = 3  # a long generic doc matches many words by size alone: its body counts at most this much


def _score(title, body, terms, body_cap=None):
    t, b = set(_words(title)), set(_words(body))
    hits = len((b - t) & set(terms))
    return 2 * len(t & set(terms)) + (hits if body_cap is None else min(hits, body_cap))


def _gist(text, terms, width=90):
    """The first line of `text` that holds a search term (else the first non-empty line), cut to width."""
    lines = [" ".join(x.split()) for x in (text or "").splitlines() if x.strip()]
    best = next((x for x in lines if set(_words(x)) & set(terms)), lines[0] if lines else "")
    best = best.lstrip("#-* ").strip()
    return best if len(best) <= width else best[:width - 3].rstrip() + "..."


def _doc_dirs(root):
    try:
        v = gates.load_config(root).get("project", {}).get("design_docs", ["docs"])
    except (AesirError, OSError, ValueError):
        v = ["docs"]
    return [v] if isinstance(v, str) else [x for x in v if isinstance(x, str)]


def related(conn, task, root, limit=RELATED_MAX):
    """Top `limit` prior decisions, other tasks and design/research docs (docs/*.md, or the dirs in [project]
    design_docs) matching the task's subject and scope: [(score, label, title, one line)] best first. Reads the
    stored rows and files; pastes nothing but a one-line gist per match."""
    terms = related_terms(task, root)
    if not terms:
        return []
    out = []
    for r in conn.execute("SELECT id, title, context, choice FROM decisions"):
        sc = _score(r["title"], f"{r['context']} {r['choice']}", terms)
        if sc >= RELATED_MIN_SCORE:
            out.append((sc, f"D{r['id']}", r["title"], _gist(r["choice"] or r["context"], terms)))
    for r in conn.execute("SELECT id, title, spec FROM tasks WHERE id != ?", (task["id"],)):
        sc = _score(r["title"], r["spec"], terms)
        if sc >= RELATED_MIN_SCORE:
            out.append((sc, f"#{r['id']:03d}", r["title"], _gist(r["spec"], terms)))
    for d in _doc_dirs(root):
        for f in sorted((Path(root) / d).glob("*.md")):
            try:
                body = f.read_text(encoding="utf-8", errors="replace")[:DOC_BYTES]
            except OSError:
                continue
            head = next((x.lstrip("# ").strip() for x in body.splitlines() if x.startswith("#")), f.stem)
            sc = _score(f"{f.stem} {head}", body, terms, DOC_BODY_CAP)
            if sc >= RELATED_MIN_SCORE:
                out.append((sc, f"{d}/{f.name}", head, _gist(body, terms)))
    return sorted(out, key=lambda x: (-x[0], x[1]))[:limit]


def related_lines(conn, task, root):
    """The brief's Related section (#157): pointers to prior decisions, tasks and design docs on this subject, with
    the rule to read and cite them. Empty when nothing matches."""
    got = related(conn, task, root)
    if not got:
        return []
    return [client_cmd("Related (read and cite before a diagnosis or fix; search by subject before saying work is "
                       "missing: `python -m aesir decision list`, `task list`, `tip search <words>`, docs/*.md):")] + [
        f"  {label} {title} -- {gist}" if gist and gist != title else f"  {label} {title}"
        for _, label, title, gist in got]


def print_for(conn, a, tag, task=None):
    """Print the tips tagged `tag` (a command's own tag, e.g. tasks, commits) as pointers; record impressions."""
    try:
        n = _surface_n(db.project_root())
    except Exception:
        n = DEFAULTS["surface_n"]
    got = match(conn, [tag], "", n)
    impress(conn, got, getattr(a, "actor", "") or os.environ.get("AESIR_AGENT", ""), task)
    for t in got:
        print(f"[{tag}] {pointer(t)}")


def promote(conn, actor, tid, role, section, scope="project", reason="", key=""):
    """Move a tip into a core row and retire it with a link. owner scope, or an owner-rule section, goes through
    a core proposal (the advisor relays the owner's approval); the tip retires when that is approved.
    Returns ('core', key) or ('proposal', id)."""
    if scope not in SCOPES:
        raise AesirError(f"scope must be one of {', '.join(SCOPES)}")
    if section not in CORE_SECTIONS:
        raise AesirError(f"unknown core section: {section} (use {', '.join(CORE_SECTIONS)})")
    t = _tip(conn, tid)
    reason = reason or f"promoted from tip T{t['id']}"
    key = key or f"tip-{t['id']}"
    if scope == "owner" or CORE_SECTIONS[section] == "owner":
        pid = propose_core(conn, actor, role, section, t["body"], reason, key, scope)
        with tx(conn):
            conn.execute("INSERT OR IGNORE INTO links VALUES ('tip',?,'core_proposal',?)", (str(t["id"]), str(pid)))
            event(conn, actor, "tip", t["id"], "promote", {"to": f"core_proposal:{pid}"})
        return "proposal", pid
    set_core(conn, actor, role, section, t["body"], reason, key, scope)
    with tx(conn):
        row = conn.execute("SELECT id FROM core WHERE role=? AND section=? AND key=?", (role, section, key)).fetchone()
        _retire_tip(conn, actor, t["id"], "core", row["id"])
    return "core", key


def promote_hook(conn, actor, tid, name):
    """Retire a tip that a hook check now enforces; `name` (the check) is kept as the link."""
    if not (name or "").strip():
        raise AesirError("name the hook check")
    t = _tip(conn, tid)
    with tx(conn):
        _retire_tip(conn, actor, t["id"], "hook", name)


def report(conn, cfg=None, fcfg=None):
    """For metrics/status: tips never surfaced; surfaced tips whose linked friction still recurs; and, of those,
    the promotion candidates (>= promote_min impressions)."""
    cfg = cfg or DEFAULTS
    imp = {r[0]: r[1] for r in conn.execute("SELECT tip_id, COUNT(*) FROM tip_impressions GROUP BY tip_id")}
    active = _active(conn)
    live = {t["id"] for t in active}
    still = []
    for x in friction.recurring(conn, fcfg):
        m = re.match(r"tip:(\d+)$", x["link"])
        tid = int(m.group(1)) if m else 0
        if tid in live and not x["stopped"] and imp.get(tid):
            still.append({"tip": tid, "key": x["key"], "friction_count": x["count"], "impressions": imp[tid],
                          "candidate": imp[tid] >= cfg["promote_min"]})
    return {"total": len(active), "surfaced": sum(1 for t in active if imp.get(t["id"])),
            "never_surfaced": [t["id"] for t in active if not imp.get(t["id"])],
            "still_recurring": still, "candidates": [s for s in still if s["candidate"]]}


def render(d):
    print(f"tips: {d['total']} active, {d['surfaced']} surfaced, {len(d['never_surfaced'])} never surfaced"
          + (": " + ", ".join(f"T{i}" for i in d["never_surfaced"][:10]) if d["never_surfaced"] else ""))
    for s in d["still_recurring"]:
        print(f"  T{s['tip']} surfaced x{s['impressions']}, friction {s['key']} still recurring (x{s['friction_count']})"
              + (" -- promotion candidate" if s["candidate"] else ""))
