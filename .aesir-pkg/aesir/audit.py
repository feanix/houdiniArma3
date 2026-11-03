"""Audit tooling (task #67): findings as rows, ranked and paged digests, sharded audit plans, toolkit runs.

Core stays stdlib. Language toolkits are separate programs that print findings in the core format
(docs/audit-findings.md); this module only imports them. A project with no toolkit enabled never reaches the
subprocess or file code below."""
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import proc
from . import gates
from .db import event, now, tx
from .service import AesirError

FORMAT_VERSION = 1
SEVERITIES = ("info", "low", "medium", "high")  # ascending; ranking is by index
STATUSES = ("open", "fixed", "wontfix", "false_positive", "baselined")
TIMEOUT = 600  # seconds per toolkit; [audit] timeout overrides
CHECKLIST = ("read every file of the shard in full (no skimming by name)",
             "record each problem as a finding: finding add <file> <line> --severity .. --rule .. --message ..",
             "stay inside the shard: other reviewers own the other files",
             "mark the shard done when finished: audit done <audit> <shard>")


def load_config(root):
    """[audit] in aesir.toml: toolkits (names), timeout, exclude (globs); [audit.<name>] cmd per toolkit."""
    try:
        sec = gates.load_config(root).get("audit", {})
    except AesirError as e:
        if "no aesir.toml" not in str(e):
            raise
        sec = {}
    tk = sec.get("toolkits", [])
    if not isinstance(tk, list) or not all(isinstance(x, str) and x for x in tk):
        raise AesirError('bad aesir.toml: [audit] toolkits must be a list of names, e.g. ["rust"]')
    ex = sec.get("exclude", [])
    if not isinstance(ex, list) or not all(isinstance(x, str) for x in ex):
        raise AesirError("bad aesir.toml: [audit] exclude must be a list of glob patterns")
    t = sec.get("timeout", TIMEOUT)
    if isinstance(t, bool) or not isinstance(t, int) or t < 1:
        raise AesirError("bad aesir.toml: [audit] timeout must be an integer >= 1 (seconds)")
    rust = sec.get("rust", {})
    if isinstance(rust, dict):
        h = rust.get("hostile", [])
        if not isinstance(h, list) or not all(isinstance(x, str) and x for x in h):
            raise AesirError('bad aesir.toml: [audit.rust] hostile must be a list of path/module globs, e.g. ["src/parse*", "net/**"]')
        m = rust.get("max_file_lines", 1)
        if isinstance(m, bool) or not isinstance(m, int) or m < 1:
            raise AesirError("bad aesir.toml: [audit.rust] max_file_lines must be an integer >= 1")
    return {"toolkits": tk, "exclude": ex, "timeout": t, "raw": sec}


# ---- findings ----

def _norm_file(f):
    f = str(f).replace("\\", "/")
    return f[2:] if f.startswith("./") else f


FILE_IDENTITY = {("python", "doc_coverage")}  # (source, rule) pairs identified by file alone


def fingerprint(source, rule, file, message, occurrence=1):
    """Identity of a problem across audits: line numbers move, so they are not part of it."""
    msg = re.sub(r"\d+", "#", " ".join(message.lower().split()))
    if (source, rule) in FILE_IDENTITY:
        msg = ""  # one finding per file; its message (top items, counts) changes with every edit (#134)
    raw = "|".join((source, rule, file, msg)) + (f"|{occurrence}" if occurrence > 1 else "")
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _clean(d, default_source=""):
    """Validate one finding dict (core format v1); returns the normalised fields."""
    if not isinstance(d, dict):
        raise AesirError("a finding must be a JSON object")
    if d.get("v", FORMAT_VERSION) != FORMAT_VERSION:
        raise AesirError(f"unsupported findings format version {d.get('v')!r} (this Aesir reads v{FORMAT_VERSION})")
    f = {k: d.get(k, "") for k in ("file", "rule", "message", "source")}
    f["source"] = f["source"] or default_source
    for k in ("file", "rule", "source"):
        if not isinstance(f[k], str) or not f[k]:
            raise AesirError(f"finding needs a non-empty '{k}'")
    f["file"] = _norm_file(f["file"])
    f["message"] = str(f["message"])
    ln = d.get("line", 0)
    if isinstance(ln, bool) or not isinstance(ln, int) or ln < 0:
        raise AesirError("finding 'line' must be an integer >= 0")
    f["line"] = ln
    f["severity"] = d.get("severity", "")
    if f["severity"] not in SEVERITIES:
        raise AesirError(f"finding 'severity' must be one of {', '.join(SEVERITIES)}")
    return f


def new_audit(conn, actor, kind, note=""):
    cur = conn.execute("INSERT INTO audits (kind, note, actor, ts) VALUES (?,?,?,?)", (kind, note, actor, now()))
    return cur.lastrowid


def get_audit(conn, aid):
    r = conn.execute("SELECT * FROM audits WHERE id=?", (aid,)).fetchone()
    if not r:
        raise AesirError(f"no audit {aid}")
    return r


def _unique_fp(f, seen):
    """Fingerprint of finding f, numbered past any identical one already in `seen` (which it joins)."""
    n = 1
    while True:
        fp = fingerprint(f["source"], f["rule"], f["file"], f["message"], n)
        if fp not in seen:
            break
        n += 1
    seen.add(fp)
    return fp


def _upsert(conn, f, audit_id, seen):
    """Insert a finding or refresh the one with the same fingerprint. Returns (finding id, 'new'|'seen'|'reopened')."""
    fp = _unique_fp(f, seen)
    ts = now()
    row = conn.execute("SELECT id, status FROM findings WHERE fingerprint=?", (fp,)).fetchone()
    if (f["source"], f["rule"]) in FILE_IDENTITY:  # collapse older per-message duplicates of the same file (#134)
        conn.execute("UPDATE findings SET status='fixed', updated=? WHERE source=? AND rule=? AND file=?"
                     " AND status='open' AND fingerprint!=?", (ts, f["source"], f["rule"], f["file"], fp))
    if not row:
        cur = conn.execute(
            "INSERT INTO findings (file, line, severity, rule, source, message, fingerprint, first_audit, last_audit,"
            " created, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (f["file"], f["line"], f["severity"], f["rule"], f["source"], f["message"], fp, audit_id, audit_id, ts, ts))
        return cur.lastrowid, "new"
    reopened = row["status"] == "fixed"  # a fixed finding that is reported again is not fixed
    conn.execute("UPDATE findings SET message=?, line=?, severity=?, last_audit=?, updated=?, status=? WHERE id=?",
                 (f["message"], f["line"], f["severity"], audit_id, ts, "open" if reopened else row["status"], row["id"]))
    return row["id"], "reopened" if reopened else "seen"


def import_findings(conn, actor, lines, source="", kind="import", audit_id=None, note=""):
    """Import JSON-lines findings (all or nothing). Returns (audit id, {'new','seen','reopened'} counts)."""
    items = []
    for i, raw in enumerate(lines, 1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            items.append(_clean(json.loads(raw), source))
        except (ValueError, AesirError) as e:
            raise AesirError(f"findings line {i}: {e}")
    counts = {"new": 0, "seen": 0, "reopened": 0}
    with tx(conn):
        aid = audit_id or new_audit(conn, actor, kind, note or f"import ({len(items)} findings)")
        seen = set()
        for f in items:
            counts[_upsert(conn, f, aid, seen)[1]] += 1
        event(conn, actor, "audit", aid, "import", counts)
    return aid, counts


def add_finding(conn, actor, file, line, severity, rule, message, source="manual", audit_id=None):
    f = _clean({"file": file, "line": line, "severity": severity, "rule": rule, "message": message,
                "source": source})
    with tx(conn):
        if audit_id is not None:
            get_audit(conn, audit_id)
        aid = audit_id if audit_id is not None else new_audit(conn, actor, "import", "manual finding")
        fid, _ = _upsert(conn, f, aid, set())
        event(conn, actor, "finding", fid, "add", {"file": f["file"], "rule": f["rule"]})
    return fid


def set_finding(conn, actor, fid, status=None, task=None, note=None):
    r = conn.execute("SELECT * FROM findings WHERE id=?", (fid,)).fetchone()
    if not r:
        raise AesirError(f"no finding {fid}")
    if status is not None and status not in STATUSES:
        raise AesirError(f"status must be one of {', '.join(STATUSES)}")
    if task is not None and not conn.execute("SELECT 1 FROM tasks WHERE id=?", (task,)).fetchone():
        raise AesirError(f"no task {task}")
    with tx(conn):
        conn.execute("UPDATE findings SET status=?, task_id=?, note=?, updated=? WHERE id=?",
                     (status or r["status"], task if task is not None else r["task_id"],
                      note if note is not None else r["note"], now(), fid))
        event(conn, actor, "finding", fid, "set", {"status": status, "task": task, "note": note})


def baseline(conn, actor, rule, **flt):
    """Baseline rollout (#082): mark every open finding of `rule` (narrowed by the other filters) as expected (status
    baselined, distinct from wontfix: to be fixed, the count shrinks), so only new hits stand out.
    Returns how many were baselined. Fixing one later: status fixed."""
    flt.pop("status", None)
    w, p = _where(rule=rule, status="open", **flt)
    ids = [r[0] for r in conn.execute(f"SELECT id FROM findings{w}", p)]
    with tx(conn):
        conn.executemany("UPDATE findings SET status='baselined', updated=? WHERE id=?", [(now(), i) for i in ids])
        event(conn, actor, "finding", rule, "baseline", {"count": len(ids)})
    return len(ids)


def _where(file=None, severity=None, rule=None, source=None, status=None, since=None, area=None):
    w, p = [], []
    for col, v in (("file", file), ("rule", rule), ("source", source), ("status", status)):
        if v:
            w.append(f"{col}=?")
            p.append(_norm_file(v) if col == "file" else v)
    if severity:  # this severity and worse
        if severity not in SEVERITIES:
            raise AesirError(f"severity must be one of {', '.join(SEVERITIES)}")
        worse = SEVERITIES[SEVERITIES.index(severity):]
        w.append("severity IN (%s)" % ",".join("?" * len(worse)))
        p += worse
    if since is not None:
        w.append("first_audit>?")
        p.append(since)
    if area:
        a = _norm_file(area).rstrip("/")
        esc = a.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        w.append("(file=? OR file LIKE ? ESCAPE '\\')")
        p += [a, esc + "/%"]
    return (" WHERE " + " AND ".join(w)) if w else "", p


_RANK = "CASE severity WHEN 'high' THEN 3 WHEN 'medium' THEN 2 WHEN 'low' THEN 1 ELSE 0 END"


def list_findings(conn, **flt):
    """Ranked: worst severity first, then file and line."""
    w, p = _where(**flt)
    return [dict(r) for r in conn.execute(
        f"SELECT * FROM findings{w} ORDER BY {_RANK} DESC, file, line, id", p)]


def digest(conn, by="area", depth=1, **flt):
    """Findings grouped by area/severity/file/rule/source, ranked by worst severity then count (a summary, not rows)."""
    w, p = _where(**flt)
    rows = conn.execute(f"SELECT file, severity, rule, source FROM findings{w}", p).fetchall()
    groups = {}
    for r in rows:
        key = {"area": "/".join(r["file"].split("/")[:depth]) if "/" in r["file"] else ".",
               "severity": r["severity"], "file": r["file"], "rule": r["rule"], "source": r["source"]}[by]
        g = groups.setdefault(key, {"key": key, "count": 0, "worst": "info", "high": 0})
        g["count"] += 1
        g["high"] += r["severity"] == "high"
        if SEVERITIES.index(r["severity"]) > SEVERITIES.index(g["worst"]):
            g["worst"] = r["severity"]
    return sorted(groups.values(), key=lambda g: (-SEVERITIES.index(g["worst"]), -g["count"], g["key"]))


# ---- audit plan ----

def tracked_files(root):
    r = proc.run(["git", "ls-files", "-z"], cwd=root, capture_output=True)
    if r.returncode:
        raise AesirError(f"git ls-files failed: {r.stderr.decode('utf-8', 'replace').strip()}")
    return [f for f in r.stdout.decode("utf-8", "replace").split("\0") if f]


def _weight(path):
    """Lines in a text file; None for a binary or unreadable file (not auditable by reading)."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    if b"\0" in data[:8192]:
        return None
    return max(1, data.count(b"\n") + (not data.endswith(b"\n")))


ALWAYS_EXCLUDED = (".aesir-pkg", ".aesir")  # vendored Aesir copy and its state: never the project's own code (aesir-reports#40)


def excluded(path, globs=()):
    """The one exclusion rule for plans and toolkit runs: any path segment in ALWAYS_EXCLUDED, or a glob matching the
    path or its basename. Applied in the core so every toolkit, built in or custom, is covered the same way."""
    path = _norm_file(path)
    if set(path.split("/")) & set(ALWAYS_EXCLUDED):
        return True
    return any(fnmatch.fnmatch(path, g) or fnmatch.fnmatch(Path(path).name, g) for g in globs)


TOOLKIT_EXTS = {"rust": (".rs",), "python": (".py", ".pyi")}  # file extensions each shipped toolkit covers


def toolkit_exts(cfg):
    """Extensions covered by the enabled toolkits: the shipped ones above, or [audit.<name>] extensions = [".go"] for a
    custom toolkit (a custom toolkit that lists none covers nothing here)."""
    exts = set()
    for name in cfg["toolkits"]:
        c = cfg["raw"].get(name, {})
        own = c.get("extensions") if isinstance(c, dict) else None
        if isinstance(own, list):
            exts |= {e.lower() for e in own if isinstance(e, str)}
        else:
            exts |= set(TOOLKIT_EXTS.get(name, ()))
    return exts


def covered_files(root, cfg=None):
    """Tracked, non-excluded files an enabled toolkit covers (by extension): the files an audit can say anything about."""
    cfg = cfg or load_config(root)
    exts = toolkit_exts(cfg)
    return [f for f in tracked_files(root) if Path(f).suffix.lower() in exts and not excluded(f, cfg["exclude"])]


def _drop_excluded(lines, globs):
    """Remove finding lines whose file is excluded; lines that do not parse pass through so import reports them."""
    keep = []
    for ln in lines:
        try:
            d = json.loads(ln)
            if isinstance(d, dict) and isinstance(d.get("file"), str) and excluded(d["file"], globs):
                continue
        except ValueError:
            pass
        keep.append(ln)
    return keep


def shard(weights, n):
    """Greedy balancing: heaviest file first into the lightest shard. A partition: no file is in two shards."""
    bins = [{"files": [], "weight": 0} for _ in range(n)]
    for f, w in sorted(weights.items(), key=lambda kv: (-kv[1], kv[0])):
        b = min(bins, key=lambda b: (b["weight"], len(b["files"])))
        b["files"].append(f)
        b["weight"] += w
    for b in bins:
        b["files"].sort()
    return [b for b in bins if b["files"]]


def plan(conn, actor, root, agents, exclude=(), note=""):
    if agents < 1:
        raise AesirError("--agents must be >= 1")
    globs = list(load_config(root)["exclude"]) + list(exclude)
    weights, skipped = {}, 0
    for f in tracked_files(root):
        if excluded(f, globs):
            skipped += 1
            continue
        w = _weight(Path(root) / f)
        if w is None:
            skipped += 1
        else:
            weights[f] = w
    if not weights:
        raise AesirError("no auditable tracked files")
    bins = shard(weights, agents)
    with tx(conn):
        aid = new_audit(conn, actor, "plan", note or f"{len(weights)} files, {len(bins)} shards")
        for i, b in enumerate(bins, 1):
            conn.execute("INSERT INTO audit_shards (audit_id, n, files, weight) VALUES (?,?,?,?)",
                         (aid, i, json.dumps(b["files"]), b["weight"]))
        event(conn, actor, "audit", aid, "plan", {"files": len(weights), "shards": len(bins), "skipped": skipped})
    return aid, skipped


def get_shard(conn, aid, n):
    get_audit(conn, aid)
    r = conn.execute("SELECT * FROM audit_shards WHERE audit_id=? AND n=?", (aid, n)).fetchone()
    if not r:
        raise AesirError(f"audit {aid} has no shard {n}")
    d = dict(r)
    d["files"] = json.loads(d["files"])
    return d


def mark_shard_done(conn, actor, aid, n):
    get_shard(conn, aid, n)
    with tx(conn):
        conn.execute("UPDATE audit_shards SET done=? WHERE audit_id=? AND n=?", (now(), aid, n))
        event(conn, actor, "audit", aid, "shard_done", {"shard": n})


# ---- toolkit run ----

# Passed to a shipped toolkit by `audit run` (the one place that resolves its settings). A toolkit started without it
# refuses unless given NO_CONFIG_FLAG, so a bare run never silently skips [audit] / [practices] settings (#114).
RUN_FLAG = "--via-audit-run"
NO_CONFIG_FLAG = "--no-config"
BUILTIN = {"rust": "aesir-audit-rust", "python": None}  # shipped in toolkits/<name>/ (value: built binary, None = script); used only when enabled


def _builtin_cmd(cfg, name, root):
    """Return the command of a shipped toolkit (cargo-built binary, or a stdlib script run with aesir's interpreter,
    nothing to build). Only reached when [audit] toolkits names it and [audit.<name>] sets no cmd, so
    install/upgrade and projects without it never build anything."""
    if BUILTIN[name] is None:
        script = Path(__file__).resolve().parent.parent / "toolkits" / name / f"audit_{name}.py"
        if not script.exists():
            raise AesirError(f"toolkit '{name}' source not found at {script}")
        c = cfg["raw"].get(name, {})
        base = c.get("base") if isinstance(c, dict) else None
        return f'"{sys.executable}" "{script}"' + f" {RUN_FLAG}" + (f' --base "{base}"' if base else "")
    crate = Path(__file__).resolve().parent.parent / "toolkits" / name
    if not (crate / "Cargo.toml").exists():
        raise AesirError(f"toolkit '{name}' source not found at {crate}")
    if not shutil.which("cargo"):
        raise AesirError(f"the {name} toolkit needs cargo (the Rust toolchain) on PATH: install it from https://rustup.rs, "
                         f'or drop "{name}" from [audit] toolkits in aesir.toml')
    target = Path(root) / ".aesir" / "toolkits" / name
    try:
        r = proc.run(["cargo", "build", "--release", "--quiet", "--manifest-path", str(crate / "Cargo.toml"),
                            "--target-dir", str(target)], capture_output=True, timeout=cfg["timeout"])
    except subprocess.TimeoutExpired:
        raise AesirError(f"building the {name} toolkit timed out after {cfg['timeout']}s")
    if r.returncode:
        tail = r.stderr.decode("utf-8", "replace").strip().splitlines()[-3:]
        raise AesirError(f"building the {name} toolkit failed: " + " | ".join(tail))
    exe = target / "release" / (BUILTIN[name] + (".exe" if os.name == "nt" else ""))
    return f'"{exe}" {RUN_FLAG}{_rust_args(cfg, root)}'


def _rust_args(cfg, root):
    """Command-line options of the Rust toolkit from [audit.rust] (base, scope tokens, hostile globs, max_file_lines)
    and the [practices] flags networked / realtime, which switch on the conditional rule groups."""
    c = cfg["raw"].get("rust", {})
    c = c if isinstance(c, dict) else {}
    args = "".join(f' --{k} "{c[k]}"' for k in ("base", "scope") if k in c)
    args += "".join(f' --hostile "{g}"' for g in c.get("hostile", []))
    if "max_file_lines" in c:
        args += f' --max-file-lines {c["max_file_lines"]}'
    try:
        flags = gates.load_config(root).get("practices", {})
    except AesirError:
        flags = {}
    return args + "".join(f" --{k}" for k in ("networked", "realtime") if flags.get(k) is True)


def toolkit_cmd(cfg, name, root=None):
    c = cfg["raw"].get(name, {})
    cmd = c.get("cmd") if isinstance(c, dict) else None
    if (not isinstance(cmd, str) or not cmd.strip()) and name in BUILTIN and root is not None:
        return _builtin_cmd(cfg, name, root)
    if not isinstance(cmd, str) or not cmd.strip():
        raise AesirError(f"toolkit '{name}' has no command: set [audit.{name}] cmd in aesir.toml "
                         "(a command that prints core-format findings, JSON lines, to stdout)")
    return cmd


def _toolkit_lines(cfg, name, root, cwd):
    """Run toolkit `name` (config and build from `root`) in directory `cwd`; its findings lines, excluded paths dropped.
    Raises AesirError on timeout or a non-zero exit."""
    cmd = toolkit_cmd(cfg, name, root)
    try:
        r = proc.run(cmd, shell=True, cwd=cwd, capture_output=True, timeout=cfg["timeout"])
    except subprocess.TimeoutExpired:
        raise AesirError(f"timed out after {cfg['timeout']}s")
    if r.returncode:
        tail = r.stderr.decode("utf-8", "replace").strip().splitlines()[-3:]
        raise AesirError(f"exit {r.returncode}: " + " | ".join(tail))
    return _drop_excluded(r.stdout.decode("utf-8", "replace").splitlines(), cfg["exclude"])


def run_toolkits(conn, actor, root):
    """Run each enabled toolkit and import its findings. None if no toolkit is enabled (nothing is touched);
    else (audit id or None, [(name, counts or None, error)])."""
    cfg = load_config(root)
    if not cfg["toolkits"]:
        return None
    results, audit_id = [], None
    for name in cfg["toolkits"]:
        try:
            lines = _toolkit_lines(cfg, name, root, root)
            if audit_id is None:
                with tx(conn):
                    audit_id = new_audit(conn, actor, "run", "toolkits: " + ", ".join(cfg["toolkits"]))
            _, counts = import_findings(conn, actor, lines, source=name, audit_id=audit_id)
            results.append((name, counts, ""))
        except AesirError as e:
            results.append((name, None, str(e)))
    return audit_id, results


def branch_config(root, cwd):
    """Main's audit config (`root`) widened by the branch checkout's own (#151): the toolkits stay main's (a branch
    cannot switch the audit off), but the [audit] exclude globs and the [audit.rust] hostile globs are the union of
    main's and the branch's, so a branch that adds a hostile glob and the code it names is checked by it at its own
    audit, not only after merge. Other settings (timeout, base, scope) stay main's. A missing or bad branch
    aesir.toml leaves main's config as is."""
    cfg = load_config(root)
    if Path(cwd).resolve() == Path(root).resolve():
        return cfg
    try:
        mine = load_config(cwd)
    except AesirError:
        return cfg
    raw = dict(cfg["raw"])
    rust = dict(raw["rust"]) if isinstance(raw.get("rust"), dict) else {}
    brust = mine["raw"].get("rust")
    hostile = list(rust.get("hostile", []))
    hostile += [g for g in (brust.get("hostile", []) if isinstance(brust, dict) else []) if g not in hostile]
    if hostile:
        rust["hostile"] = hostile
        raw["rust"] = rust
    return {**cfg, "raw": raw, "exclude": cfg["exclude"] + [g for g in mine["exclude"] if g not in cfg["exclude"]]}


def merge_base(cwd, main="main"):
    """Merge base commit of the checkout `cwd` with `main`; None when that cannot be told (not git, no `main`)."""
    try:
        return gates.git(cwd, "merge-base", "HEAD", main)
    except (AesirError, OSError):
        return None


def changed_files(cwd, main="main", base=None):
    """Paths (relative to `cwd`, '/'-separated) the checkout changes versus its merge base with `main`: committed,
    uncommitted and untracked. None when that cannot be told (not a git checkout, no `main`): callers then treat every
    file as changed, never hiding a finding."""
    base = base or merge_base(cwd, main)
    if base is None:
        return None
    try:
        diff = gates.git(cwd, "diff", "--name-only", "--relative", "-z", base)
        other = gates.git(cwd, "ls-files", "--others", "--exclude-standard", "-z")
    except (AesirError, OSError):
        return None
    return {_norm_file(p) for p in (diff + "\0" + other).split("\0") if p}


def _base_fingerprints(root, cwd, base, names):
    """Fingerprints the toolkits `names` find at the merge base `base`, run with main's own config on a temporary
    detached worktree of the branch's repository (removed afterwards). Empty when that cannot be done: a candidate is
    then kept as new, never hidden (#151)."""
    tmp = Path(tempfile.mkdtemp(prefix="aesir-base-"))
    wt = tmp / "base"
    fps, seen = set(), set()
    try:
        gates.git(cwd, "worktree", "add", "--detach", "-q", str(wt), base)
        cfg = load_config(root)
        for name in names:
            try:
                for raw in _toolkit_lines(cfg, name, root, wt):
                    if raw.strip():
                        fps.add(_unique_fp(_clean(json.loads(raw), name), seen))
            except (ValueError, AesirError):
                pass
    except (AesirError, OSError):
        pass
    finally:
        try:
            gates.git(cwd, "worktree", "remove", "--force", str(wt))
        except (AesirError, OSError):
            pass
        shutil.rmtree(tmp, ignore_errors=True)
    return fps


def new_vs_main(conn, root, cwd, pre=None, main="main"):
    """Run the toolkits enabled in main's config (`root`) inside checkout `cwd` (an unmerged branch) and compare with
    main's findings (#115). Read-only: nothing is written to the findings tables. A finding is known when main holds its
    fingerprint in any status but fixed (a fixed one that comes back is new). The branch's own hostile/exclude globs are
    added to main's (branch_config). A finding not known on main counts as new only when it lies in a file the branch
    changes versus the merge base (changed_files); one in an untouched file is main's own, not yet recorded, and is
    appended to the list `pre` when given, not returned (#151). A candidate in a changed file is also checked against
    the merge base: the toolkits run once more, lazily and only when there are candidates, on a temporary worktree at
    the merge base with main's config, and a fingerprint found there is main's own (unchanged line, not yet recorded),
    so it goes to `pre` too. Returns (new findings, errors, exit code):
    the new ones as ranked dicts, [(toolkit, message)] for toolkits that failed, and 0 clean / 1 new findings / 2 a
    toolkit failed (a clean result is not claimed when part of the audit did not run). A gate step can call this."""
    cfg = branch_config(root, cwd)
    base = merge_base(cwd, main)  # `main` is an unmerged base task's branch for a task built on one (#177)
    changed = changed_files(cwd, base=base)
    known = {r[0] for r in conn.execute("SELECT fingerprint FROM findings WHERE status!='fixed'")}
    cand, errors, seen = [], [], set()
    for name in cfg["toolkits"]:
        try:
            for i, raw in enumerate(_toolkit_lines(cfg, name, root, cwd), 1):
                if not raw.strip():
                    continue
                try:
                    f = _clean(json.loads(raw), name)
                except (ValueError, AesirError) as e:
                    raise AesirError(f"findings line {i}: {e}")
                fp = _unique_fp(f, seen)
                if fp in known:
                    continue
                if changed is None or f["file"] in changed:
                    cand.append((fp, f, name))
                elif pre is not None:
                    pre.append(f)
        except AesirError as e:
            errors.append((name, str(e)))
    at_base = _base_fingerprints(root, cwd, base, sorted({n for _, _, n in cand})) if cand and base else set()
    new = []
    for fp, f, _ in cand:
        if fp in at_base:
            if pre is not None:
                pre.append(f)
        else:
            new.append(f)
    new.sort(key=lambda f: (-SEVERITIES.index(f["severity"]), f["file"], f["line"]))
    return new, errors, 2 if errors else (1 if new else 0)
