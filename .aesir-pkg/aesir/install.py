"""Releases and client installs. `release` tags main; `install`/`upgrade` vendor the package at a tag into a client.

Decision: vendored copy (<client>/.aesir-pkg/aesir, from `git archive` at the tag), not pip. Aesir is stdlib-only, so
there is no dependency step, no network at runtime, and the pin is the copy itself. Hooks run it through the launcher
.aesir-pkg/aesir_run.py (puts its own folder on sys.path), so no PYTHONPATH syntax differs per shell."""
import io
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import tomllib
from pathlib import Path

from . import proc
from .invoke import CLIENT as CLIENT_CMD, DEV as DEV_CMD
from .service import AesirError

PKG_DIR = ".aesir-pkg"
LAUNCHER = "aesir_run.py"
HOOK_EVENTS = (("SessionStart", None), ("UserPromptSubmit", None), ("PreToolUse", "*"),
               ("SubagentStart", None), ("SubagentStop", None),
               ("PostToolUseFailure", "*"))
HOOK_CMD = f'python "$CLAUDE_PROJECT_DIR/{PKG_DIR}/{LAUNCHER}" hook'
LAUNCHER_SRC = ("import sys\nfrom pathlib import Path\n\n"
                "sys.path.insert(0, str(Path(__file__).resolve().parent))\n"
                "from aesir.cli import launcher_main\n\nsys.exit(launcher_main(Path(__file__).resolve().parent))\n")
PKG_README = ("# .aesir-pkg\n\nVendored Aesir (read-only; change it with `aesir upgrade`).\n\n"
              "Guides for agents: `.aesir-pkg/docs/` (start with `.aesir-pkg/docs/observe-drive.md`: how to "
              "observe and drive the running app instead of asking the owner to describe it) and "
              "`.aesir-pkg/docs/reporting.md` (what to report, kinds, offline queue, who messages whom) and "
              "`.aesir-pkg/docs/practices.md` (optional recommended practices and how to adopt them) and "
              "`.aesir-pkg/docs/agents.md` (project additions to agent defs via `.claude/agents/<name>.local.md`) and "
              "`.aesir-pkg/docs/visibility.md` (build visibility first: saved state, digest tools, event log) and `.aesir-pkg/docs/supervision.md` (supervising long runs: rounds, waiter, rule 11).\n")
PKG_README_FILE = "pkg-readme.md"  # under docs/: the TARGET version's text; PKG_README above is the fallback
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def _git(repo, *args, binary=False):
    r = proc.run(["git", "-C", str(repo), *args], capture_output=True)
    if r.returncode:
        raise AesirError(f"git {' '.join(args)} failed: {r.stderr.decode(errors='replace').strip()}")
    return r.stdout if binary else r.stdout.decode(errors="replace").strip()


def parse_version(v):
    m = SEMVER.match(v or "")
    if not m:
        raise AesirError(f"version must look like X.Y.Z, got {v!r}")
    return tuple(int(x) for x in m.groups())


def default_source():
    return Path(__file__).resolve().parent.parent


# ---- release (lead, in the Aesir repo) ----

CHANGELOG_HEAD = ("# Changelog\n\nAgent-facing: read the entries between your pinned version and the one you "
                  "upgrade to.\n")


FRAGMENTS_DIR = "changes"
VERSION_HEADING = re.compile(r"\A\s*## \d[^\n]*(?:\n|\Z)")  # a pasted '## 0.6.0 (date)' line; release writes its own
UNRELEASED = re.compile(r"^## Unreleased[ \t]*\n(.*?)(?=^## |\Z)", re.S | re.M)


def _fragments(repo):
    """Non-empty changes/<task>.md files, numeric task id order (non-numeric names last, by name)."""
    found = sorted((Path(repo) / FRAGMENTS_DIR).glob("*.md"),
                   key=lambda f: (0, int(f.stem), "") if f.stem.isdigit() else (1, 0, f.name))
    return [f for f in found if f.read_text(encoding="utf-8").strip()]


def _strip_heading(text):
    """(text without a leading '## <version>' heading line, whether one was stripped)."""
    m = VERSION_HEADING.match(text)
    return (text[m.end():], True) if m else (text, False)


def release(repo, version, notes, branch="main"):
    """Assemble the new CHANGELOG.md section, commit it on `branch`, tag vX.Y.Z. Returns the tag.

    One task = one changes/<task>.md fragment, so parallel branches never edit the same file. The section is
    --notes, then any legacy '## Unreleased' text (folded in once, for the transition), then the fragments in
    task order. The fragments are deleted in the same release commit."""
    parse_version(version)
    repo = Path(repo)
    tag = f"v{version}"
    if not notes or not notes.strip():
        raise AesirError("release needs --notes (agent-facing: what changed, what to do differently)")
    if _git(repo, "rev-parse", "--abbrev-ref", "HEAD") != branch:
        raise AesirError(f"release must run on {branch}")
    if _git(repo, "status", "--porcelain"):
        raise AesirError("working tree is not clean")
    if _git(repo, "tag", "--list", tag):
        raise AesirError(f"tag {tag} already exists")
    log = repo / "CHANGELOG.md"
    old = log.read_text(encoding="utf-8") if log.exists() else ""
    body = old[len(CHANGELOG_HEAD):].lstrip("\n") if old.startswith(CHANGELOG_HEAD) else old
    stripped = []
    notes, hit = _strip_heading(notes)
    if hit:
        stripped.append("--notes")
    parts = [notes.strip()] if notes.strip() else []
    legacy = UNRELEASED.search(body)
    if legacy:
        body = body[:legacy.start()] + body[legacy.end():]
        if legacy.group(1).strip():
            parts.append(legacy.group(1).strip())
    frags = _fragments(repo)
    for f in frags:
        text, hit = _strip_heading(f.read_text(encoding="utf-8"))
        if hit:
            stripped.append(f"{FRAGMENTS_DIR}/{f.name}")
        if text.strip():
            parts.append(text.strip())
    if stripped:
        print(f"note: dropped a leading '## <version>' heading from {', '.join(stripped)} (release writes the heading)")
    entry = f"## {version} ({_git(repo, 'log', '-1', '--format=%cs')})\n\n" + "\n".join(parts) + "\n\n"
    log.write_text(CHANGELOG_HEAD + "\n" + entry + body.lstrip("\n"), encoding="utf-8", newline="\n")
    _git(repo, "add", "CHANGELOG.md")
    for f in frags:
        _git(repo, "rm", "-q", "--", str(f))
    _git(repo, "commit", "-m", f"Release {version}")
    _git(repo, "tag", "-a", tag, "-m", f"Aesir {version}")
    return tag


# ---- install / upgrade (in a client repo) ----

def _extract(source, tag, dest):
    """Unpack the package, agent definitions and CHANGELOG at `tag` into dest."""
    if not _git(source, "tag", "--list", tag):
        raise AesirError(f"tag {tag} not found in {source}")
    paths = ["aesir", ".claude/agents"]
    for opt in ("CHANGELOG.md", "docs", "toolkits"):
        if _git(source, "ls-tree", "--name-only", tag, opt):
            paths.append(opt)
    data = _git(source, "archive", "--format=tar", tag, *paths, binary=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as t:
        t.extractall(dest, filter="data")


def write_pkg_readme(client, tree):
    """.aesir-pkg/README.md from the target tree's docs/pkg-readme.md (the constant only when the tree lacks it). Also
    called from finish_setup: an upgrade vendors with the OLD release's code, which writes its own constant."""
    f = Path(tree) / "docs" / PKG_README_FILE
    text = f.read_text(encoding="utf-8") if f.exists() else PKG_README
    pkg = Path(client) / PKG_DIR
    if (pkg / "docs").exists():
        (pkg / "README.md").write_text(text, encoding="utf-8", newline="\n")


def _write_launcher(pkg):
    """The one writer of .aesir-pkg/aesir_run.py (#257): _vendor and finish_setup both call it, so `upgrade --repair`
    replaces an old launcher too."""
    (Path(pkg) / LAUNCHER).write_text(LAUNCHER_SRC, encoding="utf-8", newline="\n")


def _vendor(client, src_tree, version):
    pkg = client / PKG_DIR
    pkg.mkdir(exist_ok=True)
    if (pkg / "aesir").exists():
        shutil.rmtree(pkg / "aesir")
    shutil.copytree(src_tree / "aesir", pkg / "aesir", ignore=shutil.ignore_patterns("__pycache__"))
    _write_launcher(pkg)
    (pkg / "VERSION").write_text(version + "\n", encoding="utf-8", newline="\n")
    if (src_tree / "CHANGELOG.md").exists():
        shutil.copyfile(src_tree / "CHANGELOG.md", pkg / "CHANGELOG.md")
    if (pkg / "toolkits").exists():
        shutil.rmtree(pkg / "toolkits")
    if (src_tree / "toolkits").exists():  # shipped, never built here: `audit run` builds one only when enabled
        shutil.copytree(src_tree / "toolkits", pkg / "toolkits", ignore=shutil.ignore_patterns("target", "__pycache__"))
    if (pkg / "docs").exists():
        shutil.rmtree(pkg / "docs")
    if (src_tree / "docs").exists():
        shutil.copytree(src_tree / "docs", pkg / "docs")
        write_pkg_readme(client, src_tree)


# Aesir's own agent defs run the main checkout's code from any cwd (-P keeps cwd off sys.path, PYTHONPATH names the
# checkout), never `cd X && ...`: a compound command is what the hook guards judge, and agents copy the example (#247,
# aesir-reports#161). Installs rewrite it to the client's vendored launcher, which is cwd-independent.
AESIR_MAIN_RUN = "PYTHONPATH=/e/Projects/Aesir python -P -m aesir"
AESIR_TEST_CMD = "`python -m aesir.paratest`"  # Aesir's own test-suite line in the defs; installs take the client's


AESIR_ONLY_FRONTMATTER = ("effort",)  # agent-def frontmatter keys kept out of client installs (D161)


def _strip_frontmatter_keys(text, keys=AESIR_ONLY_FRONTMATTER):
    """Drop the `key:` lines of `keys` from the leading frontmatter block only; the body is untouched."""
    head, sep, body = text.partition("\n---\n") if text.startswith("---\n") else ("", "", text)
    if not sep:
        return text
    # indented lines are continuations/nested values of another key, never top-level keys, so they stay
    kept = [ln for ln in head.split("\n") if ln.partition(":")[0].strip() not in keys or ln[:1].isspace()
            or ":" not in ln]
    return "\n".join(kept) + sep + body


def project_test_cmd(client):
    """The client's test-suite command from its aesir.toml [gates] ('quick', else 'full', else the first profile): the
    first step that is a plain string or a {cmd} table. '' when there is no config or no such step (#247)."""
    try:
        gates = tomllib.loads((Path(client) / "aesir.toml").read_text(encoding="utf-8")).get("gates", {})
    except (OSError, tomllib.TOMLDecodeError):
        return ""
    if not isinstance(gates, dict):
        return ""

    def first(name, seen):  # a {profile = "x"} entry expands in place, as gates.profile_steps does; never raises
        steps = gates.get(name)
        if name in seen or not isinstance(steps, list):
            return ""
        for e in steps:
            if isinstance(e, dict) and isinstance(e.get("profile"), str):
                cmd = first(e["profile"], seen | {name})
            else:
                cmd = e if isinstance(e, str) else e.get("cmd") if isinstance(e, dict) else None
            if isinstance(cmd, str) and cmd.strip():
                return cmd.strip()
        return ""

    for name in [n for n in ("quick", "full") if n in gates] + list(gates):
        cmd = first(name, frozenset())
        if cmd:
            return cmd
    return ""


def render_agent(text, client):
    """Agent def for a client: its CLI invocation, its own launcher and its own test command instead of Aesir's."""
    client = Path(client).resolve()
    text = _strip_frontmatter_keys(text)
    cmd = project_test_cmd(client)
    test = f"`{cmd}`" if cmd else "the test command in aesir.toml [gates]"
    path = (client / PKG_DIR / LAUNCHER).as_posix()
    launcher = f'python "{path}"' if " " in path else f"python {path}"  # a path with a space must stay one argument
    # the test line first: DEV_CMD is a prefix of Aesir's own `python -m aesir.paratest`
    return text.replace(AESIR_TEST_CMD, test).replace(AESIR_MAIN_RUN, launcher).replace(DEV_CMD, CLIENT_CMD)


LOCAL_SUFFIX = ".local.md"
GENERATED_NOTE = ("<!-- GENERATED by `aesir install`/`upgrade`; edits here are overwritten. Put project-specific "
                  "additions in .claude/agents/{stem}.local.md (never overwritten; appended below). -->")


def compose_agent(text, stem, local=None):
    """Rendered def with a generated-file note after the frontmatter and, if given, the client's additions appended."""
    note = GENERATED_NOTE.format(stem=stem)
    head, sep, body = text.partition("\n---\n") if text.startswith("---\n") else ("", "", text)
    out = f"{head}{sep}\n{note}" + f"\n{body}" if sep else f"{note}\n" + f"\n{body}"
    if local and local.strip():
        out = out.rstrip("\n") + "\n\n## Project additions\n\n" + local.strip("\n") + "\n"
    return out


def _agents(client, src_tree):
    dest = client / ".claude" / "agents"
    dest.mkdir(parents=True, exist_ok=True)
    names = []
    for f in sorted((src_tree / ".claude" / "agents").glob("*.md")):
        if f.name.endswith(LOCAL_SUFFIX):
            continue
        local_f = dest / (f.stem + LOCAL_SUFFIX)
        local = local_f.read_text(encoding="utf-8") if local_f.exists() else None
        text = compose_agent(render_agent(f.read_text(encoding="utf-8"), client), f.stem, local)
        (dest / f.name).write_text(text, encoding="utf-8", newline="\n")
        names.append(f.name)
    return names


PERSISTENT_ROLES = ("lead", "advisor")  # long-lived sessions; subagents pick up new defs at their next spawn


def restart_notice(client):
    """What `upgrade` prints after re-rendering agent defs and hook settings (#125): Claude Code reads both at session
    start, so the running lead and advisor sessions keep the old ones until restarted."""
    from .rolenames import session_prefix
    pre = session_prefix(client)
    names = " and ".join(f"{pre}-{r}" for r in PERSISTENT_ROLES)
    return (f"agent defs (test command re-read from aesir.toml [gates]) and hook settings were regenerated: running "
            f"persistent sessions ({names}) keep the old ones until restarted. Write a handoff "
            "(`role note <role> \"<handoff>\"`), restart each at a safe point, then `resume`. New subagents use the new defs at once.")


def defs_written(client):
    """UTC 'YYYY-MM-DDTHH:MM:SSZ' of the newest file in <client>/.claude/agents (when the defs were last written); '' if none."""
    from datetime import datetime, timezone
    try:
        ts = max(f.stat().st_mtime for f in (Path(client) / ".claude" / "agents").glob("*.md"))
    except (OSError, ValueError):
        return ""
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_ours(entry):
    return any(LAUNCHER in (h.get("command") or "") for h in entry.get("hooks", []) if isinstance(h, dict))


def merge_settings(path):
    """Add the Aesir hooks to .claude/settings.json, replacing only our own earlier entries; all else is kept."""
    path = Path(path)
    cfg = {}
    if path.exists():
        try:
            cfg = json.loads(path.read_text(encoding="utf-8") or "{}")
        except ValueError as e:
            raise AesirError(f"{path} is not valid JSON ({e}); fix it first, nothing was changed")
        if not isinstance(cfg, dict):
            raise AesirError(f"{path} is not a JSON object")
    hooks = cfg.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise AesirError(f"{path}: 'hooks' is not an object")
    for ev, matcher in HOOK_EVENTS:
        entries = [e for e in hooks.get(ev, []) if not (isinstance(e, dict) and _is_ours(e))]
        mine = {"hooks": [{"type": "command", "command": HOOK_CMD}]}
        if matcher:
            mine = {"matcher": matcher, **mine}
        hooks[ev] = entries + [mine]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8", newline="\n")


ATTRIBUTES = ("aesir-state/*.jsonl text eol=lf", f"{PKG_DIR}/aesir/githooks/* text eol=lf", f"{PKG_DIR}/** text eol=lf")
IGNORES = (".aesir/", "__pycache__/", "*.pyc")


def _ensure_lines(path, wanted):
    """Append the wanted lines missing from a text file (created if absent); existing content is kept."""
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    have = {ln.strip() for ln in old.splitlines()}
    missing = [ln for ln in wanted if ln not in have]
    if missing:
        sep = "" if not old or old.endswith("\n") else "\n"
        path.write_text(old + sep + "\n".join(missing) + "\n", encoding="utf-8", newline="\n")
    return missing


def _git_files(client):
    """.gitattributes pins LF for the sh git hook and state (no CRLF checkout); .gitignore hides the local db."""
    _ensure_lines(client / ".gitattributes", ATTRIBUTES)
    _ensure_lines(client / ".gitignore", IGNORES)


def _run_client(client, *args):
    """Run the vendored package in the client with a clean environment (never touches another checkout's db)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("AESIR_") and k != "PYTHONPATH"}
    r = proc.run([sys.executable, str(client / PKG_DIR / LAUNCHER), *args], cwd=str(client),
                       capture_output=True, text=True, env=env)
    if r.returncode:
        raise AesirError(f"client command {' '.join(args)} failed: {(r.stderr or r.stdout).strip()}")
    return r.stdout.strip()


def installed_version(client):
    f = Path(client) / PKG_DIR / "VERSION"
    return f.read_text(encoding="utf-8").strip() if f.exists() else None


def seed_cores(client, source):
    """Add the source checkout's owner+role scoped core rows the client lacks (add-only, read-only on the source db).
    Never fails an install: a source without a usable db, or a pinned version without `core sync`, is a note."""
    source = Path(source or default_source())
    try:
        msg = _run_client(client, "core", "sync", "--from", str(source), "--apply", "--add-only", "--as", "lead")
        print(msg.splitlines()[-1] if msg else "core seeded")
    except AesirError as e:
        print(f"note: core not seeded from {source}: {str(e).splitlines()[-1]}")


# Recommended practices (D69, #082): optional, never rules. Adopted = core key in lead's core, or [practices] flag in
# aesir.toml. The default-on ones apply to every project (source of truth: an owner-scope core row), so they are
# listed as default-on and never as unadopted. Text per practice: docs/practices.md.
_ACC = ("realistic-acceptance", "realistic_acceptance",
        "accept on realistic owner data; reproduce the owner signature before a fix", "acceptance,tests,bugfix,evidence")
_BASIC = (  # (key, one-line summary, tags): the [practices] flag is the key with underscores
    ("bound-untrusted-input", "bound untrusted input before it sizes memory, loops or recursion; reject unknown versions",
     "security,parser"),
    ("no-crash-on-untrusted-data", "no unchecked index, slice or unwrap on outside data", "security,parser"),
    ("checked-arithmetic", "checked arithmetic and lossless conversions at input boundaries", "security,parser"),
    ("surfaced-errors", "errors are surfaced, never silently dropped", "errors"),
    ("atomic-writes", "write user data atomically (temp file, then rename)", "io,data"),
    ("hostile-input-tests", "every parser of untrusted input ships a hostile-input table test", "tests,parser"),
    ("gate-builds-what-ships", "the gate builds and tests everything that ships; parked failing tests still compile",
     "tests,gate"),
    ("single-source-of-truth", "one home per list or constant; exhaustive matches", "design"),
    ("deterministic-tests", "tests are deterministic and isolated (no wall-clock asserts, unique temp dirs)", "tests"),
    ("small-units", "small functions, no copy-paste, named constants, no dead code", "design,review"),
    ("outside-data-boundaries", "outside data never picks a file path; edit structured formats through their parser",
     "security,design"),
    ("cli-contract", "one CLI contract per project (flags, --help, exit codes 0/1/2)", "cli"),
    ("networked", "network receivers decode before state change, cap growth, deadline every wait, count drops",
     "security,network"),
    ("realtime", "hot loops (per frame, tick or step) do no file IO or uncached heavy work", "performance"),
    ("shared-log-privacy", "user-sendable or committed output is non-identifying; reviewers grep it", "privacy,review"),
    ("declared-compat-paths",
     "paths that define peer or file compatibility are declared; changes are batched and announced", "design,review"),
)
_DEFAULT = (("comment-standard", "every file has a header doc comment; comments explain why (owner core row)"),)


POINTER_TIP = ("Recommended engineering practices (optional, never rules) are listed by install and status; "
               "text and how to adopt: .aesir-pkg/docs/practices.md (Aesir: docs/practices.md).")


VISIBILITY_LINE = ("guidance: build visibility first, with the first core code: saved state, digest tools, an event log "
                   "(.aesir-pkg/docs/visibility.md); each project interprets it for its domain.")


def _practice(key, flag, line, tags=""):
    return {"key": key, "flag": flag, "line": f"{key} ({line})"}


# the one per-practice tip that existed before #082 (kept); the other practices are listed, not tipped
ACCEPTANCE_TIP = ("Recommended practice, not a rule: accept on realistic owner data size/shape and reproduce the owner "
                  "signature before accepting a fix. See .aesir-pkg/docs/practices.md (Aesir: docs/practices.md).")
PRACTICES = (_practice(*_ACC),) + tuple(_practice(k, k.replace("-", "_"), line, tags) for k, line, tags in _BASIC)
DEFAULT_PRACTICES = tuple({"key": k, "line": f"{k} ({line})"} for k, line in _DEFAULT)


def practice_state(flags, core_keys):
    """{key: 'default' | 'adopted' | 'unadopted'} for every practice, from the [practices] flags and lead core row keys."""
    st = {d["key"]: "default" for d in DEFAULT_PRACTICES}
    for p in PRACTICES:
        st[p["key"]] = "adopted" if flags.get(p["flag"]) or p["key"] in core_keys else "unadopted"
    return st


# Reviewer checklist (#084): the "Reviewer asks" column of the engineering-practices table in docs/practices.md is the
# one source of each practice's check; it is read from that doc (vendored next to the package in clients). Only adopted
# practices are listed, so a project that adopted nothing gets no lines at all.
ACCEPTANCE_ASK = ("Does acceptance quote the sizes of realistic owner data, and does a bug fix reproduce the owner "
                  "signature (else stay partial)?")
_ROW = re.compile(r"^\| (?P<key>[a-z-]+) \(`(?P<flag>\w+)`\) \|(?P<cells>.*)\|\s*$")


def practice_asks(doc=None):
    """{practice key: reviewer question} from the practices doc table, plus the realistic-acceptance question."""
    doc = Path(doc) if doc else Path(__file__).resolve().parent.parent / "docs" / "practices.md"
    asks = {"realistic-acceptance": ACCEPTANCE_ASK}
    try:
        text = doc.read_text(encoding="utf-8")
    except OSError:
        return asks
    for ln in text.splitlines():
        m = _ROW.match(ln)
        cells = [c.strip() for c in m["cells"].split(" | ")] if m else []
        if len(cells) >= 4:  # rule, prevents, asks, check
            asks[m["key"]] = cells[2]
    return asks


def review_lines(flags, core_keys, task_id, worktree=None, audit_on=False, doc=None):
    """Context lines for a reviewer (#084): each adopted practice's question, and the branch audit command when an audit
    toolkit is enabled. [] when nothing is adopted and no toolkit is on (zero cost for such a project)."""
    asks = practice_asks(doc)
    adopted = [k for k, v in practice_state(flags, core_keys).items() if v == "adopted" and k in asks]
    lines = []
    if adopted:
        lines.append("Adopted practices: apply each question to the task diff; a failure is a review finding (should). "
                     f'A duplicate also gets `python -m aesir task log {task_id} "dup: <what duplicates what>"`.')
        lines += [f"- {k}: {asks[k]}" for k in adopted]
    if audit_on:
        lines.append(f"Audit toolkit enabled: run `python -m aesir audit run --dir {worktree or '<task worktree>'} "
                     f"--task {task_id}` (exit 1 = new findings, 2 = a toolkit failed) and list the new findings. A "
                     "severity high finding is a blocker unless the task log has a lead line `audit-override: <reason>`.")
    return lines


def practices_missing(client):
    """Recommended practices this client has not adopted (core row key or aesir.toml [practices] flag)."""
    client = Path(client)
    try:
        import tomllib
        flags = tomllib.loads((client / "aesir.toml").read_text(encoding="utf-8")).get("practices", {})
    except (OSError, ValueError):
        flags = {}
    try:
        core = _run_client(client, "core", "show", "lead")
    except AesirError:
        core = ""
    return [p for p in PRACTICES if not flags.get(p["flag"]) and f"{p['key']}:" not in core]


def seed_practices(client):
    """Add the two practice tips (idempotent) and print the not-yet-adopted line. Never fails an install: a pinned
    version without these commands is a note."""
    try:
        _run_client(client, "tip", "add", ACCEPTANCE_TIP, "--topic", "realistic-acceptance", "--tags",
                    "implementer,reviewer,acceptance,tests,bugfix,evidence", "--as", "lead")
        _run_client(client, "tip", "add", POINTER_TIP, "--topic", "practices", "--tags", "implementer,reviewer,lead",
                    "--as", "lead")
        todo = practices_missing(client)
    except AesirError as e:
        print(f"note: practices not seeded: {str(e).splitlines()[-1]}")
        return
    if todo:
        print("recommended practices not yet adopted (optional; see .aesir-pkg/docs/practices.md): "
              + "; ".join(p["line"] for p in todo))
    print("default-on practices: " + "; ".join(p["line"] for p in DEFAULT_PRACTICES))
    print(VISIBILITY_LINE)


def _vendored_has_finish(client):
    f = client / PKG_DIR / "aesir" / "install.py"
    return f.exists() and "def finish_setup" in f.read_text(encoding="utf-8", errors="replace")


def _resolve_source(client, source):
    """The Aesir checkout to extract from: --from, else the client's recorded `[aesir] source`, else this process's own
    checkout. Recorded wins over own so that inside a client an unrelated git checkout (the vendored .aesir-pkg
    sits in the client's repo) cannot silently override the source the client was installed from."""
    from . import releases  # late import: releases imports this module
    if source:
        cands = [Path(source)]
    else:
        rec = releases.source_of(client)
        cands = ([Path(rec)] if rec else []) + [default_source()]
    for c in cands:
        if (c / ".git").exists():
            return c
    raise AesirError(f"no git checkout of Aesir to extract from ({', '.join(map(str, cands))}): pass --from <path> "
                     f"or set [aesir] source in {Path(client) / 'aesir.toml'} to a local checkout")


def _setup(client, source, version, vendor=True):
    """Vendor the package at `version` (unless repairing), then hand the rest of setup to the VENDORED code (the new
    version's own steps), so a step added by `version` runs on the upgrade that introduces it. A vendored copy that
    predates `install-setup` (downgrade to an old tag) falls back to this process's finish_setup."""
    source = _resolve_source(client, source)
    with tempfile.TemporaryDirectory() as td:
        tree = Path(td)
        _extract(source, f"v{version}", tree)
        if vendor:
            _vendor(client, tree, version)
        if not _vendored_has_finish(client):
            return finish_setup(client, source, version, tree)
        out = _run_client(client, "install-setup", "--tree", str(tree), "--from", str(source), "--version", version)
    lines = out.splitlines()
    for line in lines[:-1]:
        print(line)
    return json.loads(lines[-1])


def finish_setup(client, source, version, tree):
    """Everything after vendoring: agent defs from `tree`, settings, git files, hooks, db, cores, practices, source."""
    client, source, tree = Path(client), Path(source), Path(tree)
    fresh = not (client / ".aesir" / "aesir.db").exists()  # a first install has no running sessions to restart
    write_pkg_readme(client, tree)
    _write_launcher(client / PKG_DIR)
    agents = _agents(client, tree)
    merge_settings(client / ".claude" / "settings.json")
    if not fresh:  # printed here, by the new vendored code, so it shows on the upgrade that introduces it (#125)
        print(restart_notice(client))
    _git_files(client)
    _run_client(client, "install-githooks")
    _run_client(client, "task", "list")  # connecting creates / migrates the db with the pinned code
    seed_cores(client, source)
    seed_practices(client)
    from . import releases  # late import: releases imports this module
    releases.record_source(client, source)  # lets status/resume/'releases' see newer tags later
    return agents


def install(client, version, source=None, project=None, session_prefix=None):
    client = Path(client).resolve()
    parse_version(version)
    if installed_version(client):
        raise AesirError(f"{client} already has Aesir {installed_version(client)}; use upgrade")
    client.mkdir(parents=True, exist_ok=True)
    if not (client / ".git").exists():
        _git(client, "init", "-q")
    toml = client / "aesir.toml"
    if not toml.exists():
        name = (project or client.name).replace('"', "")
        pre = (session_prefix or "").replace('"', "").strip()
        toml.write_text(f'[project]\nname = "{name}"\n' + (f'session_prefix = "{pre}"\n' if pre else ""),
                        encoding="utf-8", newline="\n")
    return {"client": str(client), "version": version, "agents": _setup(client, source, version)}


def upgrade(client, version, source=None, allow_major=False, allow_downgrade=False, repair=False):
    """Explicit pin change. Compatibility rule (charter s10): within a major version tools and arguments are only
    added, so same-major upgrades are safe; a major bump needs --allow-major and a read of CHANGELOG.md.
    `repair` re-runs setup with the pinned code at the pinned version (version None = the pinned one), no re-vendor."""
    client = Path(client).resolve()
    cur = installed_version(client)
    if not cur:
        raise AesirError(f"{client} has no Aesir install; use install")
    if repair:
        if version not in (None, cur):
            raise AesirError(f"--repair re-runs setup at the pinned version {cur}, not {version}")
        parse_version(cur)
        return {"client": str(client), "from": cur, "version": cur,
                "agents": _setup(client, source, cur, vendor=False)}
    new = parse_version(version)
    old = parse_version(cur)
    if new == old:
        raise AesirError(f"already at {cur}")
    if new < old and not allow_downgrade:
        raise AesirError(f"{version} is older than {cur}; pass --allow-downgrade (a newer db schema stays read-only)")
    if new[0] != old[0] and not allow_major:
        raise AesirError(f"{cur} -> {version} crosses a major version and may break compatibility; read "
                         f"{PKG_DIR}/CHANGELOG.md, then pass --allow-major")
    return {"client": str(client), "from": cur, "version": version, "agents": _setup(client, source, version)}


def edit_denied(file_path):
    """True when file_path is inside a vendored .aesir-pkg folder (the client may not edit its Aesir copy)."""
    return PKG_DIR in re.split(r"[\\/]+", str(file_path))[:-1]
