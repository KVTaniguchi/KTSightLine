# Brief: the acceptance tier

> Run Claude Code from the root of the Sightline repo and say:
> **"Read this brief and follow it. Start in plan mode."**
> `PROMPT.md` is the founding brief and stays verbatim. Its role, working rules, and
> traps still apply here; this brief only adds to them.

---

## What I want

Sightline reviews a diff for defects nobody asked about. I want a second mode that checks
what a spec *promised*: given a file of acceptance criteria, build the branch, drive the
app on a simulator, and report a verdict per criterion, each backed by an artifact.

The context is spec-driven development, where an agent harness turns a spec into code in
stages. The specs already carry given/when/then acceptance criteria. Today nothing
executes them, so "the agent says it's done" and "the criteria hold in a running app" are
the same claim. This tier separates them. Its report is also the fastest quality signal
for whoever tunes the harness: first-attempt pass rate per criterion, minutes after
generation, before a PR exists.

Same rule as the rest of Sightline: a verdict that is not backed by an artifact is not
reported as pass or fail.

## Read first

1. `PROMPT.md`, then `docs/adr/0001` through `0003`, `docs/decisions.md`,
   `docs/open-questions.md`. Do not relitigate what they settle (D4 especially: no MCP
   dependency; we vendor `xcodebuild` and `simctl`).
2. The pipeline this reuses: `sightline/review.py` (`_run_runtime_tier`),
   `sightline/config.py`, `sightline/runners/xcode/driver_template.py`,
   `sightline/runners/xcode/injection.py`, `sightline/runners/xcode/xcresult.py`,
   `sightline/core/verify/gate.py`, `sightline/core/findings/models.py`,
   `sightline/core/telemetry/trajectory.py`, `sightline/eval_harness.py`.
3. `eval/fixtures/CheckoutDemo/README.md` and `eval/README.md`.

Then tell me where this brief is wrong about the code before you plan anything.

## Scope

**In, for this pass:**

- A criteria file format, strictly validated, with a linter that runs on Linux with no
  Xcode.
- A generated XCUITest driver that executes criteria: launch with a fixture, reach a
  surface, perform steps, observe expectations, capture a screenshot.
- A three-way verdict per criterion (`pass`, `fail`, `not_verifiable`), derived only
  from records in the `.xcresult`.
- `sightline accept`: a local command that needs no PR and no forge, so it runs in a
  worktree before a PR exists.
- A versioned JSON report, a trajectory record, and a markdown summary.
- An acceptance corpus in `eval/` scored on verdict agreement.
- ADR-0004 and the doc updates that follow from it.

**Out. Do not build these, and do not design them beyond a line in "open questions":**

- Any model call. This whole tier is `model_tier: none` and costs $0.
  `sightline/adapters/model/` stays empty.
- Translating given/when/then prose into steps. The criteria author (a person or an
  upstream agent) supplies the structured steps and expectations; Sightline executes
  them.
- Posting to a PR. That needs a new `ForgeAdapter` method and my authorization (see D12).
- Finding criteria from a PR body or commit trailers; wiring into `sightline review` or
  `sightline.yml`.
- Executors for `log_event`, `network_call`, and `persisted_state` evidence. They parse,
  and resolve to `not_verifiable`.
- Device, appearance, and Dynamic Type matrices. One simulator, one configuration.
- Anything that knows about Jira, reverts, CI attribution, or any particular company's
  spec format. Sightline stays generic and open-sourceable. Provenance is opaque data it
  carries through to the report and never interprets.

## The contract I'm proposing

My opening bid. Push back where it's wrong, then write it down in ADR-0004.

### Criteria file

```yaml
acceptance_schema: 1
spec_id: CHK-7
provenance:              # optional, opaque strings, copied into the report untouched
  harness_version: 3f9c2e1
  stage: implement
criteria:
  - id: CHK-7.AC1
    given: a cart holding the sample order
    when: the shopper continues to checkout
    then: the subtotal covers every line item times its quantity
    surface: CheckoutSummary          # key into config `surfaces:` (navigation is reused)
    expect:
      - id: checkout.subtotal         # accessibility identifier
        label: "Subtotal $441.00"
    evidence: ui_state

  - id: CHK-7.AC2
    given: the shopper is on the payment screen
    when: they place the order
    then: the confirmation screen offers order history
    surface: PaymentMethod
    steps:
      - tap: payment.placeOrder
    expect:
      - id: confirmation.viewHistory
        exists: true
        enabled: true

  - id: CHK-7.AC3
    given: an order was placed
    when: the confirmation screen appears
    then: the order was submitted exactly once
    evidence: network_call            # no executor in v1

  - id: CHK-7.AC4
    given: an order was placed
    when: a minute passes
    then: a confirmation email arrives
    manual: true
    reason: outside the app
```

- `given`/`when`/`then` are prose for the human reader and the report. They are never
  parsed. `fixture`, `surface`, `steps`, and `expect` are what executes.
- `evidence` defaults to `ui_state`, the only type with an executor in v1. A `ui_state`
  criterion needs at least one expectation or a `manual` flag. The other types need no
  `expect` yet; lint warns that they will come back `not_verifiable`.
- `fixture` (optional) is a key into a new config map,
  `fixtures: {name: {launch_arguments: [...], launch_environment: {...}}}`. Sightline
  passes them at launch and does not know what they mean; honoring them is the app's
  job.
- `steps` and `expect` are closed vocabularies, in the sense of ADR-0001 §3: Python in
  `runners/`, versioned and tested. Steps for v1: `tap`, `type` (into, text),
  `wait_for`. Expectation attributes for v1: `exists`, `label`, `label_contains`,
  `value`, `enabled`. A criterion that needs something outside the vocabulary fails lint
  with a message naming it. The missing name is the ticket title.
- `extra="forbid"` everywhere, like `SkillFrontmatter`. A typo'd key that silently means
  "never checked" is the failure that makes people stop trusting this.

### Verdicts

| Verdict | Requires |
|---|---|
| `pass` | Every step completed, and every expectation was observed true, each with a record in the `.xcresult` |
| `fail` | Every step completed, and at least one expectation was observed false. The record carries expected and actual |
| `not_verifiable` | Everything else, with a reason from a closed set |

Proposed reasons for `not_verifiable`: `manual`, `evidence_type_unsupported`,
`no_observable`, `surface_unreachable`, `step_unreachable`, `ambiguous_element`,
`build_failed`, `runner_unavailable`, `no_record`.

Two rules that matter more than the table:

1. **Ambiguity never resolves to `pass` or `fail`.** This is ADR-0002's gate applied to
   verdicts. A false pass is the disqualifying error here, the way a comment on the
   wrong line is for review. Enforce it the way `VerifiedFinding` does: a `pass` or
   `fail` cannot be constructed except by the verifier, from an oracle record. Test that.
2. **XCTest's own pass/fail is not the verdict.** The harness reads records, as the
   audit driver already does. A navigation failure is not a criterion failure.

One deliberate difference from D8: `not_verifiable` criteria are reported by ID and
reason, not just counted. They are the author's own criteria, not unverified claims of
ours, and the author needs to know which ones nothing checked. Say so in the ADR.

### Report

`acceptance-<run_id>.json`, versioned, written on every run including failed ones:
`spec_id`, `provenance`, `head_sha` (from the checkout, if it is a git repo), device
context, and per criterion: `id`, `verdict`, `reason`, each expectation with `expected`,
`actual`, and `met`, evidence refs, and duration. Plus a summary block with counts by
verdict and by `not_verifiable` reason. Other tools will ingest this file keyed on
criterion ID, so treat its shape as a public contract.

Also write a trajectory for the run, and a markdown table for `--summary-file`.

## Traps

- **Criteria text ends up in generated Swift.** Much of it will be written by a model.
  `"`, `\`, a newline, or `\(` in a label must not break the build or get evaluated as
  interpolation. My lean: the driver is a fixed interpreter and the criteria travel as
  one encoded payload, with one generated test method per criterion so `-only-testing:`
  still works. Prove it with hostile strings.
- **Observed values are arbitrary app text.** The audit record is `|`-delimited inside
  an activity title. A label containing `|`, a newline, or emoji must round-trip. Pick
  an encoding or use an attachment, and test the round trip.
- **Shared identifiers.** In the fixture, `cart.removeItem` is on three rows.
  `firstMatch` would quietly pick one. More than one match is `ambiguous_element`, not a
  guess.
- **Locale.** `"$441.00"` depends on the simulator's region. Pin language and locale at
  launch, and record them in the evidence context.
- **`prepare_workspace` is welded to the audit driver.** It calls `render_driver`
  directly and raises when there are no surfaces. Generalize it without changing audit
  behaviour; `tests/test_injection.py` is the guard.
- **`_SCREENSHOT_NAME` stops at `_`.** Criterion IDs contain `.` and `-`, and two IDs
  can sanitize to the same Swift method name. Constrain the ID pattern and make lint
  reject collisions.
- **Never sleep.** Bounded polling only. Whether a timeout means "observed false" or
  "never got there" depends on the phase, and the record has to say which.
- **The xcresult schema is pinned** (`SCHEMA_VERSION` in `xcresult.py`), and the CI
  runner's Xcode is older than mine. Parse against JSON captured from a real run and
  commit it under `tests/fixtures/xcresult/`, as the audit parser does.
- **Do not invent Apple APIs.** Verify every `XCUIElement` attribute, and how launch
  arguments and environment reach the app, against the SDK headers before writing code
  against them.
- **Ground truth is observed, not intended.** I worked out `"Subtotal $441.00"` by
  reading `Model.swift` and `CheckoutSummaryView.swift`. Run it before you trust it.
- **Run output is not source.** `.sightline/eval/` is not gitignored, and about 6,700
  files of eval scratch output, mostly DerivedData, are committed. Put acceptance output
  somewhere ignored. Ask me before touching the existing files; if I say yes, untrack
  them in a separate first commit.

## What I want from this session, in order

1. **Read and report.** Confirm or correct this brief against the code.
2. **ADR-0004**, using the template in `docs/adr/README.md`, status Proposed. It must
   cover: why this is a mode and not a skill; the criteria schema; the verdict rules;
   the record encoding; the D8 difference; alternatives considered (including an agent
   driving the simulator through an MCP server, and why that loses here); and open
   questions.
3. **The schemas as code**, with tests: criteria, verdict, report. Plus the linter.
   **Stop here and show me.** This is the contract everything else hangs off.
4. **Build, each step independently testable:**
   1. Driver rendering. Linux-testable, including the hostile-string cases.
   2. Injection generalized to accept the new driver.
   3. Record parsing against captured real `.xcresult` JSON.
   4. Verdict derivation as a pure function with a table test covering every reason.
   5. `sightline accept --criteria <file> --path <checkout> --udid <udid>`, with
      `--content-size`, `--appearance`, and `--out` matching `audit`, and
      `--summary-file` matching `review`. Plus a lint command; name it to fit the
      existing CLI.
   6. An Xcode-gated integration test using the same `SIGHTLINE_INTEGRATION` and
      `SIGHTLINE_SIM_UDID` switches as `tests/test_injection.py`.
5. **Acceptance corpus** under `eval/acceptance/`, separate from `eval/corpus/` so the
   review score keeps its meaning. Each case is criteria, expected verdicts with
   reasons, and an optional patch. At minimum:
   - a criterion the unmodified fixture satisfies, expected `pass`
   - the same criterion against a patch that breaks it (subtotal ignoring quantity
     should read `$377.00`), expected `fail` with the actual value recorded
   - `manual`, unsupported evidence, and an unreachable step, each expected
     `not_verifiable` with the right reason
   - one case that exercises a `fixture`. Add the launch-argument handling in that
     case's patch, not in the base fixture.

   Score it as a verdict confusion matrix. Any false pass or false fail exits non-zero.
   The three existing cases must still apply and still score precision 1.00, recall
   1.00.
6. **Docs.** ADR index, `check-catalog.md`, `roadmap.md`, `decisions.md` (next is D13),
   `open-questions.md`, `eval/README.md`, and a README section. The README status line
   still says "No harness yet"; fix it while you're there.

## Decisions that are mine. Ask, with your recommendation.

1. **App crash mid-criterion.** `fail`, or `not_verifiable` with the crash report as
   evidence and surfaced at the top of the report? I lean the second, since a crash is
   not the `then` being observed false, but I can be argued out of it.
2. **Exit code.** Sightline never blocks a merge, but an agent loop wants a signal. I
   lean: exit 0 by default, and `--strict` exits non-zero on any `fail`, never on
   `not_verifiable`. What should `--strict` do when the build fails?
3. **Unreachable step.** If the `when` names a control that does not exist in the build,
   is that `not_verifiable(step_unreachable)` or a `fail`? I lean `not_verifiable`,
   reported loudly, because a wrong identifier in the criteria looks identical.

## Definition of done

- `sightline accept` against `eval/fixtures/CheckoutDemo` with the example criteria above
  produces: AC1 `pass`, AC2 `pass`, AC3 `not_verifiable(evidence_type_unsupported)`,
  AC4 `not_verifiable(manual)`, each `pass` with a screenshot and an oracle record, and
  a report I can read.
- The same run against the broken-subtotal patch turns AC1 into `fail`, with expected
  and actual both in the report.
- The linter rejects a `ui_state` criterion with no expectation and no `manual` flag, an
  unknown step, an unknown fixture or surface, and colliding IDs, each with a message
  that names the fix.
- `uv run pytest tests/ -q`, `ruff check`, and `ruff format --check` pass on Linux.
  `sightline eval` still scores the review corpus at 1.00 / 1.00, and the acceptance
  corpus has zero false passes and zero false fails.
- ADR-0004 tells me why it is shaped this way.

## How to work with me

- Plan mode first. Show me the plan before you build.
- Work on a branch. Small commits with real messages. Do not push, open a PR, or post
  anything to GitHub unless I say so.
- The runtime steps need Xcode 26 and a simulator. The fixture README shows how to
  create the device; ask me for the UDID if you can't list it.
- Ask when a decision is mine. Don't ask permission for obvious engineering choices.
- If something here is wrong, say so directly. If you think deterministic execution of
  structured criteria is the wrong v1, and that the tier is useless without a model
  translating prose, make that case with examples before you build.

---

# Addendum, 2026-10-11

Everything above stands, except where this section changes it. Where they conflict,
this section wins.

## The workflow this pass has to deliver

1. I invoke a Claude Code skill for a spec's acceptance criteria.
2. If I haven't given it criteria, it asks me for them.
3. Log lines that track the criteria are added to the app.
4. Sightline builds the app, drives the simulator, and reports whether each criterion is met.

Sightline itself stays `model_tier: none` and costs $0. The skill is the "upstream
agent" that the scope above already allows to author criteria. It is a separate layer,
and Sightline does not trust it: everything the skill produces goes through lint, and
every verdict still comes only from `.xcresult` records.

## Where the brief is wrong about the code (checked 2026-10-11)

- **Decision numbers.** D13–D15 are taken (Nearby Meows, driver-dismissed alerts, the
  `SIGHTLINE-AUDITED` marker). The next decision is **D16**.
- **The `.sightline/eval/` trap is resolved.** Commit `9fb3170` stopped tracking it and
  `.gitignore` covers it. Drop the "ask me before touching it" step. Acceptance output
  goes under `.sightline/run/`, which is also ignored.
- **Xcode is 27.1 on the dev machine**, not 26. `xcresult.py` pins schema `0.1.0`, and
  `xcresulttool` now defaults to `0.4.0`. Captured fixtures and the pin must agree with
  whatever the CI runner ships, so check both.
- **The driver already does things the brief doesn't mention.** It dismisses system
  alerts from configured titles (D14). It writes a completion marker after the audit
  returns (D15), which is the model for the per-phase records criteria need: a missing
  marker means "never got there", not "observed false". The acceptance driver must keep
  alert dismissal; Nearby Meows needs it.
- **`Surface` already carries `launch_arguments`.** The proposed `fixtures:` map is
  per-criterion state on top of per-surface navigation. Keep both and say so in the ADR,
  or merge them; do not leave two mechanisms that do the same job undocumented.
- **The shared-identifier trap is live.** `tap()` in `driver_template.py` uses
  `firstMatch`.
- **Target #2 exists.** Nearby Meows (`eval/nearby-meows/`) is a real app with its own
  config. CheckoutDemo stays the corpus target. Running `accept` once against Nearby
  Meows by hand is the "survives a real app" check, not a corpus case.

## Change 1: `log_event` evidence moves from Out to In

`network_call` and `persisted_state` stay out and still resolve to
`not_verifiable(evidence_type_unsupported)`.

### Where the log lines come from

**They live in the app, committed, as ordinary code I review.** The skill proposes them
as an edit and I accept or reject it. Sightline never writes app source. The scratch
clone gets only the UI test driver, as today. What the test runs is what ships.

What I accept by choosing this: a log line proves the line executed, not that the
behaviour happened. A criterion backed only by logs is as good as the instrumentation.
The report must show the evidence type next to every verdict, so a log-backed `pass` is
never mistaken for an observed-UI `pass`.

### Convention (proposal; final shape in ADR-0004)

```swift
let ac = Logger(subsystem: "<config: acceptance.log_subsystem>", category: "acceptance")
ac.notice("SIGHTLINE-AC|order_submitted|order_id=\(id, privacy: .public)")
```

- The fixed prefix `SIGHTLINE-AC|` and a closed event-name pattern
  (`[a-z][a-z0-9_]*`) make lines unambiguous inside a large stderr file.
- Every interpolated value is `privacy: .public`, or it shows up as `<private>`
  (verify; see below).
- The criteria file references events by name only. It never contains Swift.

### Criteria shape

```yaml
  - id: CHK-7.AC3
    given: an order was placed
    when: the confirmation screen appears
    then: the order was submitted exactly once
    surface: PaymentMethod
    steps:
      - tap: payment.placeOrder
      - wait_for: confirmation.viewHistory
    evidence: log_event
    expect_log:
      - event: order_submitted
        count: 1              # exact. Also: min_count; count: 0 for "never happens"
```

A criterion may carry both `expect` and `expect_log`. It passes only if every
expectation of both kinds is observed true.

### Verdict rules specific to logs

- **Proving absence.** `count: 0`, or a `fail` for "expected 1, saw 0", is valid only if
  the capture is proven live for that launch: another `SIGHTLINE-AC|` line, or a launch
  marker, was observed in the same process's output. Otherwise the verdict is
  `not_verifiable(log_unavailable)`, a new reason. Without this rule, broken capture
  reads as "the event never happened", which is a false pass on every `count: 0`
  criterion.
- **Expected vs. actual.** A log `fail` records both the expected and observed count,
  and the matching lines verbatim, as the actual value.
- **Timing.** Lines count only if they belong to this criterion's app launch and come
  after its steps start. Attribution is by process, not by timestamp guessing (see
  item 3 below).

### Mechanism: verified so far, and what to verify first

**Verified 2026-10-11 on Xcode 27.1:**
- `xcresulttool export diagnostics` on a CheckoutDemo bundle produces
  `StandardOutputAndStandardError-<bundle id>.txt`. That is the app process's
  stdout/stderr, headed `process ID <pid> beginning at <timestamp>`, with `os_log`
  output mirrored into it (XCTest's in-app agent lines appear as `[Default]`).
- `xcresulttool get log --type console` returns "No console log available" for a test
  run. Don't build on it.

**Verify before writing code against it. Each one changes the design:**

1. A `Logger` call from app code, at `.debug`, `.info`, `.notice`, and `.error`, appears
   in that file when the app is launched by XCUITest with no debugger attached. If it
   doesn't, the fallback is
   `simctl spawn <udid> log show --style ndjson --predicate ...` over the run window.
   That is evidence from outside the `.xcresult` and needs its own section in ADR-0004,
   because it breaks "derived only from records in the `.xcresult`".
2. Whether non-`.public` interpolations come through as `<private>`.
3. Whether each `app.launch()` (one per criterion method) gets its own file, and how a
   file is tied to a test method: the PID in the header plus a driver activity
   recording the launch.
4. Whether output up to a crash survives when the app crashes mid-criterion. This
   bears on mine-to-decide #1.

Put a captured diagnostics directory under `tests/fixtures/xcresult/` and parse against
it, the same way the audit parser does.

### Changes to the definition of done

- **AC3 in the example criteria becomes `log_event` with `expect_log` and must return
  `pass`.** That requires a patch adding the `Logger` line. Put the patch in an
  acceptance-corpus case, not the base fixture, the same way the `fixture` case works.
- Add corpus cases:
  - a double-submit patch, expected `fail` with actual count 2;
  - the log line removed, expected `fail` with actual count 0 (capture is proven live by
    a second event);
  - capture not live, expected `not_verifiable(log_unavailable)`.

## Change 2: the Claude Code skill (new)

It's a skill for Claude Code, not a Sightline check skill (ADR-0001). It wraps
`sightline accept`. Name it in the plan; `sightline-accept` is the working name.

**It ships with this repo**, so anyone using Sightline gets it, and can be installed
into `~/.claude/skills/` for use against other apps. Propose the install mechanism in
the plan.

**Flow:**

1. **Get criteria.** Use a criteria file or spec path I passed. Otherwise ask me for the
   given/when/then criteria in prose. Don't proceed without them.
2. **Resolve against the app.** Read config `surfaces:` and the app's views to find real
   accessibility identifiers. **Never invent one.** A criterion the skill can't ground
   in source is reported to me with what's missing, such as "no identifier on the Place
   Order button". It is not written with a guess. A missing identifier is also a
   legitimate edit to propose, under the same rules as log lines.
3. **Propose instrumentation.** For criteria whose `then` isn't observable on screen
   ("exactly once", "was saved", "was sent"), propose `SIGHTLINE-AC|` log lines as a
   diff and wait for my approval. Never commit. Never edit app code I haven't approved.
4. **Write the criteria YAML, then lint it.** Loop on lint errors. If the fix the
   linter names needs a vocabulary extension, stop and tell me; don't work around it.
5. **Run `sightline accept`.** Use the configured simulator, or ask me for a UDID.
6. **Report.** Summarize from the JSON report only. Every `not_verifiable` is listed by
   ID and reason. The skill never upgrades one to pass or fail, and never re-reads
   screenshots to second-guess a verdict.

**This doesn't contradict the "Out" list.** Turning prose into steps still happens
outside Sightline. `PROMPT.md`'s "no LLM-generated Swift refactors" still holds: log
lines and accessibility identifiers are additive instrumentation I approve one diff at a
time, not refactors.

**Testing:** the skill is prose, not code. Test it by running it once end to end
against CheckoutDemo, with the AC3 instrumentation as the proposed edit, and record the
transcript outcome in the PR description. Its correctness rests on lint and `accept`,
which are tested.

## Changes to "What I want from this session"

- Step 2 (ADR-0004) also covers:
  - log evidence and the liveness rule;
  - instrumentation living in the app, and the trust it implies;
  - the skill/Sightline boundary;
  - the `fixtures:` / `launch_arguments` relationship.
- Step 3 (schemas) includes `expect_log`, the `log_unavailable` reason, and the
  evidence type on every verdict in the report.
- Step 4 gains:
  - **4.0, before anything else:** run verification items 1–4 above on a throwaway patch
    to CheckoutDemo, and write the results into `docs/` like the 2026-08-31
    verification pass;
  - **4.4b:** log-record parsing and log verdicts, table-tested;
  - **4.7:** the Claude Code skill, last, once `accept` works end to end.
- Step 6 (docs): `decisions.md` starts at **D16**. The README section shows the skill
  workflow first and the raw CLI second.

## Added to "Decisions that are mine"

4. **Log convention.** Fixed `SIGHTLINE-AC|` prefix with a configurable subsystem (my
   lean), or fully configurable? A fixed prefix keeps lint and the parser simple and
   makes the lines easy to grep out of a real app later.
5. **Commit identity for approved instrumentation.** Should the skill commit approved
   log lines on the current branch, or leave them staged for me to commit? I lean
   toward leaving them staged.
