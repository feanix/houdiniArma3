"""Audit benchmarks (task #93): score an audit toolkit's findings against a recorded answer key.

A benchmark is a directory under tests/benchmarks/<name>/ holding key.json (the known findings, no source code) and a
README. `run` clones the keyed repo at a tag into a temp dir, runs the Rust toolkit there, and `score` reports recall by
severity, category and area, the High findings one by one, and the share of toolkit findings that hit no known problem.
Needs the network and cargo, so it is a manual command (`audit benchmark <name>`), never part of the gate.

Method (adapted from the Tetris #54 measurement scripts): a key finding is caught when a toolkit finding lies in one of
its anchor regions. A region is the enclosing fn of the anchor's first and last line (or +-3 lines outside a fn, or any
non-Rust file); a `FILE` anchor covers the whole file. The Rust toolkit emits one finding per rule and file (its first
line), so recall here is a floor on what the toolkit sees, not a count of every occurrence."""
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

from . import proc
from . import audit
from .service import AesirError

BENCH_DIR = Path(__file__).resolve().parent.parent / "tests" / "benchmarks"
SEVERITIES = ("high", "medium", "low")
CLONE_TIMEOUT = 300  # seconds
# Rules that fire on size or missing docs, not on a defect class: a hit from only these is a coincidence of location, so
# the strict figure ignores them (the loose one counts any finding in the region).
HEURISTIC_RULES = {"clippy::too_many_lines", "clippy::cognitive_complexity", "missing_docs", "doc_coverage",
                   "diff::comment_only"}


class Skipped(Exception):
    """The benchmark cannot run here (no network, no cargo, no key); the message says why."""


# ---- key ----

def load_key(name):
    p = BENCH_DIR / name / "key.json"
    if not p.is_file():
        known = sorted(d.name for d in BENCH_DIR.iterdir() if (d / "key.json").is_file()) if BENCH_DIR.is_dir() else []
        raise AesirError(f"no benchmark '{name}' (known: {', '.join(known) or 'none'})")
    return json.loads(p.read_text(encoding="utf-8"))


# ---- crude Rust fn extents (1-based, inclusive) ----

_FN_RE = re.compile(r'^\s*(?:pub(?:\([^)]*\))?\s+)?(?:const\s+)?(?:async\s+)?(?:unsafe\s+)?(?:extern\s+"[^"]*"\s+)?fn\s+(\w+)')


def _strip(s):
    """Drop strings, chars and line comments so braces inside them do not count."""
    s = re.sub(r'"(?:\\.|[^"\\])*"', '""', s)
    s = re.sub(r"'(?:\\.|[^'\\])'", "''", s)
    i = s.find("//")
    return s[:i] if i >= 0 else s


def fn_ranges(text):
    lines = text.split("\n")
    clean = [_strip(x) for x in lines]
    out = []
    for i, line in enumerate(lines):
        m = _FN_RE.match(line)
        if not m:
            continue
        depth, started, j = 0, False, i
        while j < len(lines):
            for ch in clean[j]:
                if ch == "{":
                    depth += 1
                    started = True
                elif ch == "}":
                    depth -= 1
                elif ch == ";" and not started and depth == 0:
                    started = None  # declaration without a body
                    break
            if started is None or (started and depth <= 0):
                break
            j += 1
        if started:
            out.append((m.group(1), i + 1, j + 1))
    return out


def enclosing(ranges, line):
    """Innermost fn containing the line, as (name, start, end), or None."""
    best = None
    for name, a, b in ranges:
        if a <= line <= b and (best is None or b - a < best[2] - best[1]):
            best = (name, a, b)
    return best


def _region(repo, file, spec, cache):
    if spec == "FILE":
        return 1, 10 ** 9
    a, _, b = spec.partition("-")
    a, b = int(a), int(b or a)
    if file not in cache:
        try:
            cache[file] = fn_ranges((Path(repo) / file).read_text(encoding="utf-8")) if file.endswith(".rs") else []
        except (OSError, UnicodeDecodeError):
            cache[file] = []
    ea, eb = enclosing(cache[file], a), enclosing(cache[file], b)
    return (ea[1] if ea else a - 3), (eb[2] if eb else b + 3)


# ---- scoring ----

def parse_findings(lines):
    """Toolkit stdout (JSON lines, core format v1) to dicts; blank lines skipped, anything else must be a finding."""
    out = []
    for n, raw in enumerate(lines, 1):
        if raw.strip():
            try:
                d = json.loads(raw)
            except ValueError as e:
                raise AesirError(f"toolkit output line {n} is not JSON: {e}")
            out.append({"file": str(d.get("file", "")).replace("\\", "/"), "line": int(d.get("line", 0)),
                        "severity": d.get("severity", ""), "rule": d.get("rule", "")})
    return out


def score(key, findings, repo):
    """Match toolkit findings to key findings; `repo` is the checkout at the keyed commit (for fn extents)."""
    cache, regions = {}, {}
    for kf in key["findings"]:
        regions[kf["id"]] = [(a["file"],) + _region(repo, a["file"], s, cache) for a in kf["anchors"] for s in a["lines"]]
    caught, tied = {}, set()
    for kf in key["findings"]:
        for i, f in enumerate(findings):
            if any(f["file"] == file and lo <= f["line"] <= hi for file, lo, hi in regions[kf["id"]]):
                caught.setdefault(kf["id"], []).append(f["rule"])
                tied.add(i)
    strict = {k for k, v in caught.items() if set(v) - HEURISTIC_RULES}
    res = {"total": len(key["findings"]), "caught": len(caught), "caught_strict": len(strict),
           "caught_ids": sorted(caught), "strict_ids": sorted(strict),
           "caught_by": {k: sorted(set(v)) for k, v in sorted(caught.items())},
           "missed_ids": [k["id"] for k in key["findings"] if k["id"] not in caught],
           "unanchored": [k["id"] for k in key["findings"] if not k["anchors"]]}
    for dim in ("severity", "category", "area"):
        groups = {}  # group -> [caught, total, caught strictly]
        for kf in key["findings"]:
            g = groups.setdefault(kf[dim], [0, 0, 0])
            g[1] += 1
            g[0] += kf["id"] in caught
            g[2] += kf["id"] in strict
        res["by_" + dim] = groups
    res["highs"] = [{"id": k["id"], "caught": k["id"] in caught, "rules": sorted(set(caught.get(k["id"], [])))}
                    for k in key["findings"] if k["severity"] == "high"]
    res["emitted"] = len(findings)
    res["false_alarms"] = len(findings) - len(tied)
    return res


def _pct(a, b):
    return f"{100 * a / b:.0f}%" if b else "n/a"


def render(res):
    out = [f"benchmark {res.get('benchmark', '?')} @ {res.get('tag', '?')}: toolkit {res.get('toolkit', '?')}",
           f"recall {res['caught']}/{res['total']} ({_pct(res['caught'], res['total'])}): a toolkit finding lies in the known problem's region",
           f"strict recall {res['caught_strict']}/{res['total']} ({_pct(res['caught_strict'], res['total'])}): "
           "same, ignoring size/complexity/docs rules",
           f"{len(res['unanchored'])} key findings have no code anchor and cannot be caught",
           "tables: caught/total (strict)"]
    for dim in ("severity", "category", "area"):
        groups = res["by_" + dim]
        order = [s for s in SEVERITIES if s in groups] if dim == "severity" else sorted(groups)
        out.append(f"by {dim}: " + ", ".join(f"{g} {groups[g][0]}/{groups[g][1]} ({groups[g][2]})" for g in order))
    out.append("Highs:")
    for h in res["highs"]:
        out.append(f"  {h['id']}: " + (f"hit by {', '.join(h['rules'])}" if h["caught"] else "MISSED"))
    out.append(f"false alarms: {res['false_alarms']}/{res['emitted']} toolkit findings hit no known problem "
               f"({_pct(res['false_alarms'], res['emitted'])})")
    return out


# ---- run ----

def _rmtree(path):
    """Git object files are read-only, which makes rmtree fail on Windows."""
    def fix(fn, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        fn(p)
    shutil.rmtree(path, onerror=fix)


def _clone(key, dest):
    url = f"https://github.com/{key['source']}"
    try:
        r = proc.run(["git", "clone", "--quiet", "--depth", "1", "--branch", key["tag"], url, str(dest)],
                           capture_output=True, timeout=CLONE_TIMEOUT, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except FileNotFoundError:
        raise Skipped("git is not on PATH")
    except subprocess.TimeoutExpired:
        raise Skipped(f"clone of {url} timed out after {CLONE_TIMEOUT}s")
    if r.returncode:
        tail = r.stderr.decode("utf-8", "replace").strip().splitlines()[-1:]
        raise Skipped(f"cannot clone {url} at {key['tag']} (repo unreachable or tag missing): {' '.join(tail)}")


def _aesir_commit(root):
    try:
        r = proc.run(["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True, timeout=30)
        return r.stdout.decode().strip() if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def run(name, root, source=None):
    """Run the Rust toolkit on the keyed repo and score it. `source` = an existing checkout to use instead of a clone.
    Raises Skipped if the benchmark cannot run here. Returns the result dict (see `score`)."""
    key = load_key(name)
    if not shutil.which("cargo"):
        raise Skipped("cargo (the Rust toolchain) is not on PATH: install it from https://rustup.rs")
    cfg = audit.load_config(root)
    tmp = Path(tempfile.mkdtemp(prefix="aesir-bench-"))
    try:
        if source:
            repo = Path(source)
            if not repo.is_dir():
                raise AesirError(f"--source {source} is not a directory")
        else:
            repo = tmp / "repo"
            _clone(key, repo)
        try:
            # the toolkit is built into <root>/.aesir/toolkits (cached), then run inside the clone
            cmd = audit.toolkit_cmd(cfg, "rust", root) + " --base HEAD"  # a shallow clone has no 'main' to diff against
            r = proc.run(cmd, shell=True, cwd=repo, capture_output=True, timeout=cfg["timeout"])
        except subprocess.TimeoutExpired:
            raise AesirError(f"toolkit timed out after {cfg['timeout']}s")
        if r.returncode:
            tail = r.stderr.decode("utf-8", "replace").strip().splitlines()[-3:]
            raise AesirError(f"toolkit exit {r.returncode}: " + " | ".join(tail))
        res = score(key, parse_findings(r.stdout.decode("utf-8", "replace").splitlines()), repo)
    finally:
        _rmtree(tmp)
    res.update(benchmark=name, toolkit="rust", tag=key["tag"], commit=key["commit"], aesir_commit=_aesir_commit(root))
    return res
