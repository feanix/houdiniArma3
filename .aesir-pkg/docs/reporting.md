# Reporting to Aesir, and who talks to whom

## What to report

Report every Aesir bug you hit, including Aesir-internal ones, and suggestions for the project. Do not report
Aesir's own planned features. Search first: `python -m aesir upstream search <words>`; on a match, comment with
`upstream reply <issue> <text>` instead of filing a duplicate.

## Filing

`python -m aesir report-upstream --title ... --body ... [--symptom <exact error>] [--repro ...] [--workaround ...]
[--kind bug|suggestion|internal] --severity high|medium|low`

- `--kind bug` (default): a defect in Aesir as you use it.
- `--kind suggestion`: an improvement idea for the project.
- `--kind internal`: a bug in Aesir's own machinery found while building Aesir.

`--severity` is required for `bug` and `internal`, optional for `suggestion`. The scale:

- `high`: damaged work or state, or could.
- `medium`: blocks a step or forces a workaround every time.
- `low`: annoyance, misleading display or wording.

The issue gets the label `severity: <level>` and its body starts with `Severity: <level>.`. Aesir does not relabel
existing reports; the reporting project relabels its own.

The kind becomes a label on the reports repo; `upstream search ... --kind bug` filters by it.

One-time label setup, by whoever owns the reports repo (needs repo admin; not for clients):

    for l in bug suggestion internal "severity: high" "severity: medium" "severity: low"; do gh label create "$l" -R feanix/aesir-reports --force; done

`report-upstream` also creates a missing `severity: <level>` label itself before filing (an existing one is fine).

Until the labels exist, `gh issue create --label` fails and the report is queued (below).

## When gh is down

If `gh` fails (offline, auth, rate limit), `report-upstream` saves the report in `.aesir/upstream-queue.json`
(next to the database), says so, and exits 0. `python -m aesir upstream retry` files the queue; `upstream watch`
also retries on every poll. `aesir status` shows `pending_upstream` while reports are queued. A missing `gh` is
still an error, not a queue.

## Cross-project messages go to the advisor

Messages between projects go only to the other project's advisor, never its lead or other roles:

- A client project notifies Aesir at `aesir-advisor` (the `notify:` line `report-upstream` prints).
- Aesir's notices to clients (upgrade available, workaround pings) go to `<project>-advisor`, not
  `<project>-lead`.

The receiving advisor routes the work inside its own project.
