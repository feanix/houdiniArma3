"""Newer Aesir releases for a client: list tags from the recorded source, cached notice for status/resume.

The source (a local checkout path or a git remote URL) is recorded in aesir.toml `[aesir] source` by install/upgrade.
`[aesir] release_check_hours` (default 1) is the cache interval of the status/resume check, which never fails a command.

It also owns the release plan (#228): tasks carry a target release (tasks.target_release); `plan` derives the unreleased
versions ascending with their tasks in task_after order, plus missed (open in a tagged release) and unplaced tasks;
`render_plan`/`write_roadmap` render it as the generated ROADMAP.md that every default state export writes."""
import json
import re
import subprocess
import time
from pathlib import Path

from . import proc
from . import db, gates, install
from .service import AesirError

DEFAULT_HOURS = 1
TIMEOUT = 8
TAG = re.compile(r"^v(\d+\.\d+\.\d+)$")


def _config(root):
    try:
        return gates.load_config(root).get("aesir", {})
    except AesirError:
        return {}


def source_of(root):
    s = _config(root).get("source")
    return s if isinstance(s, str) and s else None


def record_source(client, source):
    """Add `[aesir] source` to the client's aesir.toml unless one is already there (never overwrites)."""
    p = Path(client) / "aesir.toml"
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    if source_of(client):
        return False
    s = str(source)
    if Path(s).exists():
        s = Path(s).resolve().as_posix()
    line = f"source = {json.dumps(s)}\n"
    m = re.search(r"^\[aesir\][ \t]*\r?\n", text, re.M)
    if m:
        text = text[:m.end()] + line + text[m.end():]
    else:
        text = text + ("" if not text or text.endswith("\n") else "\n") + ("\n" if text else "") + "[aesir]\n" + line
    p.write_text(text, encoding="utf-8", newline="\n")
    return True


def _vkey(v):
    return tuple(int(x) for x in v.split("."))


def _headlines(text):
    """{version: first body line} from CHANGELOG '## X.Y.Z (date)' sections."""
    out, cur = {}, None
    for ln in text.splitlines():
        m = re.match(r"^##\s+(\d+\.\d+\.\d+)\b", ln)
        if m:
            cur = m.group(1)
        elif cur and ln.strip() and cur not in out:
            out[cur] = ln.strip()
    return out


def _run(args):
    try:
        r = proc.run(["git", *args], capture_output=True, timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        raise AesirError(f"git {args[0]} failed: {e}")
    if r.returncode:
        raise AesirError(f"git {args[0]} failed: {r.stderr.decode(errors='replace').strip()}")
    return r.stdout.decode(errors="replace")


def fetch(source):
    """[(version, headline)] newest first. A local checkout also gives CHANGELOG headlines; a remote only tags."""
    local = Path(source).is_dir()
    if local:
        if not (Path(source) / ".git").exists():
            raise AesirError(f"{source} is not a git checkout")
        names = _run(["-C", str(source), "tag", "--list", "v*"]).split()
    else:
        names = [ln.split("\t")[1].rpartition("/")[2] for ln in
                 _run(["ls-remote", "--tags", "--refs", str(source)]).splitlines() if "\t" in ln]
    versions = sorted({m.group(1) for n in names if (m := TAG.match(n))}, key=_vkey, reverse=True)
    heads = {}
    if local and versions:
        try:
            heads = _headlines(_run(["-C", str(source), "show", f"v{versions[0]}:CHANGELOG.md"]))
        except AesirError:
            pass
    return [(v, heads.get(v, "")) for v in versions]


def listing(root):
    """(source, pinned, [(version, headline)]) for `aesir releases`."""
    root = Path(root)
    pinned = install.installed_version(root)
    source = source_of(root)
    if not source and not pinned and (install.default_source() / ".git").exists():
        source = str(install.default_source())
    if not source:
        raise AesirError("no release source recorded (aesir.toml [aesir] source); "
                         "run 'upgrade --version X --from <source>'")
    return source, pinned, fetch(source)


def classify(pinned, latest):
    """Notice text when `latest` is newer than `pinned`, else None."""
    try:
        if _vkey(latest) <= _vkey(pinned):
            return None
        kind = "same-major" if _vkey(latest)[0] == _vkey(pinned)[0] else "MAJOR"
    except (ValueError, TypeError):
        return None
    return f"Aesir {latest} available (pinned {pinned}; {kind})"


def _cache_file(root, db_arg=None):
    return db.db_path(db_arg, root).parent / "releases-cache.json"


def notice(root, db_arg=None, now=None):
    """Cached (release_check_hours) update notice or None. Never raises: an unreachable source means no new notice."""
    try:
        root = Path(root)
        pinned = install.installed_version(root)
        source = source_of(root)
        if not pinned or not source:
            return None
        hours = _config(root).get("release_check_hours", DEFAULT_HOURS)
        if isinstance(hours, bool) or not isinstance(hours, (int, float)) or hours < 0:
            hours = DEFAULT_HOURS
        now = time.time() if now is None else now
        f = _cache_file(root, db_arg)
        try:
            c = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            c = {}
        if not isinstance(c, dict) or c.get("source") != source:
            c = {}
        if not c or now - c.get("checked", 0) >= hours * 3600:
            try:
                found = fetch(source)
                c = {"source": source, "checked": now, "latest": found[0][0] if found else None}
            except AesirError:  # keep the last known answer; retry after the interval
                c = {"source": source, "checked": now, "latest": c.get("latest")}
            try:
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(json.dumps(c), encoding="utf-8")
            except OSError:
                pass
        return classify(pinned, c["latest"]) if c.get("latest") else None
    except Exception:
        return None


# ---- release plan (#228) ------------------------------------------------------------------------------------------
# Tasks carry a target release (tasks.target_release). The plan is derived, never stored: unreleased versions ascending,
# each with its tasks in task_after order. ROADMAP.md is rendered from it by every state export.

DONE = ("merged", "verified")
CLOSED = DONE + ("wontfix",)
ROADMAP_FILE = "ROADMAP.md"
ROADMAP_HEADER = ("Generated from the Aesir database, do not edit. A plan, not a promise: it changes, and decisions "
                  "record why.")


def tagged_versions(root):
    """Versions already released: git tags vX.Y.Z in the project repo plus CHANGELOG '## X.Y.Z' headings (a release record)."""
    found = set()
    try:
        r = proc.run(["git", "tag", "--list", "v*"], cwd=str(root), capture_output=True, timeout=TIMEOUT)
        if r.returncode == 0:
            found |= {m.group(1) for n in r.stdout.decode(errors="replace").split() if (m := TAG.match(n))}
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        found |= set(_headlines((Path(root) / "CHANGELOG.md").read_text(encoding="utf-8")))
    except OSError:
        pass
    return found


def _ordered(conn, tasks):
    """Tasks of one release in task_after order (predecessors first, ties by id); links to other releases are ignored."""
    ids = {t["id"] for t in tasks}
    after = {t["id"]: set() for t in tasks}
    for r in conn.execute("SELECT task_id, after_id FROM task_after"):
        if r["task_id"] in ids and r["after_id"] in ids:
            after[r["task_id"]].add(r["after_id"])
    by_id = {t["id"]: t for t in tasks}
    out, left = [], set(ids)
    while left:
        ready = sorted(i for i in left if not (after[i] & left))
        i = ready[0] if ready else min(left)  # a cycle cannot be linked, but never loop on bad data
        out.append(by_id[i])
        left.discard(i)
    return out


def plan(conn, tagged=()):
    """{'releases': [{version, ready_to_tag, tasks}], 'missed': [...], 'unplaced': [...]}; `tagged` = versions already released."""
    rows = [dict(r) for r in conn.execute("SELECT id, title, status, target_release FROM tasks ORDER BY id")]
    tagged = set(tagged)
    groups, missed, unplaced = {}, [], []
    for t in rows:
        v = t.pop("target_release")
        if not v:
            if t["status"] not in CLOSED:
                unplaced.append(t)
        elif v in tagged:
            if t["status"] not in CLOSED:  # still open in a release that shipped without it
                missed.append({**t, "release": v})
        else:
            groups.setdefault(v, []).append(t)
    releases = []
    for v in sorted(groups, key=_vkey):
        ts = _ordered(conn, groups[v])
        releases.append({"version": v, "tasks": ts,
                         "ready_to_tag": all(t["status"] in CLOSED for t in ts) and any(t["status"] in DONE for t in ts)})
    return {"releases": releases, "missed": missed, "unplaced": unplaced}


def task_reports(conn, repo=None):
    """{task id: [upstream issue numbers]} from task->upstream links to the reports repo `repo` (OWNER/NAME).
    A bare number counts as that repo; a URL to another repo is ignored. repo=None: no repo filter."""
    out = {}
    for r in conn.execute("SELECT a_id, b_id FROM links WHERE a_type='task' AND b_type='upstream'"):
        m = re.search(r"/issues/(\d+)$", r["b_id"]) or re.fullmatch(r"(\d+)", r["b_id"])
        if m and repo and "/" in r["b_id"]:
            u = re.search(r"github\.com/([^/]+/[^/]+)/issues/\d+$", r["b_id"])
            if not u or u.group(1).lower() != repo.lower():
                continue
        if m and r["a_id"].isdigit():
            out.setdefault(int(r["a_id"]), set()).add(int(m.group(1)))
    return {k: sorted(v) for k, v in out.items()}


def _task_line(t, reports):
    rep = reports.get(t["id"])
    return f"- #{t['id']:03d} [{t['status']}] {t['title']}" + (f" (reports: {', '.join('#' + str(n) for n in rep)})" if rep else "")


def render_plan(p, reports):
    """Deterministic ROADMAP.md text: no timestamps, so an unchanged plan is byte-identical."""
    out = ["# Roadmap", "", ROADMAP_HEADER, ""]
    for rel in p["releases"]:
        out += [f"## {rel['version']}" + (" (ready to tag)" if rel["ready_to_tag"] else ""), ""]
        out += [_task_line(t, reports) for t in rel["tasks"]] + [""]
    if p["missed"]:
        out += ["## Missed", ""] + [f"{_task_line(t, reports)} (missed {t['release']})" for t in p["missed"]] + [""]
    if p["unplaced"]:
        out += ["## Unplaced", ""] + [_task_line(t, reports) for t in p["unplaced"]] + [""]
    return "\n".join(out)


def reports_repo(root):
    from . import upstream  # late: upstream imports this module lazily too
    return upstream.config(root).get("upstream", {}).get("repo") or upstream.DEFAULT_REPO


def write_roadmap(conn, root):
    """Write <root>/ROADMAP.md from the current plan (called by every state export). Returns the path, or None when
    a hand-written ROADMAP.md (not starting with the generated header) is there: it is left untouched."""
    text = render_plan(plan(conn, tagged_versions(root)), task_reports(conn, reports_repo(root)))
    path = Path(root) / ROADMAP_FILE
    if path.exists():
        head = path.read_bytes().decode("utf-8", errors="replace").replace("\r\n", "\n")
        if not head.startswith(f"# Roadmap\n\n{ROADMAP_HEADER}\n"):
            return None
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return path


def format_plan(p):
    """Text lines of `release plan`."""
    lines = []
    for rel in p["releases"]:
        lines.append(f"{rel['version']}" + ("  (ready to tag)" if rel["ready_to_tag"] else ""))
        lines += [f"  #{t['id']:03d}  {t['status']:<17}  {t['title']}" for t in rel["tasks"]]
    if p["missed"]:
        lines.append("missed (open in a tagged release; move with task set N --release X.Y.Z)")
        lines += [f"  #{t['id']:03d}  {t['status']:<17}  missed {t['release']}  {t['title']}" for t in p["missed"]]
    if p["unplaced"]:
        lines.append("unplaced")
        lines += [f"  #{t['id']:03d}  {t['status']:<17}  {t['title']}" for t in p["unplaced"]]
    return lines or ["(no planned releases)"]
