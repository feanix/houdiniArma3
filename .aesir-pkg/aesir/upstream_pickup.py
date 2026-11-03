"""Upstream pickup: watch the Aesir reports repo for changes, link issues to tasks, close the loop on release."""
import json
import os
import re
import subprocess
import time
from pathlib import Path

from . import proc
from . import db, service as S, upstream

POLL_SECONDS = 60
LIMIT_SECONDS = 25 * 60  # Monitor's own limit is 30 min; leave margin
FIELDS = "number,title,state,labels,updatedAt,comments"
# Aesir-written issue comments carry a fixed prefix; all accounts are one GitHub user, so author cannot tell.
AESIR_COMMENT_RE = re.compile(r"\s*(Triage|Reply|Release|Close) \(aesir-[\w-]+\):", re.I)
FIXED_RE = re.compile(r"fixed in (v\d+\.\d+\.\d+)", re.I)


def seen_path(root, db_arg=None):
    """Seen-state lives next to the DB in use (--db / AESIR_DB / project default), so a temp DB never reads the
    live project's cache."""
    return db.db_path(db_arg, root).parent / "upstream-seen.json"


def load_seen(root, db_arg=None):
    try:
        d = json.loads(seen_path(root, db_arg).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_seen(root, seen, db_arg=None):
    p = seen_path(root, db_arg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(seen, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)


def repo_of(root):
    return upstream.config(root).get("upstream", {}).get("repo") or upstream.DEFAULT_REPO


def fetch(repo):
    """One gh call: every issue with state, labels and comments."""
    out = upstream._gh(["issue", "list", "-R", repo, "--state", "all", "--limit", "200", "--json", FIELDS])
    try:
        data = json.loads(out or "[]")
    except ValueError:
        raise S.AesirError("gh returned unparseable JSON")
    return data if isinstance(data, list) else []


def is_aesir_comment(c):
    return bool(AESIR_COMMENT_RE.match(c.get("body", "") if isinstance(c, dict) else str(c or "")))


def _snap(i):
    return {"title": i.get("title", ""), "state": str(i.get("state", "")).upper(),
            "labels": sorted(x.get("name", "") if isinstance(x, dict) else str(x) for x in i.get("labels") or []),
            "comments": sum(1 for c in i.get("comments") or [] if not is_aesir_comment(c)), "updatedAt": i.get("updatedAt", "")}


def diff(seen, issues):
    """(lines, new_seen). First run (no seen state) announces open issues only."""
    old = seen.get("issues")
    first = old is None
    old = old or {}
    lines, new = [], {}
    for i in sorted(issues, key=lambda x: x.get("number", 0)):
        n = str(i.get("number"))
        s = new[n] = _snap(i)
        p = old.get(n)
        tag = f'#{n} "{s["title"]}"'
        if p is None:
            if not first or s["state"] == "OPEN":
                lines.append(f"upstream: new issue {tag} [{s['state']}]")
            continue
        if s["comments"] > p.get("comments", 0):
            lines.append(f"upstream: {s['comments'] - p.get('comments', 0)} new comment(s) on {tag}")
        if s["state"] != p.get("state"):
            lines.append(f"upstream: {tag} state {p.get('state')} -> {s['state']}")
        if s["labels"] != p.get("labels"):
            lines.append(f"upstream: {tag} labels {','.join(p.get('labels') or []) or '-'} -> {','.join(s['labels']) or '-'}")
    return lines, {**seen, "issues": new}


def poll(root, repo, db_arg=None):
    seen = load_seen(root, db_arg)
    lines, new = diff(seen, fetch(repo))
    save_seen(root, new, db_arg)
    return lines


def retry_pending(root, db_arg, emit):
    """File reports queued while gh was down. Uses the DB for bookkeeping when it exists."""
    path = db.db_path(db_arg, root)
    if not upstream.load_queue(path):
        return 0
    conn = db.connect(path, migrate_db=False) if Path(path).exists() else None
    try:
        return upstream.retry_queue(conn, path, emit)
    finally:
        if conn is not None:
            conn.close()


def watch(root, once=False, poll_seconds=POLL_SECONDS, limit_seconds=LIMIT_SECONDS, emit=None, db_arg=None):
    emit = emit or (lambda s: print(s, flush=True))
    repo = repo_of(root)
    start = time.monotonic()
    last_err = None
    while True:
        try:
            retry_pending(root, db_arg, emit)
            for ln in poll(root, repo, db_arg):
                emit(ln)
            last_err = None
        except (S.AesirError, OSError, subprocess.SubprocessError, ValueError) as e:  # hung gh, bad JSON
            if str(e) != last_err:  # say it once, not every minute
                emit(f"watch: poll failed: {e}")
            last_err = str(e)
        if once:
            return
        if time.monotonic() - start + poll_seconds >= limit_seconds:
            emit("watch: restart (time limit)")
            return
        time.sleep(poll_seconds)


def issue_number(ref):
    m = re.search(r"(\d+)\s*$", str(ref))
    if not m:
        raise S.AesirError(f"not an issue number or url: {ref}")
    return int(m.group(1))


def issue_url(repo, n):
    return f"https://github.com/{repo}/issues/{n}"


def triage_body(actor, task, status, workaround=""):
    return (f"Triage (aesir-{actor}):\n- task: #{task}\n- status: {status}\n"
            f"- workaround: {(workaround or '').strip() or 'none yet'}")


def reply_body(actor, text):
    return f"Reply (aesir-{actor}):\n{text.strip()}"


def comment(root, issue, body):
    n = issue_number(issue)
    upstream._gh(["issue", "comment", str(n), "-R", repo_of(root), "--body", body])
    return n


def link(conn, actor, root, issue, task, workaround="", post=True):
    """Link issue to task and post the triage comment (task, status, workaround) on the issue. A comment failure
    raises after the link is recorded."""
    row = conn.execute("SELECT status FROM tasks WHERE id=?", (task,)).fetchone()
    if not row:
        raise S.AesirError(f"no task {task}")
    n = issue_number(issue)
    url = issue_url(repo_of(root), n)
    S.add_link(conn, actor, "task", task, "upstream", url)
    if post:
        comment(root, n, triage_body(actor, task, row[0], workaround))
    return url


def search(root, words, limit=10, kind=None):
    """Reports (open and closed) matching words: title, state, linked task and latest workaround, read from
    Aesir-prefixed comments on the issue. Needs no database, so any project or role can use it."""
    q = " ".join(words).strip()
    if not q:
        raise S.AesirError("search needs words")
    args = ["issue", "list", "-R", repo_of(root), "--search", q, "--state", "all", "--limit", str(limit),
            "--json", "number,title,state,url,comments"]
    if kind:
        args += ["--label", kind]
    raw = upstream._gh(args)
    try:
        data = json.loads(raw or "[]")
    except ValueError:
        raise S.AesirError("gh returned unparseable JSON")
    res = []
    for i in data if isinstance(data, list) else []:
        task, wa = "", ""
        for c in i.get("comments") or []:
            b = c.get("body", "") if isinstance(c, dict) else ""
            if not AESIR_COMMENT_RE.match(b):
                continue
            m = re.search(r"^- task:\s*(#\d+)", b, re.M)
            if m:
                task = m.group(1)
            m = re.search(r"^- workaround:\s*(.+)$", b, re.M)
            if m:
                wa = "" if m.group(1).strip().lower().startswith("none") else m.group(1).strip()
        res.append({"issue": i.get("number"), "title": i.get("title", ""), "state": str(i.get("state", "")).upper(),
                    "url": i.get("url", ""), "task": task, "workaround": wa})
    return res


def linked_numbers(conn):
    out = set()
    for r in conn.execute("SELECT b_id FROM links WHERE a_type='task' AND b_type='upstream'"):
        m = re.search(r"/issues/(\d+)$", r[0]) or re.fullmatch(r"(\d+)", r[0])
        if m:
            out.add(int(m.group(1)))
    return out


def unlinked_open(conn, root, db_arg=None):
    """Open issues from the watcher's cache that no task links to (no gh call)."""
    linked = linked_numbers(conn)
    return [(int(n), s["title"]) for n, s in sorted(load_seen(root, db_arg).get("issues", {}).items(), key=lambda x: int(x[0]))
            if s.get("state") == "OPEN" and int(n) not in linked]


def _tag_times(root):
    """{tag: epoch seconds} for every vX.Y.Z tag; {} when there is no git."""
    try:
        r = proc.run(["git", "-C", str(root), "tag", "--list", "v*",
                            "--format=%(refname:strip=2) %(creatordate:unix)"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return {}
    out = {}
    for line in r.stdout.splitlines() if r.returncode == 0 else []:
        name, _, ts = line.partition(" ")
        if re.fullmatch(r"v\d+\.\d+\.\d+", name) and ts.strip().isdigit():
            out[name] = int(ts)
    return out


def _prev_tag_time(root, tag):
    """Epoch seconds of the newest vX.Y.Z tag other than `tag`; None when there is none (or no git)."""
    times = [t for n, t in _tag_times(root).items() if n != tag]
    return max(times) if times else None


def _epoch(ts):
    import calendar
    return calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))


def _fixed_version(root, n):
    """The vX.Y.Z of the newest 'fixed in' comment on the issue (an earlier release announced it), else None."""
    raw = upstream._gh(["issue", "view", str(n), "-R", repo_of(root), "--json", "comments"])
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return None
    found = None
    for c in (data.get("comments") or []):
        m = FIXED_RE.search(c.get("body", "")) if isinstance(c, dict) else None
        if m:
            found = m.group(1)
    return found


def tag_on_origin(root, tag):
    """True when `tag` exists on the origin remote (git ls-remote), i.e. the release was really pushed."""
    r = proc.run(["git", "-C", str(root), "ls-remote", "--tags", "origin", f"refs/tags/{tag}"],
                 capture_output=True, text=True, timeout=60)
    return r.returncode == 0 and any(ln.split()[-1].strip() == f"refs/tags/{tag}" for ln in r.stdout.splitlines())


def _vkey(v):
    """Sortable key for vX.Y.Z."""
    return tuple(int(x) for x in v.lstrip("v").split("."))


ROUTINE = ("already fixed in ", "merged before previous release")  # skip reasons that are collapsed into counts


def _open_by_issue(open_rows):
    """{issue number: [ids of linked tasks not yet merged/verified/wontfix]} from (upstream ref, task id) rows."""
    open_by = {}
    for b, tid in open_rows:
        try:
            open_by.setdefault(issue_number(b), []).append(tid)
        except S.AesirError:
            continue
    return open_by


def _held_reason(n, by_hand, open_by):
    """Why issue n is not announced regardless of release history, or None: named in `--skip`, or a linked task is
    still open (a merged task fixed part, an open one holds the rest; reports#166)."""
    if n in by_hand:
        return "skipped by hand"
    if n in open_by:
        return "partly fixed; open: " + ", ".join(f"#{i}" for i in open_by[n])
    return None


def plan_release(root, tag, db_arg=None, skip=()):
    """Read-only apart from refreshing a stale seen-file marker: ([issue numbers to announce as fixed in <tag>],
    [(issue number, reason)] skipped).
    Reasons: 'already fixed in vA.B.C', 'linked again since vA.B.C' (a task merged after that release; the lead
    comments by hand), 'merged before previous release', 'partly fixed; open: #N' (another linked task is still
    open, so the issue is not fixed yet), 'skipped by hand' (named in `skip`, i.e. `fixed-in --skip`). Each issue is classified once, on the latest merge time
    over all its linked merged tasks (an issue can be linked to several)."""
    path = db.db_path(db_arg, root)
    if not Path(path).exists():
        return [], []
    conn = db.connect(path, migrate_db=False)
    try:
        rows = conn.execute(
            "SELECT l.b_id, (SELECT MAX(g.ts) FROM task_log g WHERE g.task_id=t.id AND g.text LIKE '%-> merged%') "
            "FROM links l JOIN tasks t ON t.id=CAST(l.a_id AS INTEGER) "
            "WHERE l.a_type='task' AND l.b_type='upstream' AND t.status IN ('merged','verified')").fetchall()
        open_rows = conn.execute(
            "SELECT l.b_id, t.id FROM links l JOIN tasks t ON t.id=CAST(l.a_id AS INTEGER) "
            "WHERE l.a_type='task' AND l.b_type='upstream' AND t.status NOT IN ('merged','verified','wontfix') "
            "ORDER BY t.id").fetchall()
    finally:
        conn.close()
    latest = {}  # issue -> newest merge epoch over its tasks (None when no task has a merge entry)
    for b, merged in rows:
        try:
            n = issue_number(b)
        except S.AesirError:
            continue
        mt = _epoch(merged) if merged else None
        if n not in latest or (mt is not None and (latest[n] is None or mt > latest[n])):
            latest[n] = mt
    open_by = _open_by_issue(open_rows)
    by_hand = {int(x) for x in skip}
    prev = _prev_tag_time(root, tag)
    seen = load_seen(root, db_arg)
    done = seen.get("released", {})
    times = _tag_times(root)
    nums, skipped = [], []
    for n in sorted(latest):
        mt = latest[n]
        held = _held_reason(n, by_hand, open_by)
        if held:
            skipped.append((n, held))
            continue
        old = done.get(str(n))
        if old and old != "earlier":
            if old != tag and mt is not None and old in times and mt > times[old]:
                # looks linked again; a 'fixed in' the lead posted by hand since then clears it (and refreshes the marker)
                hand = _fixed_version(root, n)
                if hand and hand != old and _vkey(hand) > _vkey(old):
                    seen.setdefault("released", {})[str(n)] = old = hand
                    save_seen(root, seen, db_arg)
            # announced by an earlier release: only a task merged after that tag makes it news again
            if old != tag and mt is not None and old in times and mt > times[old]:
                skipped.append((n, f"linked again since {old}"))
            else:
                skipped.append((n, f"already fixed in {old}"))
            continue
        if prev is not None and mt is not None and mt <= prev:
            skipped.append((n, "merged before previous release"))
            continue
        fixed = _fixed_version(root, n)
        if fixed:
            again = mt is not None and fixed in times and mt > times[fixed]
            skipped.append((n, f"linked again since {fixed}" if again else f"already fixed in {fixed}"))
            continue
        nums.append(n)
    skipped += [(n, "skipped by hand") for n in sorted(by_hand - set(latest))]  # named but not a candidate: say so
    return nums, skipped


def skip_lines(skipped, show_all=False):
    """Output lines for plan_release's skipped list: unusual reasons (incl. 'linked again') one line each; the two
    routine reasons collapse into one count line unless show_all (a real db has ~100 of them every release)."""
    lines, counts = [], {"already fixed in an earlier release": 0, "merged before the previous release": 0}
    for n, why in skipped:
        if not show_all and why.startswith(ROUTINE[0]):
            counts["already fixed in an earlier release"] += 1
        elif not show_all and why == ROUTINE[1]:
            counts["merged before the previous release"] += 1
        else:
            lines.append(f"skipped: #{n} ({why})")
    parts = [f"{c} {k}" for k, c in counts.items() if c]
    if parts:
        lines.append("skipped: " + ", ".join(parts) + " (--all to list)")
    return lines


def announce_release(root, tag, db_arg=None, actor="unknown", plan=None):
    """Comment 'fixed in <tag>' on the issues in `plan` (default: plan_release's), once per issue. Returns the
    numbers commented. Run by `upstream fixed-in` only after the tag is on origin (D193), never by `release`."""
    nums, _ = plan if plan is not None else plan_release(root, tag, db_arg)
    repo = repo_of(root)
    seen = load_seen(root, db_arg)
    done = seen.setdefault("released", {})
    for n in nums:
        upstream._gh(["issue", "comment", str(n), "-R", repo, "--body", f"Release (aesir-{actor}): fixed in {tag}"])
        done[str(n)] = tag
        save_seen(root, seen, db_arg)
    return nums


def my_reports(conn):
    """URLs this project reported upstream (from its event log)."""
    urls = []
    for r in conn.execute("SELECT payload FROM events WHERE entity='upstream' AND action='report' ORDER BY id"):
        try:
            u = json.loads(r[0]).get("url")
        except (ValueError, TypeError, AttributeError):
            u = None
        if u and u not in urls:
            urls.append(u)
    return urls


def report_status(conn, root):
    by_num = {i.get("number"): i for i in fetch(repo_of(root))}
    res = []
    for u in my_reports(conn):
        try:
            n = issue_number(u)
        except S.AesirError:
            continue
        i = by_num.get(n)
        if not i:
            res.append({"issue": n, "url": u, "state": "UNKNOWN", "title": "", "fix": ""})
            continue
        fix = ""
        for c in i.get("comments") or []:
            m = FIXED_RE.search(c.get("body", "") if isinstance(c, dict) else "")
            if m:
                fix = m.group(1)
        res.append({"issue": n, "url": u, "state": str(i.get("state", "")).upper(), "title": i.get("title", ""), "fix": fix})
    return res


def close(root, actor, issue):
    n = issue_number(issue)
    upstream._gh(["issue", "close", str(n), "-R", repo_of(root), "--comment", f"Close (aesir-{actor}):\nverified fixed by {actor}"])
    return n
