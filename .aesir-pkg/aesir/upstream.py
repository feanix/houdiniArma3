"""Upstream channel: file an Aesir bug/friction report to the Aesir reports repo via the gh CLI."""
import json
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path

from . import proc
from . import db, gates, service as S

DEFAULT_REPO = "feanix/aesir-reports"
DEFAULT_NOTIFY = "aesir-advisor"  # clients message the Aesir side only via the advisor (D72); Aesir sets [upstream] notify
LOG_LINES = 200
KINDS = ("bug", "suggestion", "internal")  # labels on the reports repo; setup: docs/reporting.md
# The one place the severity scale is written in code; docs/reporting.md carries the prose copy. The --severity help
# is generated from this so the two cannot drift inside the code.
SEVERITIES = ("high", "medium", "low")
SEVERITY_SCALE = ("high = damaged work or state, or could; medium = blocks a step or forces a workaround every time; "
                  "low = annoyance, misleading display or wording")
SEVERITY_REQUIRED_KINDS = ("bug", "internal")  # suggestions may omit it
QUEUE_FILE = "upstream-queue.json"
FILED_FILE = "upstream-filed.json"
STOP = {"the", "and", "for", "not", "with", "when", "aesir", "was", "are", "has", "can"}


def project_head(root):
    """Short HEAD of the client project repo (separate from the Aesir version), else 'unknown'."""
    try:
        r = proc.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(root), capture_output=True,
                           text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def aesir_version():
    """Vendored .aesir-pkg/VERSION when running from a client, else package __version__, else git describe of
    the Aesir checkout holding this code, else 'unknown'."""
    import aesir
    pkg = Path(__file__).resolve().parent.parent
    if pkg.name == ".aesir-pkg":
        try:
            v = (pkg / "VERSION").read_text(encoding="utf-8").strip()
        except OSError:
            v = ""
        return v or "unknown"
    v = getattr(aesir, "__version__", None)
    if v:
        return str(v)
    try:
        r = proc.run(["git", "describe", "--always", "--tags", "--dirty"],
                           cwd=str(Path(__file__).resolve().parent), capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def config(root):
    try:
        return gates.load_config(root)
    except S.AesirError:
        return {}


def trim_log(text, n=LOG_LINES):
    lines = text.splitlines()
    if len(lines) <= n:
        return "\n".join(lines)
    return "\n".join([f"[... {len(lines) - n} earlier lines trimmed ...]"] + lines[-n:])


def dup_query(title):
    words = [w for w in re.findall(r"[A-Za-z0-9_]+", title) if w.lower() not in STOP and len(w) > 2]
    return " ".join(words[:8]) or title


def build_issue(body, repro, log_text, project, actor, version, symptom="", workaround="", head=""):
    parts = [body.strip(), ""]
    if symptom and symptom.strip():
        parts += ["## Symptom (exact error text)", "```", symptom.strip(), "```", ""]
    if repro:
        parts += ["## Repro", repro.strip(), ""]
    if workaround and workaround.strip():
        parts += ["## Workaround", workaround.strip(), ""]
    if log_text:
        parts += [f"## Log (last {LOG_LINES} lines max)", "```", trim_log(log_text), "```", ""]
    parts += ["## Context", f"- aesir version: {version}",
              f"- environment: {platform.platform()}, python {platform.python_version()}",
              f"- project: {project}" + (f" (project head: {head})" if head else ""), f"- reported by: {actor}"]
    return "\n".join(parts)


class GhFailed(S.AesirError):
    """gh ran but failed (down, auth, rate limit, timeout): a report can be queued and retried."""


def _gh(args):
    exe = shutil.which("gh")
    if not exe:
        raise S.AesirError("gh CLI not found on PATH (install GitHub CLI, or use --dry-run)")
    try:
        r = proc.run([exe, *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        raise GhFailed(f"gh {' '.join(args[:2])} failed: {e}")
    if r.returncode != 0:
        raise GhFailed(f"gh {' '.join(args[:2])} failed: {(r.stderr or r.stdout).strip()}")
    return r.stdout


def queue_path(db_file):
    """Offline queue lives next to the DB in use, like the watcher's seen-state."""
    return Path(db_file).parent / QUEUE_FILE


def _conn_file(conn):
    return conn.execute("PRAGMA database_list").fetchone()[2]


def load_queue(db_file):
    try:
        d = json.loads(queue_path(db_file).read_text(encoding="utf-8"))
        return d if isinstance(d, list) else []
    except (OSError, ValueError):
        return []


def save_queue(db_file, items):
    p = queue_path(db_file)
    if not items:
        p.unlink(missing_ok=True)
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def filed_path(db_file):
    """Generic ref -> url record of queued reports that were later filed, so callers holding a ref can resolve it."""
    return Path(db_file).parent / FILED_FILE


def filed_url(db_file, ref):
    """URL a queued report with this caller-chosen ref was filed under, or '' while it is still queued."""
    try:
        d = json.loads(filed_path(db_file).read_text(encoding="utf-8"))
        return d.get(ref, "") if isinstance(d, dict) else ""
    except (OSError, ValueError):
        return ""


def _note_filed(db_file, ref, url):
    p = filed_path(db_file)
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    d[ref] = url
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, indent=1), encoding="utf-8")


def pending_count(db_file):
    return len(load_queue(db_file))


def find_duplicates(repo, query):
    out = _gh(["issue", "list", "-R", repo, "--search", query, "--state", "all", "--limit", "5",
               "--json", "number,title,url,state"])
    try:
        return json.loads(out or "[]")
    except ValueError:
        return []


def ensure_label(repo, name):
    """Create a label on the reports repo if missing. 'already exists' is success, so this is safe to repeat and
    works for clients without repo admin once the label is there."""
    try:
        _gh(["label", "create", name, "-R", repo])
    except GhFailed as e:
        if "already exists" not in str(e).lower():
            raise


def _create(repo, title, body, kind, severity=None):
    args = ["issue", "create", "-R", repo, "--title", title, "--body", body]
    if kind:
        args += ["--label", kind]
    if severity:
        label = f"severity: {severity}"
        ensure_label(repo, label)  # before the issue, else gh issue create fails on the unknown label
        args += ["--label", label]
    lines = _gh(args).strip().splitlines()
    return lines[-1] if lines else ""


def _record(conn, actor, repo, title, url, tip, task):
    """Bookkeeping once an issue exists: event, tip link, task link."""
    with S.tx(conn):
        db.event(conn, actor, "upstream", url or title, "report", {"repo": repo, "title": title, "url": url})
    if tip is not None:
        S.add_link(conn, actor, "tip", tip, "upstream", url)
    if task is not None:
        S.add_link(conn, actor, "task", task, "upstream", url)


def report(conn, actor, title, body, repro="", log_path=None, workaround="", task=None, dry_run=False, root=None, symptom="",
           kind="bug", ref=None, severity=None):
    if not title.strip() or not body.strip():
        raise S.AesirError("--title and --body must not be empty")
    if kind not in KINDS:
        raise S.AesirError(f"--kind must be one of {', '.join(KINDS)}")
    if severity is not None and severity not in SEVERITIES:
        raise S.AesirError(f"--severity must be one of {', '.join(SEVERITIES)} ({SEVERITY_SCALE})")
    root = root or db.project_root()
    cfg = config(root)
    project = cfg.get("project", {}).get("name") or Path(root).name
    repo = cfg.get("upstream", {}).get("repo") or DEFAULT_REPO
    notify = cfg.get("upstream", {}).get("notify") or DEFAULT_NOTIFY
    log_text = Path(log_path).read_text(encoding="utf-8", errors="replace") if log_path else ""
    issue_body = build_issue(body, repro, log_text, project, actor, aesir_version(), symptom, workaround,
                             project_head(root))
    if severity:
        issue_body = f"Severity: {severity}.\n\n{issue_body}"
    query = dup_query(title)
    res = {"repo": repo, "title": title, "body": issue_body, "dup_query": query, "dry_run": bool(dry_run),
           "duplicates": [], "notify": notify, "kind": kind, "severity": severity}
    if dry_run:
        return res
    wa = (workaround or "").strip()
    tid = None
    if wa:
        tid = S.add_tip(conn, actor, wa, "upstream")
        if tid is None:
            row = conn.execute("SELECT id FROM tips WHERE topic='upstream' AND body=?", (wa,)).fetchone()
            tid = row[0] if row else None
        if tid is not None:
            res["tip"] = tid
    try:
        res["duplicates"] = find_duplicates(repo, query)
        res["url"] = _create(repo, title, issue_body, kind, severity)
    except GhFailed as e:
        db_file = _conn_file(conn)
        items = load_queue(db_file)
        items.append({"repo": repo, "title": title, "body": issue_body, "kind": kind, "severity": severity, "actor": actor,
                      "tip": tid, "task": task, "ref": ref, "error": str(e)})
        save_queue(db_file, items)
        res.update(queued=True, error=str(e), pending=len(items))
        return res
    _record(conn, actor, repo, title, res["url"], tid, task)
    return res


def retry_queue(conn, db_file, emit=print):
    """File queued reports; stop at the first gh failure. conn may be None (watch without a DB): the issue is
    still filed, bookkeeping is skipped. Returns the number filed."""
    items = load_queue(db_file)
    done = 0
    while items:
        it = items[0]
        try:
            url = _create(it["repo"], it["title"], it["body"], it.get("kind"), it.get("severity"))
        except GhFailed as e:
            emit(f"upstream: queued report retry failed: {e}")
            break
        if conn is not None:
            _record(conn, it.get("actor", "unknown"), it["repo"], it["title"], url, it.get("tip"), it.get("task"))
        emit(f"upstream: queued report filed: {url}")
        if it.get("ref"):
            _note_filed(db_file, it["ref"], url)
        items.pop(0)
        save_queue(db_file, items)
        done += 1
    return done


# ---- milestones (#228) ----------------------------------------------------------------------------------------------
# One GitHub milestone 'Aesir X.Y.Z' per planned release on the reports repo; every report linked to a task of that release
# gets it. Run by hand (`upstream sync-milestones`), dry-run unless --apply; never from a hook.

MILESTONE_PREFIX = "Aesir "


def _gh_json(args):
    """gh api output as a list; --paginate may print several JSON arrays back to back, which are concatenated."""
    text = (_gh(args) or "").strip()
    dec, i, items = json.JSONDecoder(), 0, []
    while i < len(text):
        v, j = dec.raw_decode(text, i)
        items += v if isinstance(v, list) else [v]
        i = j
        while i < len(text) and text[i].isspace():
            i += 1
    return items


def sync_milestones(conn, root, apply=False, repo=None):
    """Make the reports repo match the release plan; returns the action lines (done when apply, else only proposed)."""
    from . import releases
    repo = repo or config(root).get("upstream", {}).get("repo") or DEFAULT_REPO
    plan = releases.plan(conn, releases.tagged_versions(root))
    reports = releases.task_reports(conn, repo)
    want, desc = {}, {}  # issue number -> version (earliest planned release wins); version -> description text
    for rel in plan["releases"]:
        v = rel["version"]
        desc[v] = "Planned tasks: " + ", ".join(f"#{t['id']}" for t in rel["tasks"])
        for t in rel["tasks"]:
            for n in reports.get(t["id"], []):
                want.setdefault(n, v)
    existing = {m["title"]: m for m in _gh_json(["api", f"repos/{repo}/milestones", "-X", "GET", "-F", "state=all", "-F", "per_page=100", "--paginate"])}
    actions, numbers = [], {}
    for rel in plan["releases"]:
        v = rel["version"]
        title = MILESTONE_PREFIX + v
        m = existing.get(title)
        if m is None:
            actions.append(f"create milestone '{title}'")
            if apply:
                m = _gh_json(["api", f"repos/{repo}/milestones", "-f", f"title={title}", "-f", f"description={desc[v]}"])[0]
        elif (m.get("description") or "") != desc[v]:
            actions.append(f"update milestone '{title}' description")
            if apply:
                _gh(["api", "-X", "PATCH", f"repos/{repo}/milestones/{m['number']}", "-f", f"description={desc[v]}"])
        numbers[v] = m["number"] if m else None
    for n, v in sorted(want.items()):
        cur = (_gh_json(["api", f"repos/{repo}/issues/{n}"])[0].get("milestone") or {}).get("title")
        if cur == MILESTONE_PREFIX + v:
            continue
        actions.append(f"set #{n} milestone '{MILESTONE_PREFIX}{v}'" + (f" (was '{cur}')" if cur else ""))
        if apply:
            _gh(["api", "-X", "PATCH", f"repos/{repo}/issues/{n}", "-F", f"milestone={numbers[v]}"])
    return actions
