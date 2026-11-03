"""Aesir audit toolkit for Python (task #69). Prints findings (format v1, docs/audit-findings.md) as JSON lines.

Run by `aesir audit run`, which resolves aesir.toml and passes `--via-audit-run`. Started bare it refuses (it would skip
the project's settings) unless given `--no-config`. Options: `[--base REF] [--no-config]`. Stdlib only. Three checks:
  1. doc coverage (ast; test files: module only): modules and public or long functions/classes with neither a docstring nor a comment
     directly above, one finding per file, most missing first;
  2. ruff with the lint preset below, when ruff is installed (else one info finding says it was skipped);
  3. comment-only diff vs a base branch (ast-based, conservative: anything unparsable counts as code).
Any hard failure prints on stderr and exits 1, so the core imports nothing from a half-run."""
import ast
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

SKIP_DIRS = {".aesir-pkg", ".venv", "venv", "node_modules", "__pycache__", "site-packages", "build", "dist"}
LONG_DEF = 30  # lines: a private function this long still needs a doc

# Closest ruff rules for the Rust preset (#82). Rule -> severity. Unchecked indexing has no ruff rule: not covered.
RUFF_PRESET = {
    "E722": "medium",    # bare except
    "BLE001": "medium",  # blind `except Exception`
    "S110": "medium",    # try/except/pass: swallowed error
    "S112": "medium",    # try/except/continue: swallowed error
    "S101": "medium",    # assert outside tests (stripped under -O)
    "C901": "low",       # too complex (mccabe)
    "PLR0912": "low",    # too many branches
    "PLR0915": "low",    # too many statements
}
RUFF_CONFIG = """[lint]
select = [%s]
[lint.mccabe]
max-complexity = 15
[lint.pylint]
max-branches = 15
max-statements = 60
""" % ", ".join(f'"{r}"' for r in RUFF_PRESET)
RANK = {"info": 0, "low": 1, "medium": 2, "high": 3}


def finding(file, line, severity, rule, message):
    return {"v": 1, "file": file, "line": line, "severity": severity, "rule": rule, "message": message}


def git(root, *args):
    try:
        r = subprocess.run(["git", *args], cwd=root, capture_output=True)
    except OSError:
        return None
    return r.stdout if r.returncode == 0 else None


def python_files(root):
    """Tracked .py files (git), or a plain walk outside a repository; vendored and dot folders are skipped."""
    out = git(root, "ls-files", "-z", "--", "*.py")
    if out is not None:
        names = [s for s in out.decode("utf-8", "replace").split("\0") if s]
        names = [n for n in names if os.path.exists(os.path.join(root, n))]
    else:
        names = []
        for d, dirs, files in os.walk(root):
            dirs[:] = [x for x in dirs if x not in SKIP_DIRS and not x.startswith(".")]
            names += [os.path.relpath(os.path.join(d, f), root).replace("\\", "/") for f in files if f.endswith(".py")]
    return sorted(n for n in names if not SKIP_DIRS & set(n.split("/")))


def is_test_file(path):
    parts = path.split("/")
    name = parts[-1]
    return (any(p in ("tests", "test") for p in parts[:-1]) or name.startswith("test_")
            or name.endswith("_test.py") or name == "conftest.py")


# ---- 1. doc coverage ----

def doc_coverage(root, files):
    res = []
    for path in files:
        try:
            src = open(os.path.join(root, path), encoding="utf-8").read()
            tree = ast.parse(src)
        except (OSError, SyntaxError, ValueError):
            continue
        lines = src.splitlines()
        total = len(lines)
        missing = []  # (size, line, kind, name)
        if total >= 5 and not ast.get_docstring(tree) and not has_header_comment(lines):
            missing.append((total, 1, "module", path))
        for node in ast.walk(tree):
            if is_test_file(path):
                break  # test cases name themselves: only the module counts
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            size = (node.end_lineno or node.lineno) - node.lineno + 1
            public = not node.name.startswith("_")
            if not (public or size >= LONG_DEF) or ast.get_docstring(node) or comment_above(lines, node):
                continue
            if size < 3:
                continue  # one-liners explain themselves
            kind = "class" if isinstance(node, ast.ClassDef) else "function"
            missing.append((size, node.lineno, kind, node.name))
        if not missing:
            continue
        missing.sort(reverse=True)
        top = ", ".join(f"{k} {n} ({s} lines)" for s, _, k, n in missing[:3])
        sev = "low" if any(s >= 40 for s, *_ in missing) else "info"
        res.append((len(missing), sum(m[0] for m in missing), finding(
            path, missing[0][1], sev, "doc_coverage",
            f"{len(missing)} without docstring or comment, largest: {top}")))
    res.sort(key=lambda t: (-t[0], -t[1], t[2]["file"]))
    return [f for _, _, f in res]


def has_header_comment(lines):
    for ln in lines[:10]:
        s = ln.strip()
        if s.startswith("#!") or s.startswith("# -*-") or not s:
            continue
        return s.startswith("#")
    return False


def comment_above(lines, node):
    first = min([node.lineno] + [d.lineno for d in node.decorator_list])
    return first >= 2 and lines[first - 2].strip().startswith("#")


# ---- 2. ruff ----

def ruff_cmd():
    exe = shutil.which("ruff")
    if exe:
        return [exe]
    try:
        r = subprocess.run([sys.executable, "-m", "ruff", "--version"], capture_output=True)
    except OSError:
        return None
    return [sys.executable, "-m", "ruff"] if r.returncode == 0 else None


def ruff_findings(root, files):
    cmd = ruff_cmd()
    if cmd is None:
        sys.stderr.write("aesir-audit-python: ruff not installed, linter source skipped\n")
        return [finding(".", 0, "info", "ruff::not_installed",
                        "ruff is not installed: lint checks skipped (pip install ruff to enable them)")]
    if not files:
        return []
    with tempfile.TemporaryDirectory() as td:
        conf = os.path.join(td, "ruff.toml")
        with open(conf, "w", encoding="utf-8") as f:
            f.write(RUFF_CONFIG)
        r = subprocess.run(cmd + ["check", "--config", conf, "--output-format", "json", "--no-cache",
                                  "--force-exclude", "--exit-zero", "--", *files],
                           cwd=root, capture_output=True)
    if r.returncode not in (0, 1):
        tail = r.stderr.decode("utf-8", "replace").strip().splitlines()[-3:]
        raise RuntimeError(f"ruff failed (exit {r.returncode}): " + " | ".join(tail))
    try:
        items = json.loads(r.stdout.decode("utf-8", "replace") or "[]")
    except ValueError as e:
        raise RuntimeError(f"ruff printed bad json: {e}")
    groups = {}
    for it in items:
        rule = it.get("code") or ""
        if rule not in RUFF_PRESET:
            continue
        path = os.path.relpath(it["filename"], root).replace("\\", "/")
        if rule == "S101" and is_test_file(path):
            continue
        row = (it.get("location") or {}).get("row", 0)
        g = groups.setdefault((rule, path), [0, row, it.get("message", "")])
        g[0] += 1
        g[1] = min(g[1], row) if g[1] else row
    out = []
    for (rule, path), (n, row, msg) in groups.items():
        sev = RUFF_PRESET[rule]
        out.append(finding(path, row, sev, f"ruff::{rule}", f"{msg}" + (f" ({n} occurrences in file)" if n > 1 else "")))
    return out


# ---- 3. comment-only diff ----

def normalize(src):
    """Canonical dump of the code in `src` (comments never reach the tree; docstrings are cut), or None if unparsable."""
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError):
        return None
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            b = node.body
            if b and isinstance(b[0], ast.Expr) and isinstance(b[0].value, ast.Constant) and isinstance(b[0].value.value, str):
                node.body = b[1:] or [ast.Pass()]
    return ast.dump(tree)


def comment_only(root, base):
    """Modified .py files (branch plus working tree vs the merge base with `base`) with no code change."""
    mb = git(root, "merge-base", "HEAD", base)
    if mb is None:
        return []
    mb = mb.decode().strip()
    names = git(root, "diff", "--name-status", "--no-renames", "-z", mb)
    if names is None:
        return []
    parts = [s for s in names.decode("utf-8", "replace").split("\0") if s]
    res = []
    for status, path in zip(parts[0::2], parts[1::2]):
        if status != "M" or not path.endswith(".py"):
            continue
        old = git(root, "show", f"{mb}:{path}")
        try:
            new = open(os.path.join(root, path), encoding="utf-8").read()
        except OSError:
            continue
        if old is None:
            continue
        a, b = normalize(old.decode("utf-8", "replace")), normalize(new)
        if a is not None and a == b:
            res.append(finding(path, 0, "info", "diff::comment_only",
                               f"no code change vs {base}: syntax tree identical apart from comments, docstrings and layout"))
    return res


def find_config(start):
    """The aesir.toml in start or the nearest parent, or None."""
    for d in [start, *start.parents]:
        if (d / "aesir.toml").is_file():
            return d / "aesir.toml"
    return None


def main(argv):
    base = "main"
    via_run = no_config = False
    it = iter(argv)
    for a in it:
        if a == "--via-audit-run":  # keep in step with audit.RUN_FLAG
            via_run = True
        elif a == "--no-config":  # keep in step with audit.NO_CONFIG_FLAG
            no_config = True
        elif a == "--base":
            base = next(it, None)
            if not base:
                raise RuntimeError("--base needs a value")
        else:
            raise RuntimeError(f"unknown argument {a} (usage: [--base REF] [--no-config])")
    if not (via_run or no_config):
        found = find_config(pathlib.Path.cwd())
        raise RuntimeError(
            "run bare, this toolkit would skip the project's settings"
            + (f" in {found}" if found else " (no aesir.toml found above here)")
            + ": use `aesir audit run`, or pass --no-config for an explicit run without them")
    root = os.getcwd()
    files = python_files(root)
    out = doc_coverage(root, files) + ruff_findings(root, files) + comment_only(root, base)
    out.sort(key=lambda f: -RANK[f["severity"]])  # stable: each check's own order survives within a severity
    for f in out:
        print(json.dumps(f))


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except RuntimeError as e:
        sys.stderr.write(f"aesir-audit-python: {e}\n")
        sys.exit(1)
