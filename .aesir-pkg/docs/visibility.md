# Visibility first

Build the means to see what the program is doing alongside the first core code, not after the first mystery.

## What to build

1. State saved to a file: the running state can be written out and read back without the app being live.
2. Digest tools: turn raw data into information. Figures, counts and trends, with reference values beside them
   (expected, last run, limit). Raw data stays a paged fallback, never the first answer.
3. An event / per-step log: one line per step or event, so "what happened before it went wrong" has an answer.

"What is it doing?" questions go to the digests. A question no digest answers, if likely to recur,
becomes a new digest or tool, not a new measuring test. A clearly one-off question may use a saved single-use
script (question, date, author, result), promoted to a tool on second use (charter principle 9).

## Why

- Faster bug hunting: in a client project (VoxelTerrarium) one digest call found two real problems in seconds.
- Lower token cost: before digests, measuring tests piled up (task #072 spent 592k tokens). A digest is written once and
  answers every later question.
- Data -> information -> knowledge: see charter principle 9 (code makes information; agents make knowledge).

## Each project interprets this for its domain

This is a direction, not a format. Pick the state, figures and events that matter in your domain. One line each:

- Simulation / game: save file of world state; a digest of entity counts, rates and trends per tick against baselines;
  per-tick event log.
- Service / backend: state snapshot or dump endpoint; a digest of request rates, error counts and latency against the
  usual; structured request and job log.
- CLI / tool: saved run state or cache; a digest of what the last run read, changed and skipped, with counts; per-step
  log of the run.

Agents: where the project has digest tools, use them before writing a test or reading raw data. See
`observe-drive.md` for how a project registers its digest command.
