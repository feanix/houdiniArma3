# Recommended practices

Optional, except the comment standard (default-on, last section). Not Aesir rules and not charter rules: each project
decides whether to adopt one, as a local rule. Install and upgrade print the ones this project has not adopted yet.

## Realistic acceptance data

Source: aesir-reports#7 (a tetris project), owner decision D69.

### The practice

1. When owner or real data exists, acceptance includes a check at the realistic size and shape taken from that
   data. The reviewer quotes the sizes used (bytes, rows, rounds, entities), so a toy input is visible as one.
2. A bug fix first reproduces the owner-observed signature (the exact symptom, count or message the owner saw). If it
   cannot be reproduced, say "not reproduced" and keep the task partial; do not accept a fix for a symptom you never saw.

### Why

- Toy inputs passed review while real data existed: tests used 5-10 KB inputs, the owner's real rounds were 40-56 KB.
- Single-round tests hid cumulative statistics that only show across many rounds.
- A model that could not reproduce the owner's 981 stalls was still accepted as a fix.
- Cost: each of these was found late, by the owner, after the work was reported done, so the owner paid for the
  review and the rework came after merge.

### When it does not apply

- No real data exists yet (new project, first feature): use the smallest honest sample and say so.
- The behaviour does not depend on size or shape (pure formatting, renames, docs).
- The real data is private or cannot be copied: use a synthetic input matching its measured size and shape, and
  state that it is synthetic.
- The symptom is intermittent and cannot be forced: state "not reproduced", keep the task partial, and add the
  logging that would catch it.

### Adopt it (project-scope rule)

Run in the project (lead role):

    python -m aesir --as lead core set lead conventions "Acceptance uses realistic owner data size/shape (reviewers quote sizes); bug fixes reproduce the owner signature or stay partial (.aesir-pkg/docs/practices.md)" --key realistic-acceptance --reason "adopt recommended practice D69"

A new row is project scope, so it stays local to this project. Optionally also record the flag in `aesir.toml`
(either one marks the practice adopted; the install/upgrade line then stops listing it):

    [practices]
    realistic_acceptance = true

To drop it: `core rm lead conventions realistic-acceptance --reason ...` and remove the flag.

## Engineering practices (#082)

Source: aesir-reports#29 (an audit of a game project: most findings fell in a few clusters a written rule plus a lint
would have prevented). Each is a named, opt-in practice, adopted exactly like realistic acceptance: a lead core row with
the practice name as `--key`, or the `[practices]` flag (the name with `_`). Language-neutral; the Check column names
what the opt-in audit toolkits (`docs/audit-findings.md`) already report, by rule id. `R:` = Rust toolkit, `Py:` =
Python toolkit. "Reviewer only" means no automatic check exists.

| Practice (flag) | Rule | Prevents | Reviewer asks | Check |
|---|---|---|---|---|
| bound-untrusted-input (`bound_untrusted_input`) | Bound outside input before it sizes memory, loops or recursion; parsers reject unknown versions and values. | A hostile or corrupt file allocates gigabytes or recurses to a crash. | What caps this length, count or depth, and what happens past it? | R: `hostile::unbounded_alloc`, `hostile::unbounded_read` |
| no-crash-on-untrusted-data (`no_crash_on_untrusted_data`) | No unchecked index, slice or unwrap on outside data; return an error. | One bad input panics the process. | Can any byte of outside data reach this index, slice or unwrap? | R: `clippy::unwrap_used`, `expect_used`, `indexing_slicing`, `string_slice` (high severity in parser and net scope). Py: `S101` (assert); no indexing rule |
| checked-arithmetic (`checked_arithmetic`) | Use checked or saturating arithmetic and lossless conversions where input meets the code. | Silent overflow or truncation turns a size or id into a wrong one. | Which operations here take outside numbers, and do they overflow loudly? | R: `clippy::arithmetic_side_effects`, `cast_possible_truncation` (fix with `checked_`/`saturating_`, `try_from`) |
| surfaced-errors (`surfaced_errors`) | Errors are surfaced to the caller or the log, never silently dropped. Exempt: infallible write-to-String and best-effort cleanup (say so in a comment). | A failure is invisible until the data is already wrong. | Where does this error go? | R: `clippy::let_underscore_must_use`, `map_err_ignore`, `unused_result_ok`. Py: `E722`, `BLE001`, `S110`, `S112` |
| atomic-writes (`atomic_writes`) | Write user data to a temp file in the same folder, then rename over the target. | A crash mid-write leaves a truncated save. | Could a crash here leave a half-written file? | R: `io::non_atomic_write` |
| hostile-input-tests (`hostile_input_tests`) | Every parser of untrusted input ships a table test of hostile inputs: empty, truncated, maximum values, deep nesting, path-like ids. | Parsers pass happy-path tests and fail on the first odd file. | Which hostile rows does this parser's test have? | R: `hostile::no_hostile_test` (parser globs in `[audit.rust] hostile`) |
| gate-builds-what-ships (`gate_builds_what_ships`) | The gate builds and tests everything that ships: every feature, binary and script. Parked failing tests still compile (mark them ignored or expected-fail; never comment out or `cfg` them away). | Shipped code the gate never builds rots; a parked test that stops compiling is lost. | Is every feature, bin and script in the gate command? Do parked tests still compile? | R: `gate::feature_not_built`, `gate::release_not_tested`, `debug_only_invariant` |
| single-source-of-truth (`single_source_of_truth`) | One home per list, table or constant. Where all cases must be handled, use an exhaustive match, not a default arm. | Two copies drift; a new case is silently ignored. | Where else is this list written down? | Reviewer only |
| deterministic-tests (`deterministic_tests`) | Tests are deterministic and isolated: no wall-clock asserts, unique temp dirs, no shared global state. | Flaky tests train everyone to ignore red. | Could this test pass or fail depending on time, order or a neighbour? | R: `test_hygiene::wall_clock` |
| small-units (`small_units`) | Small functions and files, no copy-paste, named constants, no dead code. | Long or duplicated code hides the bug and doubles the fix. | Could this be split, or does it repeat another block? | R: `clippy::too_many_lines`, `cognitive_complexity`, `file_size`. Py: `C901`, `PLR0912`, `PLR0915` |
| outside-data-boundaries (`outside_data_boundaries`) | Outside data never picks a file path. Structured formats (JSON, TOML, XML) are edited through their parser, not by string search and replace. Modules exchange data, not formatted text. | Path traversal; a replace that corrupts the file; code that parses its own log lines. | Does any outside string become part of a path or get spliced into a structured file? | Reviewer only |
| cli-contract (`cli_contract`) | One CLI contract per project: consistent flags, working `--help`, exit codes 0 ok, 1 failure, 2 usage. | Scripts cannot tell a bad flag from a real failure. | Do all commands share flag names and exit codes? | Reviewer only |
| networked (`networked`) | For projects with network receivers: decode fully before any state change, cap growth, put a deadline on every wait, count drops. Adopt only if the project talks over a network. | One malformed or slow peer corrupts state or hangs the loop. | What is the deadline on this wait, and is the packet fully decoded first? | R: `networked::no_deadline` (the flag switches it on) |
| realtime (`realtime`) | For projects with a hot loop (per frame, tick or step): no file IO and no uncached heavy work inside it. Adopt only for simulations and games. | Frame drops and stalls from IO nobody noticed in the loop. | Is anything in this loop touching disk or recomputing a cacheable value? | R: `realtime::io_in_hot_path` (the flag switches it on) |
| shared-log-privacy (`shared_log_privacy`) | Output a user may send, or that is committed (logs, replays, reports), is non-identifying: no names, home paths with user names, tokens. Reviewers grep for it. | Private data leaks in a bug report. | Does the new output carry a name, a home path or a token? | Reviewer only (grep) |
| declared-compat-paths (`declared_compat_paths`) | Paths that define peer or file compatibility (wire format, save format, schema) are declared in the project; changes there are batched and announced. | A casual edit silently breaks old saves or peers. | Does this diff touch a declared compatibility path, and is it announced? | Reviewer only |

Rust `unsafe` blocks need a `SAFETY` comment (`clippy::undocumented_unsafe_blocks`), part of the Rust preset.

Adopt one (lead role; key and flag are in the table):

    python -m aesir --as lead core set lead conventions "<one-line rule> (.aesir-pkg/docs/practices.md)" --key atomic-writes --reason "adopt recommended practice"

Optionally also set `[practices] atomic_writes = true` in `aesir.toml`. Either marks it adopted: install and upgrade stop
listing it and `status` shows it as adopted. `networked` and `realtime` also switch on their Rust rule groups.

The presets are opt-in: enable a toolkit under `[audit]` (see `docs/audit-findings.md`). The Rust clippy preset is
`PRESET` in `toolkits/rust/src/clippy.rs` plus `toolkits/rust/src/rules.rs`; the Python preset is `RUFF_PRESET` in
`toolkits/python/audit_python.py`. Aesir applies the Python preset to itself.

### Baseline rollout

Adopting a check on an existing codebase reports every old hit. Do not fix them all first and do not leave the gate red:

1. Run the new check once (`audit run`), then record the existing hits as expected:
   `python -m aesir finding baseline --rule <rule>` (add `--area <dir>` to narrow). They get status `baselined` (expected
   for now, to be fixed; `wontfix` means never fix), so only new hits stay `open` and fail review. In Rust you
   may instead write `#[expect(lint, reason = "baseline")]` at the site; the compiler then complains once the hit is fixed, so the expect cannot outlive the problem.
2. The baseline count is visible: `python -m aesir finding digest --status baselined --by rule`; `status` prints
   `baselined findings: N`.
3. It shrinks as hits are fixed: fix one, then `python -m aesir finding set <id> --status fixed`. A baselined finding
   the toolkit stops reporting is not closed automatically; sweep `finding list --status baselined` now and then.
   After the rollout nothing new joins the baseline: a new hit is fixed.

### Gate on new findings

For a project with a zero baseline (no findings yet, or all baselined), add `{ audit = "new" }` to the `full` gate profile
so no task merges with a new finding. Opt-in; see "Audit as a gate step" in `docs/audit-findings.md`.

## Comment standard (default-on)

Not opt-in: it applies to every project and to Aesir's own code. The source of truth is the owner-scope lead core row
`comment-standard` (`python -m aesir core show lead`); read the clauses there, they are not restated here. Install and
upgrade list it as default-on, never as unadopted; `status` shows it under default-on.

- Prevents: a reader (human or agent) re-derives intent from code and reworks on a wrong guess.
- Reviewer asks: does each file open with a header comment, and does each non-obvious block say why?
- Automatic part: the toolkits report missing header doc comments as `doc_coverage` (R: also `missing_docs`; Py:
  modules, public and long definitions with no docstring or comment above). The reviewer judges quality; the preset
  only finds gaps.

## Parallel test runner (optional tooling)

Source: Aesir task #99. Not a rule to adopt; a faster way to run a Python `unittest` suite in a gate.

A full gate that runs the suite serially (and again with end-to-end tests on) grows with the suite. `aesir.paratest`
(stdlib only, ships with the vendored package) runs each TestCase class in its own process, in parallel:

    [gates]
    quick = ["python -m aesir.paratest"]
    full  = [{ cmd = "python -m aesir.paratest", env = { AESIR_E2E = "1" } }]   # E2E run covers the plain run

    [tests]
    workers = 8   # default: cpu count - 2

- Each worker gets its own temp dir (TEMP/TMP/TMPDIR), so tests that write temp files or lock files do not collide.
- A TestCase class that cannot share the machine sets `serial = True`; it runs alone after the parallel phase.
- Output is one summary: failing test names with tracebacks, then `Ran N tests`. Exit 1 on a failure, a crashed worker,
  or fewer tests run than discovered. A failure anywhere fails the gate and the stored tail names it.
- Tests must not depend on running in one process or in file order. Run it once against your suite before switching the
  gate, and compare its test and skip counts with `python -m unittest discover`.
