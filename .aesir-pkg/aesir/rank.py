"""File ranking and review passes (task #135, D139, D140): which files most deserve a judgement pass next.

score = smell (weighted severity of the file's open and baselined findings) + churn (lines changed since the file's
last review pass, per git; a never-reviewed file counts all its lines). Findings are smell signals, not verdicts.
A recorded review (commit + date) lives in the space table (role audit, kind review, key file): no schema change.
See docs/audit-findings.md."""
from datetime import datetime, timezone
from pathlib import Path

from . import audit, gates, proc
from . import service as S
from .db import now
from .invoke import cmd as client_cmd
from .service import AesirError

PASS_PREFIX = "Review pass: "
DEFAULTS = {"high": 10.0, "medium": 4.0, "low": 1.0, "info": 0.0, "churn": 0.05, "top_n": 3, "tests": False,
            "churn_cap_lines": 100}
SPACE = ("audit", "review")
OPEN_STATES = ("open", "baselined")
DONE_TASK = ("merged", "verified", "wontfix")


def load_config(root):
    """[audit.rank] in aesir.toml: severity weights (high/medium/low/info), churn (weight per changed line), top_n,
    churn_cap_lines (most lines of churn a never-reviewed file counts; default 100), tests (true: test files rank too; default false)."""
    try:
        sec = gates.load_config(root).get("audit", {}).get("rank", {})
    except AesirError as e:
        if "no aesir.toml" not in str(e):
            raise
        sec = {}
    if not isinstance(sec, dict):
        raise AesirError("bad aesir.toml: [audit.rank] must be a table")
    cfg = {**DEFAULTS}
    for k, v in sec.items():
        if k not in DEFAULTS:
            raise AesirError(f"bad aesir.toml: [audit.rank] unknown key {k} (known: {', '.join(DEFAULTS)})")
        if k == "tests":
            if not isinstance(v, bool):
                raise AesirError("bad aesir.toml: [audit.rank] tests must be true or false")
        elif isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            raise AesirError(f"bad aesir.toml: [audit.rank] {k} must be a number >= 0")
        cfg[k] = v
    if int(cfg["top_n"]) != cfg["top_n"] or cfg["top_n"] < 1:
        raise AesirError("bad aesir.toml: [audit.rank] top_n must be an integer >= 1")
    cfg["top_n"] = int(cfg["top_n"])
    return cfg


def is_test_file(path):
    """A test file: under a tests/ or test/ directory, or named test_*, *_test.* or *.test.*."""
    parts = audit._norm_file(path).split("/")
    n = parts[-1].lower()
    return (any(p.lower() in ("tests", "test") for p in parts[:-1]) or n.startswith("test_")
            or Path(n).stem.endswith("_test") or ".test." in n)


def _git(root, *args):
    r = proc.run(["git", *args], cwd=str(root), capture_output=True)
    return r.returncode, r.stdout.decode("utf-8", "replace")


def head(root):
    """Full sha of HEAD in `root`; an error when the project has no commit."""
    rc, o = _git(root, "rev-parse", "--verify", "HEAD")
    if rc:
        raise AesirError("no git commit in the project root to record a review against")
    return o.strip()


def reviews(conn):
    """{file: {commit, date, task}} of recorded review passes."""
    return {r["key"]: r["body"] for r in S.space_list(conn, SPACE[0], SPACE[1])}


def record_review(conn, actor, file, root, task=None):
    """Record that `file` was reviewed at the current HEAD today; resets its churn to 0."""
    file = audit._norm_file(file)
    sha = head(root)
    S.space_put(conn, actor, SPACE[0], SPACE[1], file,
                {"commit": sha, "date": now()[:10], "task": task})
    return sha


def _churn_since(root, commit, files):
    """{file: lines added + deleted since commit (to HEAD)} for `files`; None for a commit git does not know."""
    rc, o = _git(root, "diff", "--numstat", "--no-renames", commit, "HEAD", "--", *files)
    if rc:
        return None
    res = {}
    for ln in o.splitlines():
        a, d, f = (ln.split("\t") + ["", "", ""])[:3]
        res[audit._norm_file(f)] = (int(a) if a.isdigit() else 0) + (int(d) if d.isdigit() else 0)
    return res


def open_pass_tasks(conn):
    """{file: task id} of review-pass tasks not yet merged."""
    res = {}
    for r in conn.execute("SELECT id, title, status FROM tasks WHERE title LIKE ? ORDER BY id", (PASS_PREFIX + "%",)):
        if r["status"] not in DONE_TASK:
            res[audit._norm_file(r["title"][len(PASS_PREFIX):])] = r["id"]
    return res


def rank(conn, root, cfg=None):
    """Files with a score, top first: [{file, score, smell, churn, churn_lines, findings, reviewed}]. Only files an enabled
    toolkit covers rank (data and generated files never do); test files only with tests = true. Files with an
    open pass task are left out (not offered again); a file with score 0 is left out."""
    cfg = cfg or load_config(root)
    ac = audit.load_config(root)
    tracked = [f for f in audit.covered_files(root, ac) if cfg["tests"] or not is_test_file(f)]
    rev, busy = reviews(conn), open_pass_tasks(conn)
    smell, count = {}, {}
    for r in conn.execute("SELECT file, severity FROM findings WHERE status IN (?,?)", OPEN_STATES):
        smell[r["file"]] = smell.get(r["file"], 0.0) + cfg[r["severity"]]
        count[r["file"]] = count.get(r["file"], 0) + 1
    by_commit = {}
    for f in tracked:
        if f in rev:
            by_commit.setdefault(rev[f]["commit"], []).append(f)
    churn = {}
    for commit, fs in by_commit.items():
        got = _churn_since(root, commit, fs)
        for f in fs:
            churn[f] = None if got is None else got.get(f, 0)
    rows = []
    for f in tracked:
        if f in busy:
            continue
        lines, cap = churn.get(f), None
        if lines is None:  # never reviewed (or its review commit is gone): all its lines, capped (D147)
            lines = audit._weight(Path(root) / f)
            if lines is None:
                continue  # binary or unreadable
            cap = cfg["churn_cap_lines"]
        s, c = smell.get(f, 0.0), min(lines, cap if cap is not None else lines) * cfg["churn"]
        if s + c <= 0:
            continue
        rows.append({"file": f, "score": round(s + c, 2), "smell": round(s, 2), "churn": round(c, 2),
                     "churn_lines": lines, "churn_cap": cap, "findings": count.get(f, 0), "reviewed": rev[f]["date"] if f in rev else None})
    rows.sort(key=lambda r: (-r["score"], r["file"]))
    return rows


def status_line(conn, root):
    """One line for `status`: the top N files for this release's passes, or '' when nothing can be ranked."""
    try:
        cfg = load_config(root)
        top = rank(conn, root, cfg)[:cfg["top_n"]]
    except (AesirError, OSError):
        return ""
    if not top:
        return ""
    return client_cmd("review passes (top %d): " % cfg["top_n"] + ", ".join(f"{r['file']} {r['score']:g}" for r in top)
                      + " -> python -m aesir finding pass <file>  (all: python -m aesir finding rank)")


def _last_release_ts(root):
    """Epoch seconds of the newest v* tag, 0 when the project has none."""
    rc, o = _git(root, "describe", "--tags", "--abbrev=0", "--match", "v*")
    if rc:
        return 0
    rc, o = _git(root, "log", "-1", "--format=%ct", o.strip())
    return int(o.strip()) if not rc and o.strip().isdigit() else 0


def passes_this_release(conn, root):
    """Review-pass tasks created since the last release tag."""
    since = _last_release_ts(root)
    n = 0
    for r in conn.execute("SELECT created FROM tasks WHERE title LIKE ?", (PASS_PREFIX + "%",)):
        try:
            ts = datetime.strptime(r["created"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            ts = since + 1
        n += ts >= since
    return n


def _elsewhere(conn, f):
    """Id of the not-closed task a finding is already linked to (a fix-now task, say), else None."""
    if f["task_id"] is None:
        return None
    t = conn.execute("SELECT status FROM tasks WHERE id=?", (f["task_id"],)).fetchone()
    return f["task_id"] if t and t["status"] not in DONE_TASK else None


def criteria(file, findings, linked=None):
    """The task spec of a pass: D139 as numbered criteria plus the findings to start from."""
    lines = [f"1. Start from the findings listed below for {file}; judge each one fix or wontfix, with a reason "
             "(finding set <id> --status fixed|wontfix --note TEXT). Findings are smell signals, not verdicts.",
             "2. Also fix same-kind issues the audit cannot see (for docs: missing, wrong or stale comments).",
             "3. Record other kinds of issue, including real bugs, as findings (finding add) or tasks; do not fix "
             "them in this pass.",
             "4. Merging this task records the review of the file (commit and date), which resets its churn.",
             "", f"Findings ({len(findings)}, open or baselined, all rules):"]
    linked = linked or {}
    lines += [f"- F{f['id']} {f['severity']} {f['file']}:{f['line']} {f['rule']} [{f['source']}] "
              f"{f['status']}: {f['message'][:160]}"
              + (f" (linked to #{linked[f['id']]})" if f["id"] in linked else "") for f in findings]
    if not findings:
        lines.append("- (none)")
    return "\n".join(lines)


def start_pass(conn, actor, root, file, requester=None, reason=None):
    """Create the pass task for `file`. Returns (task id, created); an open pass task is returned, not duplicated."""
    file = audit._norm_file(file)
    if file not in audit.tracked_files(root):
        raise AesirError(f"{file} is not a tracked file of {root}")
    busy = open_pass_tasks(conn)
    if file in busy:
        return busy[file], False
    n = load_config(root)["top_n"]
    done = passes_this_release(conn, root)
    if done >= n and not (reason or "").strip():
        raise AesirError(f"{done} review passes already started since the last release (aesir.toml [audit.rank] "
                         f"top_n = {n}); taking more needs --reason TEXT (logged on the task)")
    findings = [f for f in audit.list_findings(conn, file=file) if f["status"] in OPEN_STATES]
    linked = {f["id"]: t for f in findings if (t := _elsewhere(conn, f))}  # keep links to open tasks
    tid = S.new_task(conn, actor, PASS_PREFIX + file, criteria(file, findings, linked),
                     requester=requester or "advisor", scope=file)
    for f in findings:
        if f["id"] not in linked:
            audit.set_finding(conn, actor, f["id"], task=tid)
    if done >= n:
        S.log_task(conn, actor, tid, f"pass beyond top {n} for this release: {reason.strip()}")
    return tid, True


def on_merged(conn, actor, task, root=None):
    """Hook for set_task: a merged review-pass task records its file's review. Best effort, never blocks the merge."""
    title = task["title"]
    if not title.startswith(PASS_PREFIX):
        return None
    try:
        sha = record_review(conn, actor, title[len(PASS_PREFIX):], root or Path("."), task["id"])
        S.log_task(conn, actor, task["id"], f"review recorded at {sha[:10]}")
        return sha
    except AesirError as e:
        S.log_task(conn, actor, task["id"], f"review not recorded: {e}")
        return None


def render(rows, n):
    """Text lines of `finding rank`; the first n are marked as this release's passes."""
    return [f"{i:>3}. {r['score']:>8g}  smell {r['smell']:g} ({r['findings']} findings) + churn {r['churn']:g} "
            f"({r['churn_lines']} lines{'' if r['churn_cap'] is None else ', never reviewed, capped at %g' % r['churn_cap']})  {r['file']}"
            + ("   <- this release" if i <= n else "") for i, r in enumerate(rows, 1)]
