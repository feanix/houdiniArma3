# Supervising rounds, and never blocking a session (#156)

Shipped guide (`.aesir-pkg/docs/supervision.md` in a client). The rules are the owner's (D159, via VT); each project
interprets the signals for its domain in `aesir.toml` `[rounds]`.

## Rule 11: never block a session

A session that waits in the foreground cannot be steered, cannot answer the owner and is cut by tool timeouts. So:

- Never `Wait-Process`, `sleep` loops, `sleep N` between polls, or any long foreground command to wait for a
  process, a run or a log line. A steering note cannot land mid-sleep.
- Every wait goes through a Claude Code **Monitor**: a loop that exits on the event AND on every end state (success
  line, failure line, process gone), polls every 5-10 s, has a bounded timeout, and is re-armed if needed.
- Division of labour:
  - **Monitor**: one awaited event (a process, a log marker, a task's long run). Use `wait` below.
  - **CronCreate**: recurring checks of a round (the advisor's backstop, below).
  - **Workflow completion notice**: agent rounds started as a Workflow tell you when they end; do not poll them.
- Background the long command (paratest, a soak, a build) with its output to a log, then `wait --log` under a Monitor.

### The waiter: `wait`

    python -m aesir wait (--pid N | --log PATH | --task N) [--end RE] [--fail RE] [--poll 5] [--timeout 900]
    python -m aesir wait ... --print-monitor     # prints the command to arm instead of waiting

It prints exactly ONE final line and exits:

| line | exit | meaning |
|---|---|---|
| `wait: end <line>` | 0 | an end marker matched |
| `wait: fail <line>` | 1 | a failure marker matched (wins over the end marker on the same line) |
| `wait: gone pid N` | 0 / 1 | the process ended; 1 if a log with end markers was watched and none appeared |
| `wait: timeout <s>` | 3 | nothing happened in time: re-arm, or look |

- `--log` scans the file from its start (wait on a fresh log), incrementally, whole lines only; a log that does not
  exist yet is waited for. `--pid` may be combined with `--log` (the process ending is also an end state).
- `--task N` follows the task's long run (`gate run <profile> --task N`, `gate long`) to its end: passed = end,
  failed or timed out = fail. Markers are regexes; `--end`/`--fail` repeat.
- Markers default to `[rounds] end_markers` / `failure_markers` in `aesir.toml` (lists of regexes), else
  `^OK`, `^Ran \d+ tests`, `gate \w+ PASS` and `^FAILED`, `Traceback`, `gate \w+ FAIL`. Put the project's own
  end/failure lines there (a soak's `end:` line, a build's `error:`), so every agent waits the same way.
- Arm it: `wait ... --print-monitor` prints the command; pass it to the Monitor tool with a `timeout_ms` above the
  waiter's `--timeout`. Each final line is the event.

Example (a backgrounded full suite):

    python -m aesir.paratest > run.log 2>&1 &        # background, output to a log
    # Monitor command: python -m aesir wait --log run.log --timeout 900

## Watching a round (lead)

A round is one spawn working a task (Agent tool or Workflow). Aesir holds its state in the database (the `round`
commands), so nothing lives only in a cron prompt or session memory.

1. On spawn: `python -m aesir round arm --task N [--workflow WF] [--label L] [--limit 45m]`.
2. Watch it under ONE Monitor: `python -m aesir round watch` (about 3 min per pass; one line per round per pass:
   branch head subject, uncommitted files, agent tokens, daily budget %, longest step and its age if the project
   sets `[rounds] longest_step`). Every line is an event, so the session never blocks. The loop ends by itself when
   no armed round remains (`round watch: no armed rounds`); on `watch: restart` (the Monitor's 30-minute cap)
   re-arm the same command.
3. Disarm when the task is merged or verified: `round disarm --task N` (`round check` also disarms rounds whose
   task reached merged, verified or wontfix).
4. A round with no progress for 2 x `[rounds] interval` of wall-clock time (however many checks ran), or past its
   wall-clock limit, is flagged `stuck`. `round check` records a check; `round show` / `round list` is the read-only
   view (state, last progress, idle time, live tokens as output and context, open command). A round without `--agents` counts the task's
   running agents plus those started since arming. A stuck round gets a `round_stuck` anomaly
   in `status` and a message to the lead, with the evidence. Aesir never stops a round itself. The lead looks first.
5. Progress signals are `[rounds] progress` in `aesir.toml`: `commit` (new commit on the branch), `evidence` (new
   evidence for the task), `agent` (any of the round's agents wrote to its transcript), `testlog:<glob>` (a newer
   matching file in the branch's worktree). Default: commit, evidence and agent (a list set explicitly in
   `aesir.toml` is kept as written). A project adds its own, such as a test-log directory.
6. A running agent's tokens in `round watch` are read live from its transcript (the path is stored when the agent
   binds). While a running agent has an open tool call (a test suite or gate; the transcript shows a call with no
   result yet) younger than the step limit, silence is not flagged. The limit is `[rounds] step_limit` (a duration,
   e.g. `20m`; default 30m; `longest_step` stays the display command). Past it the round is stuck as before and the message names the command.
   Re-arming a live round with a new `--branch` or `--workflow` resets its baseline and stuck state.

### Advisor backstop

The advisor does not poll rounds. It arms one session-only CronCreate `*/10 * * * *` whose prompt is
`python -m aesir round check --all` and acts only on a `stuck` line the lead has not handled. CronCreate fires
only when the session is idle, so checks can be late; the round state lives in Aesir, not the prompt. CronDelete it
when the rounds are merged and verified.

## Stop and respawn

When a round is going wrong (rule broken, stuck, wrong route):

1. Prefer a steering note where it works: `python -m aesir note <agent> "<text>"` to a running agent (it is read at
   the agent's next tool call, so it saves a respawn when the agent is still moving; a hung agent never reads it).
2. Otherwise stop the round (TaskStop on the Workflow or agent task id; it kills the children) and **keep the
   worktree and branch**.
3. Respawn WITHOUT isolation into the existing worktree and branch. The brief is the old brief plus a section:

        ROUND N CORRECTION
        - What was wrong, with the evidence (log line, commit, command output).
        - The rule it broke.
        - The route to use instead.
        - Fallback if that route is closed.
        - Stop-and-report: when to stop and say so instead of improvising.

4. Tell the advisor (`round disarm` the old round, `round arm` the new one).

## Budgets and briefs

- No per-task token budgets or stop lines by default. A budget cap kills good work half done and invites padding.
  Token figures in `round watch` are a record, not a limit.
- Token estimates matter only near the global daily/weekly stop: when the usage statusline (`daily_budget_pct`)
  nears it, estimate what the remaining rounds need and decide what to park.
- Every implementer brief carries three things: **commit per step** (so progress is visible to the progress signal and
  work survives a stop), a **checkpoint** (what to report after the first step or at the halfway point), and a
  **wall-clock limit** (the `--limit` given to `round arm`; at the limit, stop and report where it stands).

## Aesir's own blocking waits, reviewed against rule 11 (#156)

| wait | verdict |
|---|---|
| `gate run <profile> --wait` (long runs) | foreground by design; changed in help and docs: use only under a Monitor. Default (detached) plus `wait --task N` is the rule-11 path |
| `wait <M#>` (a reply to an ask, default 600 s) | blocks; now part of `wait` (same command); use only under a Monitor, or check `inbox` |
| `gate run` / `queue run` (merge gate, minutes) | foreground gate: background it, `wait --log` the output, or run under a Monitor; `--lock-wait` bounds the slot wait |
| budget/gate-slot and lock polls (`budget.py`, `gates.py`) | short internal polls inside one bounded command, not session waits |
| `gate timing`, `--load-test` | bounded measurement runs: run backgrounded with a log and `wait --log` |
| `upstream watch`, `at` | own detached/long-lived processes; never run in the foreground of a session |
| tip 46 (full suite takes minutes, pass a tool timeout up to 600000 ms) | a long timeout still blocks the session: background the suite to a log and `wait --log` it under a Monitor |

## Restarting a session while agents run (#195)

Restarting the lead orphans its running Workflow agents: their owner session is gone, nothing reads their results.

- The restart warning (`resume`, `status` session_large) names agents still running with recent transcript activity
  and says to restart only after they end. Wait for them, or `round`/`agent end` them on purpose first.
- A running agent whose transcript has had no write for `[observe.aesir] agent_idle_min` (default 10) is shown as
  `no activity Nm; owner session may be gone` (`resume`, and the `idle_agent` anomaly in `status`).
- If that quiet agent also has no open tool call, its Workflow has ended: `resume` and `status` end the row
  ('ended stale on resume'), keeping the tokens read from its transcript. Any agent ended without a SubagentStop
  (stale, killed, superseded) keeps its transcript tokens.
- A re-spin (a new implementer for a task) ends the earlier bound implementer row of that task ('superseded by <id>').
  So never run parallel implementers on one task: the second supersedes the first.
- An open tool call younger than `[rounds] step_limit` (default 30m) excuses a quiet transcript: such an agent is not
  labelled idle and counts as active. Running agents with no transcript are named in the warning as 'unknown activity'.
- A claimed or in_progress task with no running agent and no task-log line for `no_agent_min` (default 1440, 24h)
  is a `no_live_agent` anomaly ('set status or unclaim'); `status ack no_live_agent:<id>` pauses it.
