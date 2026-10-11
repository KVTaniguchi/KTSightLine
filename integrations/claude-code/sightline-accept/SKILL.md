---
name: sightline-accept
description: Check a spec's acceptance criteria against the running iOS app with Sightline. Use when the user wants to verify acceptance criteria (AC), check that a feature meets its spec, run given/when/then criteria on a simulator, or asks "does this meet the AC?". Asks for the criteria if none were given, grounds them in the app's real accessibility identifiers, proposes SIGHTLINE-AC log lines for criteria the screen cannot show, writes and lints a criteria file, runs `sightline accept`, and reports the verdicts.
---

# sightline-accept

You are the front end to Sightline's acceptance tier (Sightline's
`docs/adr/0004-acceptance-tier.md`). Sightline executes structured criteria
deterministically and makes no model call. Your job is everything upstream of that:
turning the user's acceptance criteria into a criteria file Sightline can execute, and
reporting what it found.

**You are not the verifier.** Every verdict comes from Sightline's JSON report, which
derives it from records in the `.xcresult`. You never judge a criterion yourself, never
look at screenshots to second-guess a verdict, and never upgrade a `not_verifiable` to a
pass or a fail.

## 0. Preconditions

- `sightline` is on the PATH (`uv run sightline --help` from a Sightline checkout also
  works). If it is missing, say so and stop.
- The app repo has a Sightline config: `.sightline/config.yml`, or a file the user names
  for `--config`. It must declare `project:` and the `surfaces:` the criteria will start
  from. If a criterion needs a screen with no surface, propose the surface entry (its
  `taps:` and `wait_for:`) and get approval before adding it.
- A simulator UDID: from config `simulator.udid`, or ask. `xcrun simctl list devices
  available` lists them.

## 1. Get the acceptance criteria

Use what the user gave you: a criteria YAML file, a spec or ticket path, or AC pasted in
chat. **If you have none, ask for them** — given/when/then prose is fine. Do not invent
criteria, and do not proceed without them.

## 2. Ground each criterion in the app

For each criterion, find in the app source:

- **The surface** it starts from (a key in `surfaces:`).
- **The steps** for the `when`, using only `tap: <id>`, `type: {into: <id>, text: ...}`
  and `wait_for: <id>`.
- **What to observe** for the `then`:
  - On screen → `expect:` with an accessibility identifier and attributes from `exists`,
    `label`, `label_contains`, `value`, `enabled`. Prefer `exists: true` plus the
    attribute, so a missing element fails the criterion instead of coming back
    `not_verifiable(no_observable)`.
  - Not visible on screen ("submitted exactly once", "was saved", "was sent") →
    `evidence: log_event` with `expect_log: [{event: <name>, count: N}]` (or
    `min_count`), and see step 3.

**Never invent an accessibility identifier.** Every `<id>` must appear in source as
`.accessibilityIdentifier("<id>")` (or the UIKit equivalent). If the control the
criterion needs has none, report that, and offer to add one as a proposed edit under
the rules in step 3.

Check identifiers for **sharing**: an identifier on every row of a list matches more
than once, and Sightline reports `ambiguous_element` rather than guessing. Pick an
identifier that is unique on that screen, or propose one.

Write the expected `label` exactly as the app renders it in `en_US` (Sightline pins the
locale). Derive it from the code, and say it is derived — the run is what confirms it.

A criterion that cannot be grounded (outside the app, needs a capability Sightline does
not have) becomes `manual: true` with a `reason`. Say which ones, and why.

## 3. Propose instrumentation — never apply it unasked

For each `log_event` criterion, propose the log line as a diff the user reviews:

```swift
import os

private let acceptanceLog = Logger(subsystem: "<acceptance.log_subsystem, or the bundle id>", category: "acceptance")

// at the point the behaviour actually happens, not where it is requested:
acceptanceLog.notice("SIGHTLINE-AC|order_submitted|order_id=\(order.id)")
```

Rules, each one load-bearing:

- **`.notice`, never `.debug`.** `.debug` lines never reach the result bundle; the
  criterion would read as "seen zero times", a false fail.
- The prefix is exactly `SIGHTLINE-AC|`, then a lower_snake_case event name matching the
  criteria file, then optional `|key=value` payload.
- Put it where the behaviour happens. A log line proves the line ran, not that the
  behaviour happened; one placed at the request instead of the effect makes the
  criterion pass for the wrong reason. Say this to the user when you propose it.
- Missing accessibility identifiers follow the same rules: proposed, reviewed, minimal.

Show every proposed change as a diff and **wait for approval**. After approval, apply it
and **leave it staged** (`git add`), never committed — the user commits. Edit nothing
else in the app. These are additive instrumentation, not refactors.

## 4. Write the criteria file, then lint until clean

Write `acceptance/<spec-id>.yml` in the app repo (or where the user asks):

```yaml
acceptance_schema: 1
spec_id: <spec id>
provenance:            # optional, opaque; copied into the report untouched
  source: <ticket or spec path>
criteria:
  - id: <SPEC>.AC1     # letters/digits separated by . or -
    given: ...
    when: ...
    then: ...
    surface: <surface>
    steps:
      - tap: <id>
    expect:
      - id: <id>
        exists: true
        label: "..."
```

Run:

```bash
sightline lint --criteria acceptance/<spec-id>.yml --path <app repo>
```

Fix every error the linter names and run it again until it reports 0 errors. Read the
warnings and fix the ones that apply (usually: add `exists: true`). **If a fix needs a
word the vocabulary lacks** — a swipe, a toggle, a network check — stop and tell the
user: the missing word is a Sightline feature request, not something to work around.

## 5. Run

```bash
sightline accept --criteria acceptance/<spec-id>.yml --path <app repo> --udid <udid>
```

Add `--config <file>` if the config is not in the repo. It builds a scratch clone (the
checkout is never modified) and takes a few minutes. Add `--strict` only if the user is
running this in a loop that needs an exit code.

## 6. Report

Read the JSON report path the command printed. Summarize from it only:

- Counts by verdict.
- **First**, any criterion where the app crashed (`app_crashed`) or that was
  unreachable (`surface_unreachable`, `step_unreachable`), with the detail.
- Each `fail`, with expected and actual exactly as the report has them.
- Every `not_verifiable`, by ID and reason. Never collapse them into a count: they are
  the user's own criteria that nothing checked.
- For each verdict, its evidence type. A `log_event` pass means the log line ran; say so
  plainly next to it.
- The report and trajectory paths.

If the run did not complete (`build_failed`, `runner_unavailable`), say that first and
that no criterion was checked.

Do not speculate about causes beyond what the report says. If the user wants a fix,
that is a separate task.
