"""Per-criterion evidence (task #55). Only executed checks count: `evidence run` runs the command itself and records
exit code, output excerpt (full output in a file), the checkout head sha, actor and time. `evidence add` records a
claimed note for what cannot be executed (owner-sensor, manual observation); it satisfies a criterion only where the
criterion text says so. in_review needs, per numbered criterion, evidence at the branch head sha (older = stale)."""
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from . import proc
from . import db, gates, observe
from .db import event, now, tx
from .service import AesirError, get_task, resolve_branch_head

CRIT_RE = re.compile(r"^\s*(\d+)[.)]\s+(.*)")
# criterion wording that allows claimed evidence: owner-sensor / manual observation, or an explicit marker.
# A bare word like "claimed" does not count (a criterion may merely describe kind=claimed).
CLAIM_OK_RE = re.compile(r"owner[- ]sensor|manual(?:ly)?\s+(?:observation|check|verification|test)|\[claimed ok\]", re.I)
# An --expect that only restates success proves nothing criterion-specific: such a run is [RAN], not [MET].
GENERIC_EXPECT_RE = re.compile(r"^\s*(ok|pass(ed)?|success(ful)?|exit(\s+code)?\s*0|done)\s*[.!]?\s*$", re.I)
# Command or tool names a criterion cites: `backticked`, "double quoted" or 'single quoted' (not apostrophes in words).
NAMED_RE = re.compile(r"`([^`]+)`|\"([^\"]+)\"|(?<!\w)'([^']+)'(?!\w)")
OK_STATES = ("met", "ran", "claimed")  # states that satisfy the in_review check
EXCERPT = 600
TIMEOUT = 600
# Exit codes aesir itself assigns when the command never produced a trustworthy result (127 = shell "not found").
NOT_STARTED = 127
REFUSED = 125
TIMED_OUT = 124
FIRST_WORD_RE = re.compile(r"""^\s*(?:"([^"]*)"|'([^']*)'|(\S+))""")
# Extensions cmd.exe really executes; any other existing file as first word is opened through its file association.
RUNNABLE_EXT = (".exe", ".com", ".bat", ".cmd")


def criteria(spec):
    """[(n, text)] for the numbered lines of a spec (continuation lines join the criterion above)."""
    out = []
    for ln in (spec or "").splitlines():
        m = CRIT_RE.match(ln)
        if m:
            out.append([int(m.group(1)), m.group(2).strip()])
        elif out and ln.strip():
            out[-1][1] += " " + ln.strip()
    return [(n, t) for n, t in out]


def _head(cwd):
    return gates.git(cwd, "rev-parse", "HEAD"), gates.git(cwd, "rev-parse", "--abbrev-ref", "HEAD")


def _check_criterion(t, n):
    if n not in [c[0] for c in criteria(t["spec"])]:
        raise AesirError(f"task {t['id']} has no numbered criterion {n}")


def _excerpt(out, expect):
    out = out.strip()
    if expect:
        i = out.find(expect)
        if i >= 0:
            a = max(0, i - EXCERPT // 3)
            return out[a:a + EXCERPT]
    return out[-EXCERPT:]


def _check_ui_cmd(root, cmd):
    sec = observe.load(root)
    allowed = [c for k in observe.REQUIRED if k in sec for c in [observe._cmd(sec[k])[0]] if c]
    if not any(cmd.strip().startswith(c) for c in allowed):
        raise AesirError("UI task: the evidence command must be an [observe] digest/headless command"
                         + (f" ({'; '.join(allowed)})" if allowed else " (none configured)")
                         + "\nThe config is read from the main checkout. If this task's headless check exists only on its"
                         " branch, add it to [observe] on main first (then rerun), or lead can override with"
                         " --force-evidence-skip --reason citing the gate run and the test output")


def log_dir_for(db_file, root):
    """Where evidence logs go for the DB file in use (None = in-memory db: under root)."""
    return Path(db_file).parent / "evidence" if db_file else Path(root) / ".aesir" / "evidence"


def _log_dir(conn, root):
    """Evidence logs live beside the DB in use, so a temp/test DB never touches the live one's logs."""
    for _, name, file in conn.execute("PRAGMA database_list"):
        if name == "main":
            return log_dir_for(file, root)
    return log_dir_for(None, root)


def find_bash():
    """Git for Windows bash.exe, or None. Never System32\\bash.exe (the WSL launcher: another filesystem view). Looks
    beside git first (the install the project already uses), then the standard dirs, then PATH minus System32."""
    git = shutil.which("git")
    cands = []
    if git:
        root = Path(git).resolve().parent.parent  # <Git>/cmd/git.exe or <Git>/bin/git.exe
        cands += [root / "bin" / "bash.exe", root / "usr" / "bin" / "bash.exe"]
    for env in ("ProgramFiles", "ProgramW6432", "LocalAppData"):
        base = os.environ.get(env)
        if base:
            cands.append(Path(base) / "Git" / "bin" / "bash.exe")
    cands += [Path(p) for p in [shutil.which("bash")] if p and not any(x in p.lower() for x in ("system32", "windowsapps"))]
    return next((str(c) for c in cands if c.is_file()), None)


def first_word(cmd):
    """The command's first word without quotes ('' when blank)."""
    m = FIRST_WORD_RE.match(cmd)
    return next((g for g in m.groups() if g is not None), "") if m else ""


def quote(argv, platform=None):
    """An argv as one command line, each argument quoted as executed (the recorded `cmd:`). One helper for both OSes."""
    return subprocess.list2cmdline(argv) if (platform or sys.platform) == "win32" else shlex.join(argv)


def cmd_escape(line):
    """A list2cmdline result made safe for cmd.exe: caret-escape ^ & | < > ( ) and % outside double quotes, where cmd
    would otherwise act on them. Inside "..." cmd keeps them literal, so those regions are left alone."""
    out, inq = [], False
    for ch in line:
        if ch == '"':
            inq = not inq
        elif not inq and ch in "^&|<>()%":
            out.append("^")
        out.append(ch)
    return "".join(out)


def text_of(cmd, platform=None):
    """The command as recorded: a single-string command as given, an argv quoted."""
    return cmd if isinstance(cmd, str) else quote(cmd, platform)


def _no_bash():
    return ("cannot run a .sh script or bash: Git for Windows bash was not found; install Git for Windows "
            "(https://git-scm.com) or put its bin directory on PATH")


BASH_NAMES = ("bash", "sh", "bash.exe", "sh.exe")


def plan(cmd, platform=None):
    """(Popen args, shell flag, refusal text). The one place a command becomes a process.
    An argv (list of 2+ arguments) runs directly with no shell, so ^ ( ) & < > | stay literal (#191). A single string
    runs through the shell (cmd.exe on Windows, /bin/sh elsewhere). On Windows:
    - an argv whose first word is bash, sh or a .sh file runs through Git bash (find_bash);
    - a first word that is no program on PATH (a cmd.exe builtin) falls back to cmd.exe, whatever shell called
      aesir, with the line built by cmd_escape so metacharacters in the arguments stay literal;
    - a single string whose first word is a .sh file runs through Git bash too: cmd.exe would hand it to the file
      association (exit 0, no output) or idle in a console."""
    win = (platform or sys.platform) == "win32"
    if not isinstance(cmd, str):
        argv = list(cmd)
        if win:
            first = argv[0].replace("\\", "/")
            word = first.rsplit("/", 1)[-1].lower()
            if word in BASH_NAMES or word.endswith(".sh"):
                bash = find_bash()
                if not bash:
                    return None, False, _no_bash()
                argv = [bash] + (argv[1:] if word in BASH_NAMES else [first] + argv[1:])
            else:  # CreateProcess does not search PATHEXT (.cmd, .bat): resolve the way a shell would
                found = shutil.which(argv[0])
                if not found:  # no such program: a cmd.exe builtin (echo, dir) or a typo; the shell decides, as before
                    return cmd_escape(quote(argv, platform)), True, ""
                argv[0] = found
        return argv, False, ""
    if not win or not first_word(cmd).lower().endswith(".sh"):
        return cmd, True, ""
    bash = find_bash()
    if not bash:
        return None, False, _no_bash()
    script = first_word(cmd).replace("\\", "/")
    rest = cmd[FIRST_WORD_RE.match(cmd).end():]
    # bash -c '<bash> script rest': `rest` keeps its own shell syntax (args, &&); `bash script` needs no exec bit/shebang.
    return [bash, "-c", f'"{bash.replace(chr(92), "/")}" "{script}"{rest}'], False, ""


def shell_of(cmd, platform=None):
    """Which shell `cmd` runs under, for the record: 'none (argv)', 'cmd.exe', '/bin/sh' or the Git bash path."""
    args, shell, _ = plan(cmd, platform)
    if shell:
        return "cmd.exe" if (platform or sys.platform) == "win32" else "/bin/sh"
    if args and "bash" in Path(args[0]).name.lower():
        return args[0]
    return "none (argv)"


def silent_failure(code, text, cwd, cmd):
    """The line that replaces an empty output on a non-zero exit, so a bare `[exit 1]` is never the whole record
    (aesir-reports #111: cargo fmt --check recorded exit 1 with nothing). '' when there is output or the exit is 0."""
    if code == 0 or (text or "").strip():
        return ""
    return f"[no output captured: exit {code}; cwd {cwd}; shell {shell_of(cmd)}]"


def execute(cmd, cwd, timeout):
    """Run `cmd`; returns (exit code, output text, pid or None). A command that did not start, or an exit 0 with no
    output and no sign the process ran, never comes back as exit 0 (aesir-reports #164). The process runs as a
    gates.StepProc (the one job-object owner): on timeout the whole tree dies, so a grandchild (bash -> sleep) cannot
    hold the output pipe, and the output captured so far is kept."""
    args, shell, refusal = plan(cmd)
    cmd = text_of(cmd)
    if refusal:
        return NOT_STARTED, f"[not started: {refusal}]", None
    try:
        sp = gates.StepProc(args, shell=shell, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    except OSError as e:  # missing cwd/interpreter, access denied: the command never ran
        return NOT_STARTED, f"[not started: {type(e).__name__}: {e}]", None
    p = sp.p
    try:
        try:
            text, _ = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as e:
            partial = e.output or ""
            if isinstance(partial, bytes):
                partial = partial.decode("utf-8", "replace")
            sp.kill()
            try:
                rest, _ = p.communicate(timeout=3)
            except subprocess.TimeoutExpired:  # an escapee holds the pipe: keep what we have
                rest = ""
            return TIMED_OUT, partial + (rest or "") + f"\n[timed out after {timeout}s]", p.pid
    except BaseException:  # interrupted: do not orphan the tree
        sp.kill()
        raise
    finally:
        sp.close()  # kills stragglers of a finished command
    if p.returncode == 0 and not (text or "").strip():
        why = _silent_zero(cmd, cwd, p.pid, shell)
        if why:
            return REFUSED, f"[refused as evidence: exit 0, no output, and {why}]", p.pid
    return p.returncode, text or "", p.pid


def _runnable_ext():
    """Extensions cmd.exe executes itself: the built-in set plus PATHEXT (.py, .ps1 ... when so registered)."""
    return set(RUNNABLE_EXT) | {e.lower() for e in os.environ.get("PATHEXT", "").split(os.pathsep) if e}


def _silent_zero(cmd, cwd, pid, via_cmd):
    """Why an exit 0 with empty output proves nothing, or '' when the process plausibly ran."""
    if not pid:
        return "no process id (the spawn did not happen)"
    word = first_word(cmd)
    if via_cmd and sys.platform == "win32" and word and Path(cwd, word).is_file() and Path(word).suffix.lower() not in _runnable_ext():
        # cmd.exe opens such a file through its association and reports success without running it
        return f"`{word}` is a file cmd.exe cannot execute"
    return ""


DIRTY_MARK = "dirty (mutation): "  # first line of a must-fail run's excerpt when the tree was dirty


def dirty_must_fail(e):
    """The dirty paths of a must-fail run that ran on a dirty tree ('' when it did not)."""
    if not e.get("expect_fail"):
        return ""
    for ln in e["excerpt"].splitlines()[:2]:
        if ln.startswith(DIRTY_MARK):
            return ln[len(DIRTY_MARK):]
    return ""


def failed_as_declared(code, text, expect_fail, expect):
    """(met, line): did a must-fail run fail as declared? `expect_fail` is 'any' or an exit code string. Only a command
    that ran counts: not-started (127), refused (125) and timeout (124) are never the expected failure, so a missing
    tool cannot pass a mutation check. `line` explains a miss ('' when met)."""
    if code in (NOT_STARTED, REFUSED, TIMED_OUT):
        return False, f"[expected failure, command did not run to a result (exit {code})]"
    if code == 0:
        return False, "[expected failure, command passed (exit 0)]"
    if expect_fail != "any" and code != int(expect_fail):
        return False, f"[expected failure with exit {expect_fail}, got exit {code}]"
    if expect and expect not in text:
        return False, f"[expected failure, but expected text not found: {expect}]"
    return True, ""


def holds(e):
    """True when an executed row's command did what the run declared: exit 0, or (--expect-fail) failed as declared."""
    return bool(e["fail_met"]) if e.get("expect_fail") else e["exit_code"] == 0


def run(conn, actor, tid, criterion, cmd, cwd, expect="", timeout=TIMEOUT, root=None, expect_fail=""):
    """Execute `cmd` in `cwd`, record it as executed evidence. Returns the row dict. `expect_fail` ('any' or an exit
    code) declares a mutation check: the row is MET only when the command fails as declared (failed_as_declared)."""
    t = get_task(conn, tid)
    _check_criterion(t, criterion)
    root = root or db.project_root(cwd)
    argv, cmd = cmd, text_of(cmd)
    if observe.is_ui(t):
        _check_ui_cmd(root, cmd)
        if not expect:
            raise AesirError("UI task: give --expect <text> that the rendered output must contain")
    dirty = gates.dirty_files(cwd)
    if dirty and not expect_fail:
        raise AesirError("dirty tree: commit first, the sha must describe the code that ran\n"
                         + "\n".join(gates.dirty_detail(conn, cwd, dirty)))
    # a must-fail run may carry an uncommitted mutation in a temp worktree at the task head: the sha stays the
    # unmutated head (so the row is not stale) and the mutated paths are named in the record
    head_lines = []
    if dirty:
        head_lines.append(DIRTY_MARK + ", ".join(gates.dirty_paths(cwd, dirty)))
    sha, branch = _head(cwd)
    code, text, _pid = execute(argv, cwd, timeout)
    if note := silent_failure(code, text, cwd, argv):
        text = note
    fail_met = 0
    if expect_fail:
        ok, line = failed_as_declared(code, text, expect_fail, expect)
        fail_met = int(ok)
        if line:
            head_lines.insert(0, line)  # first, so the excerpt keeps the verdict
    elif expect and code == 0 and expect not in text:
        code = 1
        text += f"\n[expected text not found: {expect}]"
    head = "\n".join(head_lines)
    excerpt = (head + "\n" if head else "") + _excerpt(text, expect)
    if head:
        text = head + "\n" + text
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO evidence (task_id, criterion, kind, cmd, exit_code, excerpt, expect, sha, branch, actor, ts,"
            " expect_fail, fail_met) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (t["id"], criterion, "executed", cmd, code, excerpt, expect, sha, branch, actor, now(),
             expect_fail, fail_met))
        eid = cur.lastrowid
        ref = _log_dir(conn, root) / f"E{eid}.log"
        ref.parent.mkdir(parents=True, exist_ok=True)
        ref.write_text(f"$ {cmd}\n{text}\n[exit {code}]\n", encoding="utf-8")
        conn.execute("UPDATE evidence SET output_ref=? WHERE id=?", (str(ref), eid))
        event(conn, actor, "evidence", eid, "run", {"task": t["id"], "criterion": criterion, "exit": code, "sha": sha})
    return get(conn, eid)


def add(conn, actor, tid, criterion, note, ref="", cwd=None):
    t = get_task(conn, tid)
    _check_criterion(t, criterion)
    if not (note or "").strip():
        raise AesirError("--note must not be empty")
    try:
        sha, branch = _head(cwd or ".")
    except AesirError:
        sha, branch = "", ""
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO evidence (task_id, criterion, kind, note, ref, sha, branch, actor, ts)"
            " VALUES (?,?,?,?,?,?,?,?,?)", (t["id"], criterion, "claimed", note, ref or "", sha, branch, actor, now()))
        event(conn, actor, "evidence", cur.lastrowid, "add", {"task": t["id"], "criterion": criterion})
    return get(conn, cur.lastrowid)


def get(conn, eid):
    return dict(conn.execute("SELECT * FROM evidence WHERE id=?", (eid,)).fetchone())


def listing(conn, tid):
    get_task(conn, tid)
    return [dict(r) for r in conn.execute("SELECT * FROM evidence WHERE task_id=? ORDER BY id", (int(tid),))]


def named(text):
    """Command/tool names a criterion text cites (backticked or quoted), in order, de-duplicated."""
    seen = []
    for m in NAMED_RE.finditer(text):
        n = next(g for g in m.groups() if g).strip()
        if n and n not in seen:
            seen.append(n)
    return seen


def command_warnings(text, es):
    """Cited names that no executed evidence command for the criterion contains (warn only: names can be loose)."""
    cmds = [e["cmd"].lower() for e in es if e["kind"] == "executed"]
    if not cmds:
        return []  # no executed evidence: the criterion already shows MISSING/STALE/CLAIMED
    return [n for n in named(text) if not any(n.lower() in c for c in cmds)]


def is_specific(expect):
    """True when an --expect pattern is criterion-specific (set and not just an OK/pass/exit 0 restatement)."""
    return bool(expect) and not GENERIC_EXPECT_RE.match(expect)


def _passes(e, ui):
    if e["kind"] != "executed" or not holds(e):
        return False
    return not ui or bool(e["expect"] and e["expect"] in e["excerpt"])


def status(conn, t, head=None):
    """Per criterion: {n, text, state, warnings, entries}. state: met (executed at head, criterion-specific --expect
    matched), ran (executed at head, exit 0 only or a generic expect), claimed (allowed by the criterion's
    wording), stale (only older shas), missing. `head` None = do not compare shas (no branch to resolve)."""
    ui = observe.is_ui(t)
    rows = [dict(r) for r in conn.execute("SELECT * FROM evidence WHERE task_id=? ORDER BY id", (t["id"],))]
    res = []
    for n, text in criteria(t["spec"]):
        es = [e for e in rows if e["criterion"] == n]
        cur = [e for e in es if head is None or e["sha"] == head]
        allow = bool(CLAIM_OK_RE.search(text))
        if any(_passes(e, ui) and (e["expect_fail"] or is_specific(e["expect"])) for e in cur):
            # a failed expect makes the run exit 1, so a recorded specific expect with exit 0 did match; a must-fail
            # run that failed as declared is MET by its declaration (fail_met)
            st = "met"
        elif any(_passes(e, ui) for e in cur):
            st = "ran"
        elif allow and any(e["kind"] == "claimed" for e in cur):
            st = "claimed"
        elif any(_passes(e, ui) or (allow and e["kind"] == "claimed") for e in es):
            st = "stale"
        else:
            st = "missing"
        # the current must-fail runs that make this criterion MET; `dirty` names the paths when one ran on a dirty tree
        mf = [e for e in cur if e["expect_fail"] and _passes(e, ui)] if st == "met" else []
        dirty = next((d for e in mf if (d := dirty_must_fail(e))), "")
        res.append({"n": n, "text": text, "state": st, "warnings": command_warnings(text, es), "entries": es,
                    "must_fail": bool(mf), "dirty": dirty})
    return res


def state_tag(c):
    """The state word for task show: 'MET', or 'MET, must-fail[, dirty]' when a must-fail run made it so."""
    tag = c["state"].upper()
    if c.get("must_fail"):
        tag += ", must-fail" + (", dirty" if c.get("dirty") else "")
    return tag


def log_lines(sts):
    """Per criterion, the evidence log files to read before verifying ([OK] only means the command ran)."""
    out = []
    for c in sts:
        logs = [e["output_ref"] for e in c["entries"] if e["kind"] == "executed" and e["output_ref"]]
        out.append(f"{c['n']}. " + (", ".join(logs) if logs else "(no log: " + ("claimed note only)" if c["entries"] else "no evidence)")))
    return out


def head_of(t, repo):
    """The task branch head sha, or None when there is no resolvable branch."""
    r = head_ref(t, repo)
    return r[1] if r else None


def head_ref(t, repo):
    """(ref_used, sha) of the task branch head via service.resolve_branch_head (origin head when local lags), or None."""
    if not t["branch"]:
        return None
    try:
        return resolve_branch_head(repo, t["branch"])
    except AesirError:
        return None


def notes(sts):
    """Non-blocking lines for in_review: criteria that only RAN (the reviewer must quote the output line) and
    cited commands that no evidence command contains."""
    out = []
    ran = [str(c["n"]) for c in sts if c["state"] == "ran"]
    if ran:
        out.append(f"RAN (exit 0, no criterion-specific --expect) for criteria: {', '.join(ran)}; "
                   "the reviewer must quote the output line that shows each is met")
    # a must-fail run is allowed on a dirty tree (the mutation); an unrelated failure there would also read as MET
    out += [f"criterion {c['n']} met by a must-fail run on a dirty tree ({c['dirty']}): confirm the dirt is the intended mutation"
            for c in sts if c.get("dirty")]
    out += [f"warning: criterion {c['n']} cites `{w}` but no evidence command for it contains that"
            for c in sts for w in c["warnings"]]
    return out


def check_in_review(conn, t, repo):
    """Raise unless every numbered criterion has evidence at the branch head sha. Returns non-blocking notes."""
    hr = head_ref(t, repo)
    sts = status(conn, t, hr[1] if hr else None)
    bad = []
    for c in sts:
        if c["state"] in OK_STATES:
            continue
        newest = c["entries"][-1]["sha"][:8] if c["entries"] else ""
        bad.append(f"{c['n']} ({c['state']})" + (f" [newest evidence at {newest}]" if c["state"] == "stale" else ""))
    if bad:
        at = f"{hr[0]} head {hr[1][:8]}" if hr else "the branch head"
        raise AesirError(f"task {t['id']} lacks executed evidence at {at} for criteria: {', '.join(bad)}; "
                         f"run: aesir evidence run {t['id']} --criterion N -- <cmd> (in the task worktree); "
                         f"lead can override with --force-evidence-skip --reason")
    return notes(sts)


def predates(conn, t):
    """True when the task reached in_review before the evidence table existed (or never via a logged in_review)."""
    since = conn.execute("SELECT value FROM meta WHERE key='evidence_since'").fetchone()
    if not since:
        return True
    return not conn.execute("SELECT 1 FROM task_log WHERE task_id=? AND text LIKE 'status % -> in_review%' AND ts>=?",
                            (t["id"], since[0])).fetchone()


def lacking(conn, t):
    """Criteria without evidence, for status anomalies (no sha comparison: the branch may be gone)."""
    return [c["n"] for c in status(conn, t, None) if c["state"] not in OK_STATES]
