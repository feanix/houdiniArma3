# Gates

Profiles (`[gates]` in aesir.toml) are read from the MAIN checkout, so a branch cannot weaken its own gate.

## Bootstrap gate

The first task that adds `[gates]` (or a new profile) cannot be gated from main's config, since main does not have it yet. Rule:

- Main lacks the requested profile and the branch's aesir.toml has it: `gate run <profile> --task N --dir <worktree>` and the merge queue's full gate run the branch's profile.
- It is printed (`bootstrap gate: profile 'x' is not in main's aesir.toml, ran the branch's own`), starts the recorded gate_runs tail, and adds `(bootstrap gate: branch profile)` to the task log line.
- A profile main lacks (new or renamed) is taken from the branch, so the lead reviews the branch's profile text like code.
- Main has the profile: main's always wins, whatever the branch says.
- Neither has it: the usual "no gate profile" error.

The marker lives in the existing tail text and task log; there is no schema column for it.

## Tiers

- quick: about 2 min. The inner loop; every task, every change.
- full: about 5 min. What a merge needs (quick plus the e2e run and the roundtrip). Keep it near 5 min: it holds a merge slot.
- long: separate, up to about 30 min. Milestone acceptance and periodic or nightly checks (soak, many-sim-day runs). Never required for a merge, never holds a merge slot.

## Long-run lane

A long profile is an ordinary `[gates]` profile that `[long]` marks with its time limit in seconds (`true` = 1800):

```toml
[gates]
soak = ["python -m sim --days 30"]
[long]
soak = 2400
```

- `gate run soak --task N --criterion K` starts it detached and returns at once (`--wait` stays in the foreground: use it only under a Claude Code Monitor, never to block a session; the rule-11 path is the detached run plus `python -m aesir wait --task N` under a Monitor, see docs/supervision.md). Existing `[gates]` syntax and profiles are unchanged.
- A detached supervisor (`python -m aesir.longrun`) holds a per-project+profile long-run lock (`longrun-<hash>.lock` in the gate dir; one run per profile per project), takes no merge slot and not the legacy lock, and leases workers in the long lane (min 1, never waits, at most half the budget; `GATE_LONG_WORKERS` tells steps the grant). Merge gates in any project proceed meanwhile.
- The limit covers all steps together; on expiry the process tree is killed and the status is `timed_out`. Other statuses: `running`, `passed`, `failed`. Output is written when the run ends, to `<db dir>/longruns/L<id>.log`.
- With `--task` and `--criterion`, the end of the run records executed evidence for that criterion at the run's commit sha (exit 0 only when passed), so in_review checks can cite it. The tree must be clean.
- A killed supervisor frees its lock (OS lock on its handle) and its steps die with it on Windows (job object), and on POSIX when it exits or gets SIGTERM/SIGHUP (each step runs in its own process group, killed on timeout or interruption; a SIGKILL of the supervisor cannot be caught and orphans the running step); the row is marked `failed` on the next `status` or `gate long`.
- On Windows the supervisor tries to break away from the calling tool's job object; if the job forbids it, `gate run` prints a warning (also in `L<id>.supervisor.log` and the event log) and the run may die with the tool. Workaround: start it from a shell outside the tool's job (a Monitor or an OS scheduler).
- `gate long` lists running and recent runs, `gate long <id>` shows one; `status` shows a `long runs:` line.
- Schedule with the OS scheduler running `python -m aesir gate run soak --as lead` in the project (Task Scheduler on Windows, cron elsewhere).

## Conditional steps (`when_changed`, #154)

The queue runs the `full` profile on the merge commit. A step that should run only for some changes (a per-merge test tier for simulation code, say) is an inline table with `when_changed`:

```toml
[gates]
full = ["python -m aesir.paratest", { cmd = "python -m sim.tier", when_changed = ["sim/", "core/*.py"] }]
```

- Globs match like task scope: `dir/` is a subtree, otherwise an fnmatch glob on the repo-relative path. `{ audit = "new", when_changed = [...] }` works too.
- In `queue run` the changed paths are the merge commit versus main (its first parent), so only this task's change counts. `gate run` uses the merge base with main. If that base cannot be found the step runs.
- A step with no matching change is listed `skipped (no matching change: ...)`; it is never reported as run. A failed step refuses the merge with `full gate failed on <sha>: step <cmd> (exit N)`; later steps are listed `not run`.
- `queue run` prints one `gate step <result>: <cmd>` line per step, `gate run` one `step` line, and the task log's `gate full PASS|FAIL` line ends with `| steps: ...`, whenever a profile has more than one step or any step was skipped. Single-step profiles log as before.
- Long-run profiles ignore `when_changed` (every step runs).
