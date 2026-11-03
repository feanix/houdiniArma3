# Audit findings and toolkits

Audience: agents auditing a project, and authors of audit toolkits. Findings live in the Aesir database as rows;
you read them through bounded, ranked, paged digests instead of reading every file. Core is stdlib only; language
toolkits (Rust, Python, ...) are separate programs, opt-in per project, that print findings in the format below.

## Findings format (version 1)

JSON lines (one object per line, UTF-8, blank lines ignored). Fields:

| field | required | meaning |
|---|---|---|
| `v` | no (default 1) | format version; anything other than 1 is rejected |
| `file` | yes | path relative to the project root; `\` is read as `/` |
| `line` | no (default 0) | 1-based line, 0 = whole file |
| `severity` | yes | `info`, `low`, `medium` or `high` |
| `rule` | yes | stable rule id, e.g. `clippy::unwrap_used`, `missing_docs` |
| `message` | no | one line of text |
| `source` | no | toolkit name; defaults to the toolkit being run or `--source` of the import |

Example: `{"v":1,"file":"src/main.rs","line":42,"severity":"medium","rule":"missing_docs","message":"pub fn run has no doc comment"}`

An import is all or nothing: one bad line rejects the file and names the line.

Identity: a finding is the same across audits when source, rule, file and message (digits ignored, so moved
line numbers and counts do not matter) match. Re-importing updates it (`last_audit`, line); a finding marked `fixed`
that is reported again reopens. Identical findings within one import are kept apart by order.

## Toolkit contract

A toolkit is a command that prints findings (above) to stdout and exits 0. Enable it in `aesir.toml`:

```toml
[audit]
toolkits = ["rust"]       # empty or absent: `audit run` does nothing and costs nothing
timeout = 600             # seconds per toolkit (optional)
exclude = ["*.lock"]      # globs dropped from `audit plan` and toolkit findings (optional); .aesir-pkg and .aesir always are

[audit.rust]
cmd = "cargo run -q --manifest-path tools/audit-rust/Cargo.toml"   # run in the project root, via the shell
```

`audit run` runs each enabled toolkit in order and imports its output into one audit (kind `run`). A toolkit that
fails (nonzero exit, timeout, missing `cmd`, bad output) imports nothing and is reported; the others still run and
`audit run` then exits 1. stderr is for humans and is shown only on failure.

## Rust toolkit (opt-in)

Source ships in `toolkits/rust/` (vendored to `.aesir-pkg/toolkits/rust/`); nothing builds it until a project lists
`"rust"` in `[audit] toolkits` and has cargo on PATH. With no `[audit.rust] cmd`, `audit run` builds it with the
project's toolchain (cached in `.aesir/toolkits/rust`) and runs it in the project root. Optional keys under `[audit.rust]`:

- `hostile`: list of path/module globs naming the code that reads untrusted input (replay/network/file parsers, log
  readers). `**` crosses folders, `*`/`?` stay in one name; a plain name also matches a folder or module (`net`, `src::replay`).
  The project declares this; the toolkit knows no project's file names. Without it, path name tokens apply as a rough
  default (`pars lex net proto codec decod deserial wire socket packet input replay json share log session`); `scope`
  (comma list of tokens) overrides that default.
- `base` (branch for the diff check, default `main`), `max_file_lines` (default 1000).
- `[practices] networked = true` / `realtime = true` switch on the conditional rule groups below.

It emits, worst first. Findings are per occurrence: the message names the enclosing function, function-level lints
carry the line range, and the identity ignores digits, so a finding keeps its identity when lines move.

- clippy preset: `unwrap_used`/`expect_used` outside tests, and in the hostile scope only `indexing_slicing`,
  `arithmetic_side_effects`, `string_slice`, `cast_possible_truncation` (unwrap, expect, indexing and string slicing
  there are `high`: a crash on outside data); `let_underscore_must_use` (not for `write!`/`writeln!` into a `String`),
  `map_err_ignore`, `unused_result_ok`, `undocumented_unsafe_blocks` (unsafe needs a SAFETY comment), `too_many_lines`,
  `cognitive_complexity`, `missing_docs` (one per file). Compile errors are `high`.
- text rules: `file_size`; `test_hygiene::wall_clock` (asserts on elapsed time) and `test_hygiene::shared_temp_name`
  (fixed temp path); `hostile::unbounded_alloc` (capacity/resize from a non-constant), `hostile::unbounded_read`,
  `hostile::no_hostile_test`; `io::non_atomic_write` (file written in place, no rename in the file);
  `gate::feature_not_built` (a Cargo feature that `aesir.toml`/CI never names and no `--all-features`).
- state rules (#124, from a blind review that found what the rules above missed):
  `debug_only_invariant` (medium: `if cfg!(debug_assertions)`, `#[cfg(debug_assertions)]` or `debug_assert!` around a
  `check`/`verify`/`validate` call, so release and soak runs have no such check; `checked_*` arithmetic and tests are
  not checks); `test_hygiene::no_assertion` (low: a `#[test]` with no assert-family macro, `panic!`, `should_panic`,
  unwrap/expect, `?` in a Result-returning test, or call to a helper that asserts or panics; a helper that only unwraps
  does not count); `unbounded_growth` (low: a `HashMap`/`BTreeMap`/`Vec` field that gets inserts or pushes outside
  constructors but is never removed from, retained, cleared, drained, reassigned, handed on as `&mut` or length-capped
  in its crate; a Vec only counts when a `&mut self` method pushes to `self.<field>`; matched by field name, so a
  same-named field elsewhere can hide or cause a hit); `capacity_nesting` (low, low confidence: a per-owner limit
  `MAX_<OWNER>_<THING>` times the owner limit `MAX_<OWNER>S` that exceeds the global `MAX_<THING>`, or two `MAX_*`
  from different files multiplied or compared in one runtime expression; fix with a `const _: () = assert!(..)`);
  `gate::release_not_tested` (low: release is what ships, by a `--release` gate or CI step, a `[profile.release]`
  or a binary, but no gate command runs non-ignored tests with `--release`; `-- --ignored` alone does not count).
  Limits: `unbounded_growth` cannot see a map that is removed from only when an entry dies yet filled for entries that
  live on (VT's `since_branch`), and `no_assertion` cannot see a test that asserts elsewhere but not on one statement.
- `networked` only: `networked::no_deadline` (blocking receive in a file with no timeout). `realtime` only:
  `realtime::io_in_hot_path` (file or console IO in update/tick/frame/step/render/draw functions).
- `doc_coverage`: per file, pub items without docs, most missing first.
- `diff::comment_only`: files changed against the base branch whose syntax tree is identical apart from comments,
  docs and layout. Conservative: unparsable, added or deleted files count as code.

These are line heuristics and lints; duplication and logic errors are out of their reach (review or an LLM pass).

Bare runs: both shipped toolkits (Rust exe, Python script) refuse to start without the `--via-audit-run` flag that
`audit run` adds, because only `audit run` resolves `aesir.toml` ([audit] exclude and base, [audit.rust] hostile and
max_file_lines, [practices] networked and realtime); a bare run would silently audit less. The error names the
`aesir.toml` it found (or says none was found). `--no-config` is the explicit opt-out for a debugging run with defaults.
Bare runs are for debugging only; task evidence uses `audit run`.

## Python toolkit (opt-in)

Source ships in `toolkits/python/audit_python.py` (vendored to `.aesir-pkg/toolkits/python/`); stdlib only, nothing to
build. List `"python"` in `[audit] toolkits` with no `[audit.python] cmd` and `audit run` runs it in the project root
with the same interpreter as aesir. Optional key: `base` (branch for the diff check, default `main`). It emits, worst first:

- ruff findings with the lint preset, one finding per rule and file, when ruff is installed (`pip install ruff`); else one
  `info` finding `ruff::not_installed` and the rest still runs. Preset: `E722` bare except, `BLE001` blind
  `except Exception`, `S110`/`S112` swallowed errors, `S101` assert (not in test files), `C901` complexity over 15,
  `PLR0912` over 15 branches, `PLR0915` over 60 statements. Ruff has no unchecked-indexing rule, so that is not covered.
- `doc_coverage`: per file, modules, public functions/classes and private ones of 30+ lines with neither a docstring nor
  a comment directly above, most missing first.
- `diff::comment_only`: modified files whose ast is identical to the base apart from comments, docstrings and layout.
  Conservative: unparsable, added or deleted files count as code.

## Commands

Digest first, rows second; every list is ranked (worst severity first), capped and paged (`--limit`, `--page`).

- `audit run` -- run enabled toolkits and import.
- `audit run --dir <worktree> [--task N]` -- audit an unmerged branch before merge. Runs the toolkits enabled in main's config (`[audit]`, `[practices]`; the branch's own aesir.toml cannot weaken them) inside the worktree and compares with main's findings (any status but `fixed`, so a fixed one that returns counts). Prints only the new findings, logs them on task N, imports nothing (main's findings stay unchanged). Exit 0 clean, 1 new findings, 2 a toolkit failed. See Evidencing an audit criterion before merge.
- `finding digest [--by area|severity|file|rule|source] [--depth N] [filters]` -- counts per group, worst first.
- `finding list [filters]` -- rows. Filters: `--file`, `--area <dir>`, `--severity <min>`, `--rule`, `--source`, `--status`, `--since <audit>` (first seen after that audit: the re-audit delta).
- `finding baseline --rule R [filters]` -- mark every open finding of a newly adopted rule as expected (status `baselined`: expected for now, to be fixed; `wontfix` means never fix) so only new hits stand out; count: `finding digest --status baselined --by rule`. See docs/practices.md, Baseline rollout.
- `finding add <file> <line> --severity S --rule R [--message ..]`, `finding import <file|->`, `finding set <id> [--status open|fixed|wontfix|false_positive|baselined] [--task N] [--note TEXT]` (link the fix task; `--note` records the triage reason, e.g. why a finding is `wontfix`, shown by `finding list`).
- `finding rank`, `finding pass <file> [--done]` -- file ranking and review passes; see File ranking and review passes.
- `audit benchmark <name> [--source DIR] [--out FILE]` -- score the Rust toolkit against a recorded answer key in `tests/benchmarks/<name>/` (clones the keyed repo tag to a temp dir; manual, needs network and cargo, skips with a reason without them; not in the gate). Prints recall by severity/category/area, the High findings one by one and the false-alarm share. First key: `tetris-054` (89 findings, see its README). Reads no database.
- `audit list`, `audit show <id>` -- audits and how many findings each first saw.
- `audit plan [--agents N] [--exclude GLOB] [--note ..]` -- shard tracked text files into N balanced chunks (by line count) recorded as an audit; shards never overlap. `audit shard <id> <n>` prints one reviewer's file list and checklist; `audit done <id> <n>` marks it finished. Give each parallel reviewer one shard.

## Triage buckets (D138, #130)

Every open finding is triaged by value into one bucket, recorded on the finding:

- fix-now (critical, or easy and valuable; a doc gap likely to cause bugs or long investigation): linked to a task (`finding set <id> --task N`) and fixed in one bounded chunk.
- batch (low-value categories such as doc_coverage and complexity): status `baselined`. They stay baselined until their file's review pass; which files get a pass is the file ranking of D140 (next section).
- no-value: status `wontfix` plus a reason (`finding set <id> --status wontfix --note "<reason>"`).

## File ranking and review passes (D139, D140, #135)

Findings are smell signals, not verdicts. Which files get a judgement pass is decided by ranking, not by a finding-count threshold.

- Score per tracked file = smell + churn. Smell: weighted severity of the file's `open` and `baselined` findings (all rules). Churn: lines added plus deleted since the file's last review pass (`git diff --numstat` from the pass commit to HEAD); a never-reviewed file counts its current lines, but at most `churn_cap_lines` of them (default 100, about one modest edit), so smell leads and churn only breaks ties (D147); reviewed files keep their real churn since the pass. Churn counts `churn` weight per line.
- Weights, in `aesir.toml`; every key optional, defaults shown:

```toml
[audit.rank]
high = 10        # per finding
medium = 4
low = 1
info = 0
churn = 0.05     # per changed line
top_n = 3        # files named for this release's passes
churn_cap_lines = 100  # most churn lines a never-reviewed file counts
tests = false    # true: test files (tests/, test_*, *_test.*) rank too
```

- Candidates (#142): only tracked files an enabled `[audit] toolkits` entry covers, by extension (rust `.rs`; python `.py`, `.pyi`; a custom toolkit lists its own with `[audit.<name>] extensions = [".go"]`), minus `[audit] exclude`. Data and generated files (`aesir-state/`, `*.jsonl`) never rank. Test files are left out unless `tests = true`.

- `finding rank [--limit N --page P]` lists files top first with the breakdown (smell part with finding count, churn part with line count) and marks the top N as this release's passes. Files with score 0 and files with an open pass task are left out.
- `status` shows one line, the top N files with their scores and the command to run.
- `finding pass <file> [--requester R]` creates one task `Review pass: <file>` (requester advisor by default, scope the file) and links every open or baselined finding of the file to it, except findings already linked to a task that is not closed (merged, verified, wontfix): those keep their link and the spec marks them "(linked to #N)". Its criteria encode D139: start from the listed findings and judge each fix or wontfix with a reason; also fix same-kind issues the audit cannot see (for docs: missing, wrong or stale comments); record other kinds of issue, including real bugs, as findings or tasks and do not fix them in the pass. A second `finding pass` on a file with an open pass task creates nothing and names the existing task.
- Recording: when a pass task is set `merged`, or with `finding pass <file> --done`, the file's review is recorded (HEAD commit and date, in the `space` table, role `audit`, kind `review`). That resets its churn to 0; later edits count again.
- Release rule: take at most `top_n` passes per release. Starting more than `top_n` review-pass tasks since the latest `v*` tag needs `--reason TEXT`, which is logged on the new task.

An audit-only chunk ships only alongside non-audit work, at most one per release.

## Evidencing an audit criterion before merge

For a criterion like "no new audit findings", run the audit on the task's worktree from the main checkout, as evidence
(the exit status is the verdict):

`python -m aesir evidence run <task> --criterion <k> --dir <worktree> -- python -m aesir audit run --dir <worktree> --task <task>`

Zero new findings against main passes; any new finding fails and is listed in the output and the task log. After merge,
`audit run` imports findings as usual. The comparison is also a function, `audit.new_vs_main(conn, root, cwd)`, returning
`(new findings, errors, exit code)`, for a gate step.

## Audit as a gate step (opt-in, #116)

Add `{ audit = "new" }` to a gate profile in `aesir.toml` to fail the gate (and so the merge queue) when the gated tree has
findings that are not on main:

```toml
[gates]
full = ["python -m unittest", { audit = "new" }]
```

It runs `audit run --dir .` in the gated tree with main's `[audit]` config (a branch cannot switch it off), using the
code of the aesir running the gate. Known findings on main in any status but `fixed` (open, baselined, wontfix) do not
count; one the task fixes is not an error; a `fixed` one that comes back is new. Failure lists the new findings in the
gate tail, and the queue puts that tail in the task log (`changes_requested`). A project with no findings yet passes
with none. A toolkit that cannot build or run fails the step (exit 2, `FAILED <reason>`), never passes silently. The
step reports its duration. The Rust toolkit builds into `<main>/.aesir/toolkits/rust` shared by all worktrees, and cargo
rebuilds it only when its source changed. Off unless the profile has the entry. Recommended for zero-baseline projects.

Severity threshold (#133): `{ audit = "new", min_severity = "medium" }` fails the step only on new findings at or above
that severity (`info`, `low`, `medium`, `high`). Lower new findings are still listed in the gate output, marked
`(below threshold, not blocking)`, and the step exits 0. Omitted means `"any"`: every new finding fails (unchanged).
Aesir's own `quick` and `full` use `medium` (D138). The same flag exists on the command: `audit run --dir D --min-severity medium`.

Typical re-audit: `audit run`, then `finding digest --since <previous audit id>`, then `finding list --area <dir> --since <id>`.
