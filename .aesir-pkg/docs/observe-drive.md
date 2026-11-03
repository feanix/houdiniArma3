# Observe and drive

Audience: you, the agent. Goal: you see and operate the running app yourself. The owner is not your sensor.
If you catch yourself asking the owner "what do you see?", the missing piece is a tool, not an answer. Build the tool.

## Why

You read code; you do not see the running app. Without eyes and hands the loop becomes: owner describes bug,
you guess a fix, owner screenshots, repeat. Slow, lossy, and it spends the owner's attention on your job.
Fix it once, structurally: give the app a way to be questioned and a way to be operated by an agent.

## Observe

Make state answerable by questions, not dumps.

- **Question-style tools.** One tool per question a debugger would ask ("what is on screen?", "why is X
  disabled?", "what changed since step N?"). Each returns a judgement plus the few facts behind it, not raw state.
- **One menu.** A single entry point lists every observe tool with a one-line purpose. You find tools by reading
  the menu, never by guessing names or reading source.
- **Broad, then narrow.** First answer is a summary (counts, status, anomalies). Detail is a follow-up call
  scoped by id, region or time range.
- **Tools flag anomalies themselves.** Do not rely on the reader to spot the problem in a wall of data. The tool
  computes "3 elements overlap", "queue is empty but the worker is busy" and says so first.
- **Every answer suggests next tools.** End with 1-3 concrete follow-up calls relevant to what was just found.
  The chain from symptom to cause should be walkable by following suggestions.
- **Page everything.** Every list has a default cap, a total count, and a way to ask for the next page. An
  unbounded answer burns your context and hides the signal.

### UI layout as data

The cheapest way to "see" a UI is its layout as structured data, not pixels.

- Every element has a stable id, a kind, text, state, parent, and bounds (x, y, w, h in a stated unit).
- A layout tool lists elements broad-to-narrow (screen, panel, element) and computes checks:
  overlap between siblings, misalignment against neighbours or a grid, off-screen or clipped, text overflowing
  its box, zero-size, hidden-but-focusable.
- Output names the offenders by id with the numbers ("btn_ok and lbl_status overlap by 14x8 at 120,40").
- Ids are the contract between observe and drive: what you read here is what you click there.

### Screenshots: last resort

Use pixels only for what data cannot say (colour, rendering glitches, "does it look right").

- Triggered by a specific question or event, never by habit or on a timer.
- One moment, one targeted region (crop to the element or panel), not the full screen.
- Downscaled to the smallest size that still answers the question.
- Annotated: ids and bounds drawn on, so pixels map back to elements.
- Never a sequence. To see change over time, use a state diff between two steps.

## Drive

You operate the app as a human would, through the same inputs.

- **Everything a human can do, you can do.** Keys, pointer, text entry, menus, dialogs, drag, scroll, window
  events. If a feature cannot be driven, it is not finished.
- **Target by element id**, taken from the layout data. Never by raw coordinates; coordinates break on any
  layout change and teach nothing. The driver resolves id to a point itself.
- **Headless from day one.** A client with no window, runnable in CI and by you, that starts the app, takes
  input, and answers observe questions.
- **Same code paths as the real client.** Headless means offscreen rendering of the real UI, not a fake or
  mock UI. If the headless client passes, the real client passes; otherwise the harness is lying.
- **Deterministic stepping.** The harness advances the app in explicit steps (one tick, one frame, one event,
  until-idle) with a seeded clock and RNG. Same inputs, same outputs. No sleeps, no wall-clock waits.
- **Scenarios are scripts.** A scenario is a list of drive steps and observe assertions, stored in the repo,
  rerunnable. A bug fix lands with the scenario that failed before and passes after.

### Never steal the owner's focus

The owner types on the same desktop you run on. A window that grabs focus swallows their keystrokes.

- **Headless is the default** for every drive and observe run, including tests and scenarios.
- When a real window is unavoidable, it must open without taking focus: no-activate, minimised or offscreen.
  Declare such a command in `[observe] window`; `aesir observe check` fails it if it carries no no-focus flag
  (it is checked, never run).
- **Batch runs.** One process runs many scenarios; never spawn a window per step or per test.
- **Visible runs** (a window the owner can see or that may take focus) need owner OK, asked through the advisor,
  before the run.

## Real-world gate

Before the first owner or real-environment test of any networked or performance-sensitive feature, the
real-world gate must hold. Otherwise the owner becomes the sensor for setup, logs and environment, and a
hypothesis gets refuted only by the owner (aesir-reports#8).

- **(a) Environment in the session record.** Each session record captures hardware class, network type, power
  state, window/focus state, and timer/pacing stats (frame/tick interval min/mean/max, dropped or late ticks).
  Invariant: non-identifying. Classes and buckets only; no hostnames, user names, addresses, serials or paths.
- **(b) Every participant's logs reach the analysing side with no manual steps.** Each peer ships its own log
  and environment record automatically (same session id on all sides). The owner never copies, zips or
  describes a log.
- **(c) One digest compares all sides.** A digest question lines up every participant's record for a session
  and flags divergence (environment mismatch, one-sided events, clock or pacing skew), verdict first.
- **Proof.** Declare a command in `[observe] realworld` that exercises (a)-(c) headless (e.g. two local peers).
  `aesir observe check` runs it; `"n/a"` means the project has no networked or performance-sensitive feature.
- **Lead checks the gate before handing the owner a build for real-world testing.** Gate not met: build the
  missing tool first, as a task.

## Recording evidence for a UI task

`evidence run` on a task flagged ui accepts only a command that starts with an `[observe]` digest or headless
command, and needs `--expect <text>` the output must contain. Aesir's headless is the viewer test
(`python -m unittest tests.test_047_viewer`). The config is read from main, so a headless check that exists only on
the task's branch is refused: add it to `[observe]` on main first, then record at the branch head. Lead override
(`--force-evidence-skip --reason`) must cite the gate run and test output.

## Order of work

Build observe and drive before the features they cover. A feature built blind gets verified by the owner, which
is the failure this guide exists to prevent.

1. Minimal observe menu and headless driver for the app shell.
2. Per feature: add its observe questions and drive coverage together with the feature.
3. Each task's acceptance criteria name the digest question and/or headless scenario that verifies it
   ("`layout` shows no flags on the settings panel"; "scenario `save_and_reload` passes").
   A criterion that only says "works" or "looks right" is not verifiable; rewrite it.
4. Every test, run and review round in a brief or spec states its purpose before it starts: the step or decision it
   serves and what it could realistically catch. Confirming what is already believed is not a purpose. Measuring
   belongs in an observe/digest tool, not a new test.

## Anti-patterns

- Screenshot sequences, or periodic screenshots "to watch what happens".
- Raw state dumps (full JSON, whole logs, entire widget trees) as the primary answer.
- Clicking by coordinates; sleeping and hoping; fixed delays in tests.
- Owner-described bugs as the only evidence ("the button looks off").
- A fake or mock UI in the headless client that diverges from the real one.
- Observe tools that return data but no verdict, or verdicts with no supporting facts.
- Unpaged lists; tool names you must guess; dead-end answers with no next step.
- Verifying a UI feature by reading its code.
- Checks with no stated purpose: confirm-only tests, re-recorded expected outputs, review rounds that cannot say what they would catch.
- Opening real windows per test or per step; any window that takes the owner's focus.

## Checklist (implementer and reviewer)

Apply to any task that touches running behaviour or UI.

- [ ] Acceptance criteria name a digest question or headless scenario that verifies the change.
- [ ] The scenario or question was actually run; its output is quoted in the report, not paraphrased.
- [ ] New state is reachable through an observe tool; the tool is on the menu.
- [ ] Observe answers are summary-first, paged, flag anomalies, and suggest next tools.
- [ ] New UI elements have stable ids and bounds in the layout data; layout checks are clean (no overlap,
      misalignment, off-screen, overflow).
- [ ] Every new user action can be driven headless by element id.
- [ ] Headless uses the real code path (offscreen render); no fake UI was added.
- [ ] Stepping is deterministic: seeded, no sleeps.
- [ ] No screenshots were needed; if one was, it is a single cropped, downscaled, annotated region with a
      stated reason.
- [ ] A bug fix includes the scenario that reproduced it.
- [ ] Nothing in the report depends on the owner describing what the app looks like.
