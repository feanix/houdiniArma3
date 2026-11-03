# Gate concurrency design (#104, serves #106)

Status: #104 built (aesir/budget.py, aesir/gates.py gate_lock, aesir/paratest.py). Lead answers are D125. The long lane
(#106) is built on `budget.LANES['long']` and `budget.lease(lane=)`: aesir/longrun.py, docs/gates.md.

## 1. Cause of STATUS_DLL_INIT_FAILED (0xC0000142)

0xC0000142 = a process started but a DLL init routine failed (user32/kernel32 attach). On Windows the
classic cause is desktop-heap exhaustion: every process (and each console host or window it owns) charges
the session desktop heap; when full, new processes die in loader init. Second cause: commit-charge
(pagefile) exhaustion. Both scale with the number of live processes, not with CPU.

D61 context: 3 gates at once plus ~52 stale python processes from another tool. Since #099 each gate
spawns cpu-2 = 18 workers (plus runner and job-object children), so 3 gates are ~54+ processes: the same
order as D61 even without strays. Most likely cause: total live process count in one session crossing a
heap/commit threshold; the stray processes were the multiplier.

Evidence gathered (read-only registry; bounded spawn test; every spawned process exited by itself):
- Machine: 20 logical CPUs (so the #099 default is 18 workers). Python processes alive at start: 1.
- Registry HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\SubSystems, Windows value:
  `SharedSection=1024,20480,768` (system heap 1 MB, interactive desktop heap 20 MB, non-interactive
  desktop heap 768 KB). The interactive heap is generous; a gate started from a non-interactive session
  (scheduler, service-like agent host) gets only 768 KB and would hit the limit far sooner.
- Experiment: N concurrent `python -c "import time;time.sleep(4)"` processes via subprocess.Popen in a
  temp dir, all started at once, return codes collected (verbatim output):
  ```
  20 failed 0 [] spawn+run 4.1s
  40 failed 0 [] spawn+run 4.2s
  60 failed 0 [] spawn+run 4.2s
  80 failed 0 [] spawn+run 4.4s
  ```
  No larger probe is run (D125); the stress run under the budget (section 5) is the confirmation.
- Conclusion: NOT reproduced. 80 trivial processes are safe in an interactive session, so 18x3=54 workers
  alone is not proven to fail here. The D61 failure needed more load (strays, real imports with larger
  commit, possibly a non-interactive session). Status: hypothesis (desktop heap / commit charge from total
  process count), unconfirmed. Record on the task "bounded by total live processes" and enforce a
  process budget; the criterion-4 stress run is the real confirmation.

## 2. Lock model (as built)

Resources, not one lock. All files live in the gate dir (`AESIR_GATE_DIR`, else beside `AESIR_GATE_LOCK`, else the
temp dir). Gate steps get the dir as `GATE_BUDGET_DIR` (not AESIR_-prefixed, so the AESIR_* strip keeps it).
- Slot lock (per working tree): `gate-<sha1(tree path)[:12]>.lock`, same OS-lock primitive as the legacy lock, label
  `pid label @start`. A gate takes it for its `key` (run_profile: the gate's cwd; merge queue: the project root).
  Different projects and different worktrees do not block each other; one tree runs one gate at a time.
- Legacy lock: `[machine] legacy_lock` (default true). True: after the slot, a new gate also takes
  `%TEMP%/aesir-gate.lock` (pid file + `.os` OS lock, protocol untouched), so gates are serial exactly as before and
  older pinned releases exclude each other and us. False: the legacy file is not consulted; slots plus the budget
  allow concurrency. No group handover (D125). Flip to false only when every client on the machine runs a
  budget-aware release. Env `AESIR_LEGACY_LOCK=0` overrides for one process (stress runs).
- Worker budget (machine-wide): `aesir-budget.json` guarded by a short OS lock `aesir-budget.lock`. Total
  B = `[machine] workers`, default min(cpu-2, 24) (min 2). Entries: one per running gate (kind gate, n 0) and per
  worker lease (kind workers, n workers, lane), each with pid + creation time.
  - Admission (`budget.gate_slot`, inside `gate_lock`, after the slot and legacy lock): a gate starts when fewer than
    `max_gates` (default 3) gates run and at least 2 workers are unleased; otherwise it waits (bounded by lock_wait,
    then `GateLockBusy`).
  - Lease (`budget.lease(want, lane=)`, called by paratest in place of cpu-2): grants
    `max(2, min(want, (free - reserve) // unleased_gates, cap))`. `reserve` keeps 2 workers per still-empty gate
    slot, so a lone gate does not starve a later one, and gates started together share fairly. Free is B minus leased
    workers minus stray python processes (count above what the leases and gates explain; solo: above 4), floor 2
    when nobody holds a lease. When free < 2 the merge lane waits up to 120 s, then runs with 2 anyway (a slow gate
    beats a failed gate).
  - Lanes (`budget.LANES`): merge (min 2, waits, cap B). long (#106 hook): min 1, never waits, cap B/2; no command
    uses it yet. The long run's own lock files and entry point belong to #106.

## 3. Compatibility with older pinned releases

Old releases only know `%TEMP%/aesir-gate.lock` (+ OS-lock sidecar and pid-file protocol, see `_claim_legacy` in
aesir/gates.py). Its meaning never changes; new code only adds files (`gate-*.lock`, `aesir-budget.*`).
- With `legacy_lock = true` (default) a new gate holds the legacy lock for its whole run: an old gate waits for it,
  it waits for an old gate. Behaviour is today's, serial.
- With `legacy_lock = false` an old-release gate can overlap new gates (it does not know the budget). Residual risk,
  accepted by D125 for machines where all clients are upgraded.

## 4. Crash safety

- All locks are OS byte-range/flock locks on open handles: a killed process drops them with its handle.
  Files are never unlinked (avoids the read+unlink race, aesir-reports#20).
- Budget leases carry pid + process creation time (ctypes GetProcessTimes on Windows; existing
  `pid_alive` on POSIX). A lease with a dead pid or mismatched creation time (pid reuse) is dropped on
  the next budget access. No reliance on a clean exit.
- Gate steps already run in a kill-on-close job object (`_make_job`), and paratest runs inside a step, so its workers
  die with the gate; the lease entry then has a dead pid and is dropped on the next access. Long runs start detached (DETACHED_PROCESS |
  CREATE_NEW_PROCESS_GROUP) under a small supervisor that holds the longrun lock and its own kill-on-close
  job, enforces the per-profile timeout, and writes status (running/passed/failed/timed out) and
  evidence. A killed supervisor drops the job, so the tree dies and the lock frees.

## 5. Test plan and runs

Automated (`python -m unittest tests.test_104_gate_concurrency`): all tests set `AESIR_GATE_DIR`/`AESIR_GATE_LOCK` to a
per-test tempdir (tests/sandbox.py guards the machine files); the budget total is a setting, not the host CPU count.
- Stress (c4, automated): 4 trees (2 projects, 2 worktrees of one) gate at once through `gates.run_profile` with
  `legacy_lock = false`; the step leases workers and samples the budget; leased <= B and gates <= max_gates at every
  sample, gates overlap, all pass, no lease/gate entry or slot label left behind.
- Slots: same tree serialises, different trees overlap, legacy true keeps one gate per machine, legacy false ignores a
  held legacy file, max_gates caps, a killed holder frees slot and entry, nested gate does not wait.
- Budget: fair share, wait then minimum, strays, dead-pid and pid-reuse reclaim, corrupt state, long lane.
- Queue (real `queue.run`): another tree's gate does not block a merge, the same root's gate does.

Manual c4 (3 concurrent full gates, 3 passes in a row), after merge, from E:/Projects/Aesir, each pass:
```
export AESIR_LEGACY_LOCK=0
python -m aesir gate run full > /tmp/g1.txt &          # Aesir main checkout
(cd <aesir worktree>   && python -m aesir gate run full > /tmp/g2.txt) &
(cd <VT project>       && python -m aesir gate run full > /tmp/g3.txt) &
wait
```
Expect three PASS, no `0xC0000142`/`flaky-retry` in the tails, `aesir-budget.json` leases empty afterwards
(`python -c "from aesir import budget; print(budget.snapshot())"`), and `gate-*.lock` files empty. Persist the setting
with `[machine] legacy_lock = false` in each project's aesir.toml instead of the env var.

Manual c5 (before/after): started together, Aesir full gate and VT full gate. Before: `AESIR_LEGACY_LOCK=1` (default);
after: `AESIR_LEGACY_LOCK=0`. Time with `time ( A & B & wait )`, same two commands both times.

## Decisions (D125, formerly open questions)

1. No larger spawn probe; the stress run under the budget is the confirmation.
2. B = min(cpu-2, 24), max_gates = 3.
3. No legacy group handover: `[machine] legacy_lock` (default true) serialises through the legacy lock.
4. A long run (#106) starts with min 1 worker, cap B/2, never blocks merge gates.
5. Long-run entry (#106): `gate run <profile>` on a profile marked long; status table via the next free migration number
   at merge time.

## Load test (#144)

`python -m aesir gate timing --load-test` raises CPU load in stages (25%, 50%, 75% of logical cores, 30 s each) and
prints per stage the measured system CPU use and a responsiveness probe (wall time of `python -c pass`). Owner-runnable
with one command: `! python -m aesir gate timing --load-test` (about 2 minutes). Options: `--levels 10,25` (percent),
`--stage-secs 2`.

- Cap: 75% is a constant in `aesir/load.py` (`MAX_SHARE`); every input is clamped, no flag or config exceeds it.
- Few processes (at most 4) with busy threads (hashlib releases the GIL), duty-cycled to hold the share; a few MiB total.
  Duty cycles run on an absolute 0.25 s schedule (a late timer wake shortens the next rest; #140 fixed an undershoot
  where Windows slept ~15 ms too long per 50 ms period and 75% applied only about 60%).
- Each stage reports the machine-wide CPU and the generator's own share (workers write their process CPU time to small
  report files); the difference is other work on the machine.
- Each worker has a deadline (never more than 900 s) and also exits if its parent dies. Ctrl-C, errors, SIGTERM/SIGBREAK
  and normal exit stop all workers; nothing persists (no files, no DB writes).
- Measured CPU: GetSystemTimes (Windows) or /proc/stat; stdlib only.
- Guard: the probe is first measured at 0% load. If a stage's probe exceeds 5x that baseline (baseline floored at 0.05 s),
  all workers stop at once and no further stage runs ("stopped early").

## Timing pair (#140)

Owner-runnable with one command, from the project that is one side of the pair:
`! python -m aesir gate timing --with <other project path>` (a client uses `python .aesir-pkg/aesir_run.py gate timing ...`).

- Checks both trees are clean first (stops with a message naming the dirty tree and a file otherwise).
- Run 1: both projects' `gate run full` started together, lock on (`AESIR_LEGACY_LOCK=1`); run 2: both started together,
  `AESIR_LEGACY_LOCK=0`. The variable is set only in the child processes' environment; no setting or file persists.
  Each project runs through its own launcher (`.aesir-pkg/aesir_run.py` if present, else `python -m aesir`).
- Prints one table: per run and project start, end, wall, gate duration, lock wait (wall minus gate duration, so it
  includes process start-up), PASS/FAIL, background load, machine CPU and generator CPU during the run. The result is
  also recorded as a DB event; `gate timing --last` (optionally `--json`) shows it again, for the advisor.
- Ctrl-C, errors or SIGTERM kill every child gate (process tree) and load worker.
- `--profile <name>` (default full); `--load <percent>` holds background CPU load (same generator, cap 75%) during both
  runs; the load workers stop at the end of each run. Run `gate timing --load-test` first and have its results reviewed
  before using `--load` in a pair.
- Tests: tests/test_140_timing.py (two temp repos with short gates; no real load, no machine lock files).

## Arrival order at the machine lock (#180, aesir-reports#89)

Waiters for the machine gate lock (`%TEMP%/aesir-gate.lock`, when `[machine] legacy_lock` is true) are served in arrival
order. Each waiter writes a ticket file (JSON: pid, process start token, class, arrival ns) into the new directory
`<lock path>.tickets/` and only the first live ticket tries the lock; the rest wait and print
`waiting for gate lock held by <holder>; position 2 of 3`.

- Class: a `queue run` (`PRIO_QUEUE`) is served before plain gate runs (`PRIO_GATE`); FIFO within a class.
- Stale tickets (owner pid gone, or pid reused: start token differs, as `budget.process_start`) are skipped and unlinked
  by whoever polls. The owner removes its own ticket on acquire, timeout (`GateLockBusy`) or error, so a crash cannot
  wedge the line.
- Compatibility (D86): the lock files and the legacy pid-file protocol are unchanged. Older Aesir knows nothing of the
  tickets directory, keeps contending for the lock as before and is never blocked by tickets; fairness holds among
  versions that have this change.
- Tests: tests/test_180_gate_line.py (temp `AESIR_GATE_LOCK`, never the real lock or tickets).
