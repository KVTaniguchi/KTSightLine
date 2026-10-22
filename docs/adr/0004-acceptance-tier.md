# ADR-0004: The acceptance tier

- **Status:** Proposed
- **Date:** 2026-10-11
- **Deciders:** Kevin Taniguchi

> Brief: [`docs/briefs/0002-acceptance-tier.md`](../briefs/0002-acceptance-tier.md),
> including its 2026-10-11 addendum. Premises verified in
> [`docs/verification-2026-10-11.md`](../verification-2026-10-11.md), cited below as
> V1–V7.

## Context

Sightline reviews a diff for defects nobody asked about. Spec-driven development needs
the opposite question answered: does the running app do what the spec *promised*?

The specs already carry given/when/then acceptance criteria, and nothing executes them.
So "the agent says it's done" and "the criteria hold in a running app" are currently
the same claim. A tool that separates them is:
- the fastest quality signal for whoever tunes the agent harness (first-attempt pass
  rate per criterion, minutes after generation);
- a checkpoint an agent loop can run in a worktree before a PR exists.

What breaks if we build it carelessly:

- **A false pass is the disqualifying error.** It's the equivalent of a review comment
  on the wrong line. One false pass teaches everyone that "pass" means nothing.
- **Criteria are often written by a model.** Their text will contain `"`, `\`,
  newlines, `\(` and emoji. They travel into generated Swift and back out through the
  `.xcresult`.
- **The runtime is non-deterministic.** Shared identifiers, locale-dependent formatting
  (V6), crashes (V4), slow screens. Each has to resolve to a stated verdict, not a
  guess.
- **Some promises aren't visible on screen.** "Submitted exactly once" is the canonical
  one. The tier needs a second evidence type, and that evidence comes from the app
  under test.

## Decision

### 1. A mode, not a skill

The acceptance tier is a separate command, `sightline accept`, not a check skill in the
ADR-0001 sense. A skill is the wrong shape on every axis:

| | Check skill (ADR-0001) | Acceptance |
|---|---|---|
| Input | a diff, routed by triggers | a criteria file, chosen by its author |
| Output | `Finding`, only when something is wrong | a verdict for **every** criterion, including `pass` |
| Gate | falsifiability: post or suppress | verdict derivation: pass / fail / not_verifiable |
| Destination | a forge comment | a local JSON report; no forge |
| Cost | model tier per skill | always `model_tier: none`, $0 |

Making it a skill would mean a `Finding` that represents success, and a dispatch layer
that runs things it was told to run instead of things the diff reaches. Both corrupt
contracts that already work.

What it **does** share with the runtime tier:
- workspace injection (§6);
- simulator preparation (`Simulator.prepare`);
- the build (`XcodeBuild.test`);
- the `.xcresult` adapter (`xcresult.py` stays the only module that knows the JSON
  shape);
- the evidence store, with redaction;
- the trajectory.

`sightline accept` needs no PR and no forge. It runs on a local checkout or worktree.

### 2. The criteria file

```yaml
acceptance_schema: 1
spec_id: CHK-7
provenance:              # optional; opaque strings copied into the report untouched
  harness_version: 3f9c2e1
criteria:
  - id: CHK-7.AC1
    given: a cart holding the sample order
    when: the shopper continues to checkout
    then: the subtotal covers every line item times its quantity
    surface: CheckoutSummary          # key into config `surfaces:` (navigation reused)
    fixture: sample_cart              # optional; key into config `fixtures:`
    steps:                            # optional; closed vocabulary
      - tap: checkout.payment
    expect:                           # ui_state observations; closed vocabulary
      - id: checkout.subtotal
        label: "Subtotal $441.00"
    expect_log:                       # log_event observations (§5)
      - event: order_submitted
        count: 1
    evidence: ui_state                # ui_state | log_event | network_call | persisted_state
    manual: false
    reason: ""                        # required when manual: true
```

- **Prose is never parsed.** `given`/`when`/`then` are for the human reader and the
  report. `surface`, `fixture`, `steps`, `expect` and `expect_log` are what executes.
  Turning prose into steps happens upstream: a person, or the Claude Code skill in §9.
- **`extra="forbid"` everywhere.** A typo'd key that silently means "never checked" is
  the failure that makes people stop trusting the tool.
- **Closed vocabularies, versioned in `runners/`, in the sense of ADR-0001 §3.**
  - Steps: `tap: <id>`, `type: {into: <id>, text: <str>}`, `wait_for: <id>`.
  - `expect` attributes: `exists`, `label`, `label_contains`, `value`, `enabled`.
  - `expect_log`: `event` plus exactly one of `count` (exact) or `min_count`.

  A criterion needing anything else fails lint with a message naming the missing word.
  That name is the ticket title.
- **Criterion IDs** match `^[A-Za-z][A-Za-z0-9]*(?:[.-][A-Za-z0-9]+)*$`. Each becomes a
  Swift method name by mapping `.` and `-` to `_`. Lint rejects two IDs that map to the
  same name.
- **`evidence`** defaults to `ui_state`. Rules:
  - `ui_state` needs at least one `expect`, or `manual: true`.
  - `log_event` needs at least one `expect_log`.
  - `network_call` and `persisted_state` parse, have no executor in v1, and lint warns
    that they'll come back `not_verifiable(evidence_type_unsupported)`.
  - A criterion can carry both `expect` and `expect_log`. It passes only if every
    observation of both kinds holds.

**Fixtures vs. surface launch arguments.**
- `Surface.launch_arguments` (existing) is *navigation*: how to reach a screen.
- `fixtures: {name: {launch_arguments, launch_environment}}` (new) is *per-criterion
  app state*: an empty cart, a logged-out user.

We keep both. The driver passes locale pins, then surface arguments, then fixture
arguments. Lint rejects `-AppleLanguages`/`-AppleLocale` in either, because the locale
pin (§4) must not be overridden by accident. Sightline doesn't know what a fixture
means; honouring it is the app's job.

### 3. Verdicts

| Verdict | Requires |
|---|---|
| `pass` | Navigation and every step completed, and every observation was recorded **true** in the `.xcresult` |
| `fail` | Navigation and every step completed, and at least one observation was recorded **false**, with `expected` and `actual` |
| `not_verifiable` | Anything else, with exactly one reason from a closed set |

Reasons:

| Reason | Meaning |
|---|---|
| `manual` | The author marked it manual |
| `evidence_type_unsupported` | `network_call` / `persisted_state` (no executor in v1) |
| `no_observable` | An observation's element wasn't present, and the observation didn't assert `exists` (see below) |
| `surface_unreachable` | Configured navigation to the surface did not complete |
| `step_unreachable` | A step's element never appeared or never became hittable |
| `ambiguous_element` | An identifier in a step or observation matched more than one element |
| `app_crashed` | The app crashed during the criterion |
| `log_unavailable` | Log capture can't be proven live for this criterion's launch (§5) |
| `build_failed` | The build failed before any test ran |
| `runner_unavailable` | Injection, simulator or project setup failed |
| `no_record` | The driver left no record for an observation (it didn't run, or the record didn't survive) |

Rules that matter more than the table:

1. **Ambiguity never resolves to pass or fail.** This is ADR-0002's gate applied to
   verdicts. `CriterionResult` with `pass` or `fail` can be built only by
   `core/acceptance/derive.py`, which holds a private construction token: the
   `_GATE_TOKEN` / `VerifiedFinding` pattern. `not_verifiable` is freely constructible,
   because it claims nothing. A test asserts that direct construction of pass/fail
   raises.
2. **XCTest's own pass/fail isn't the verdict.** The driver never asserts. The harness
   reads records, as the audit driver already does.
3. **Phases decide what a timeout means.** A timeout during navigation or a step is
   "never got there" (`surface_unreachable` / `step_unreachable`). A timeout while
   waiting for an attribute to match is an *observation*: the final observed value
   becomes `actual`.
4. **A missing element is a fail only if the author asserted existence.**
   - `{id: X, exists: true}` with X absent → `fail` (actual `exists: false`).
   - `{id: X, label: "…"}` with X absent → `no_observable`. A wrong identifier in the
     criteria looks identical to a missing element, the same reasoning as decision 3
     (Kevin, 2026-10-11).

   Lint warns on an observation with no `exists` attribute, saying which verdict a
   missing element will produce.
5. **Crashes are `not_verifiable(app_crashed)`, never `fail`** (Kevin, 2026-10-11).
   - A crash isn't the `then` observed false.
   - Crashed criteria are listed **first** in the report.
   - The crash log attachment (V4) is the evidence.
6. **Unreachable steps are `not_verifiable(step_unreachable)`** (Kevin, 2026-10-11).
   They're reported with the identifier that wasn't found, in the summary as well as
   the detail.

Derivation order, first match wins:
1. `manual`;
2. `evidence_type_unsupported`;
3. `build_failed` / `runner_unavailable`;
4. `app_crashed`;
5. `surface_unreachable`;
6. `step_unreachable` / `ambiguous_element` (steps, in order);
7. per observation: `ambiguous_element`, then `no_observable`, then `no_record`, then
   `log_unavailable`;
8. any observation false → `fail`;
9. otherwise `pass`.

Earlier reasons dominate later ones, so one crash never also reports a confusing
`no_record`.

### 4. The driver: a fixed interpreter and one encoded payload

The generated `SightlineAcceptance.swift` is a **fixed interpreter**. It contains:
- no criterion text in Swift source;
- one string literal holding the whole criteria payload as **base64-encoded JSON**
  (base64's alphabet can't close a string literal or start a `\(` interpolation);
- one generated method per criterion, `test_AC_<sanitized id>()`, that calls
  `run(index:)`, so `-only-testing:` still selects one criterion.

Per criterion, the interpreter:
1. Launches with `-AppleLanguages (<lang>) -AppleLocale <locale>` (config; default `en` /
   `en_US`), then the surface args, then the fixture args and environment. V6: `fr_FR`
   renders `$441.00` as `US$441,00`.
2. Dismisses system alerts from configured titles (D14), as the audit driver does.
3. Navigates the surface's taps and `wait_for`, then runs each step.
   - Every identifier resolves through `matching(identifier:)`. A `count > 1` is
     recorded as ambiguous and stops the criterion. **Never `firstMatch`**: on Cart,
     `cart.removeItem` matches three rows (V5).
   - Taps scroll to hittable, with a bounded number of swipes, as the audit driver
     does.
4. Evaluates observations with **bounded waits only**: `waitForExistence(timeout:)`,
   `waitForNonExistence(timeout:)` (present in Xcode 27.1, V5), and
   `XCTNSPredicateExpectation` for attribute matches. **No sleeps.**
5. Captures a screenshot attachment named `SIGHTLINE-ACC-SHOT-<index>`, by index
   because IDs contain `.` and `-` and `_SCREENSHOT_NAME` stops at `_`.
6. Writes a final `done` record.

**Record encoding.** Every record is an activity titled
`SIGHTLINE-ACC|<base64url(JSON)>`:

```json
{"v":1,"i":0,"id":"CHK-7.AC1","phase":"expect","n":0,
 "expected":{"label":"Subtotal $441.00"},"actual":{"label":"Subtotal $377.00"},"met":false}
```

Phases: `launched`, `surface` (`ok` / `unreachable` / `ambiguous`), `step` (`ok` /
`unreachable` / `ambiguous`), `expect`, `shot`, `done`.

Activity titles do round-trip `|`, newlines and emoji (verification, "Also confirmed in passing"), but the parser should
never split observed app text on a delimiter. Base64url makes that structural rather
than tested-for.

### 5. Log evidence

**The lines live in the app, committed, written by its authors** (Kevin, 2026-10-11).
The Claude Code skill may *propose* them as a diff; a human accepts it. Sightline never
writes app source. The scratch clone gets only the UI test driver, as today. What the
test runs is what ships.

**Convention.** Fixed prefix, configurable subsystem (Kevin, 2026-10-11):

```swift
let acceptanceLog = Logger(subsystem: "<acceptance.log_subsystem>", category: "acceptance")
acceptanceLog.notice("SIGHTLINE-AC|order_submitted|order_id=\(id)")
```

- **`.notice`, not `.debug`.** `.debug` never reaches the result bundle (V1). A
  `.debug` line would read as "observed zero", a false fail. The level is the one
  convention rule Sightline can't lint, because it lives in Swift. The skill enforces it
  and the README states it.
- **Event names** match `^[a-z][a-z0-9_]*$`. Anything after the second `|` is opaque
  payload, kept verbatim as the `actual` lines.

**Mechanism.**
- `xcresulttool export diagnostics` yields `StandardOutputAndStandardError-<bundle
  id>.txt` (V1). It's **one file for the whole test run**, every launch interleaved
  (V3).
- XCUIApplication exposes no PID (V5). So lines are **attributed by time window, then
  confirmed by PID**:
  - The window runs from the criterion's `Launch <bundle id>` activity `startTime` to
    its `done` record. If there's no `done`, the window ends at the test's `Tear Down`.
    Only lines after the first step's record count, so app-start lines can't satisfy
    "after they place the order".
  - **Liveness:** the window must contain unified-log lines from **exactly one PID**.
    XCTest's in-process agent always logs from the app's PID at launch (V3), so a live
    capture always has one. Zero or several PIDs → `not_verifiable(log_unavailable)`.
    This is what lets `count: 0` and "expected 1, saw 0" be real verdicts instead of
    broken-capture artifacts.
  - The match is `count` exact or `≥ min_count`. A `fail` records the expected count,
    the actual count, and the matching lines.
- **Evidence:**
  - only the attributed `SIGHTLINE-AC|` lines, after `redact_text`, stored as a
    `console_log` artifact;
  - never the whole file: it holds about 67,000 lines of XCTest chatter per run, and
    `.private` interpolations arrive in clear text (V2).

**The trust this implies, stated plainly.** A log line proves the line executed, not
that the behaviour happened. A log-backed `pass` is exactly as good as the
instrumentation. So every verdict in the report carries its evidence type, and the
markdown summary labels log-backed verdicts. A log-backed `pass` must never be
mistaken for an observed-UI `pass`.

### 6. Injection, generalized

`prepare_workspace` in `runners/xcode/injection.py` is welded to the audit driver. It
calls `render_driver` directly and raises when there are no surfaces. We split it:
- `_inject(source_root, …, driver: DriverSpec)` holds the clone, target resolution,
  file write, and scheme. `DriverSpec` carries filename, class name, source and method
  names.
- `prepare_workspace(…)` keeps its signature and behaviour exactly, including the
  no-surfaces error. `tests/test_injection.py` is the unchanged guard.
- `prepare_acceptance_workspace(…)` renders the acceptance driver and calls `_inject`.

The same scratch-clone guarantee (ADR-0003 §5) holds: the checkout is never modified.

### 7. The report is a public contract

`.sightline/run/accept/<run_id>/acceptance-<run_id>.json` is written on **every** run,
including build failure and an unavailable runner, with `report_schema: 1`. It holds:
- `spec_id`, `provenance` (copied untouched, never interpreted), `run_id`;
- `head_sha` (from the checkout, if it's a git repo; empty otherwise);
- `context`: device, OS, locale, language, appearance, content size, Xcode and
  `xcresulttool` versions;
- `criteria[]`: `id`, `given`/`when`/`then`, `evidence`, `verdict`, `reason`,
  `detail`, `observations[]` (`kind`, `target`, `expected`, `actual`, `met`), evidence
  refs, `duration_s`;
- `summary`: counts by verdict and by `not_verifiable` reason, plus `crashed` and
  `unreachable` lists with IDs.

Other tools ingest this keyed on criterion ID. Adding a field is a minor change;
renaming or removing one bumps `report_schema`.

A trajectory (skill trace `acceptance`) and an optional markdown table
(`--summary-file`) are written alongside.

**The D8 difference, deliberately.** D8 shows suppressed findings as counts only,
because their text is our unverified claim. `not_verifiable` criteria are the
opposite: the **author's own** criteria, which nothing checked. Hiding them behind a
count would let "3 not verifiable" pass as a clean run. Each one is reported by ID and
reason.

### 8. Exit codes (Kevin, 2026-10-11)

- **Default:** exit 0. Sightline never blocks.
- **`--strict`:**
  - **1** if any criterion is `fail`;
  - **2** if the build failed or no criterion could run (every result is
    `build_failed` / `runner_unavailable`). An agent loop must not read "didn't build"
    as "all clear";
  - never non-zero for `not_verifiable` alone.
- `sightline lint` exits 1 on any lint error.

### 9. The skill/Sightline boundary

A Claude Code skill, `integrations/claude-code/sightline-accept/`, is the
conversational front end:
- asks for the AC;
- grounds them in real accessibility identifiers, never inventing one;
- proposes `SIGHTLINE-AC|` lines and missing identifiers as diffs, left staged and never
  committed;
- writes the criteria file and loops on `sightline lint`;
- runs `sightline accept` and summarizes the JSON report.

It's the "upstream agent" the brief allows to author criteria. Sightline doesn't trust
it:
- every criterion it writes goes through lint;
- every verdict still comes only from `.xcresult` records;
- the skill may not upgrade a `not_verifiable` or re-read screenshots to second-guess
  a verdict.

`sightline/adapters/model/` stays empty. Sightline makes no model call.

## Alternatives considered

**An agent driving the simulator through an MCP server** (XcodeBuildMCP,
ios-simulator-mcp). It loses on three counts:
- D4 already rejected the dependency for the runtime tier, for policy reasons that
  apply equally here.
- An agent that taps and then judges what it saw is both actor and verifier. The
  record of what was observed would be the agent's description, which is exactly the
  "evidence-decorated, not verified" failure ADR-0002 exists to prevent.
- It isn't deterministic. The same criteria on the same build must give the same
  verdict, or the harness-tuning signal is noise.

It wins on flexibility: it could handle criteria outside the closed vocabulary. The
answer to that is growing the vocabulary one named word at a time.

**Translating prose into steps inside Sightline**, with a model call. This lost on the
same actor/verifier grounds, plus `model_tier: none`. It moved to the skill (§9),
upstream of lint, where its output is a reviewable file instead of an invisible step.

**XCTest assertions as the verdict.** `XCTAssertEqual` in generated code, reading
pass/fail from the test result. It lost because XCTest can't tell "observed false" from
"never got there": a navigation timeout and a wrong subtotal both fail the test. The
three-way verdict needs phase records.

**Per-criterion generated Swift with escaped literals.** It works until a model writes
`\(`. Every escaping rule is a bug waiting for a string nobody tested. One payload in a
fixed alphabet removes the class.

**`simctl spawn <udid> log show` over the run window** for log evidence. It's a viable
fallback, kept in open questions. It lost because the result bundle already carries the
app's output (V1), and keeping every verdict inside one `.xcresult` keeps "derived only
from records in the bundle" true without exception.

**A launch-token heartbeat in app code for log liveness.** It would need app changes
for every adopter. The XCTest agent's own lines from the app PID give the same proof for
free (V3). If a future Xcode stops emitting them, liveness fails closed
(`log_unavailable`), not open.

## Consequences

**Good.**
- "The agent says it's done" and "the criteria hold" become separate, cheap claims.
  $0 in model cost; one build and one simulator run.
- A false pass needs a recorded `met: true` from the interpreter. Anything ambiguous,
  missing or crashed is `not_verifiable`, by construction and by test.
- The criteria file and the report are stable, versioned contracts other tools can
  build on.

**Bad.**
- The closed vocabulary will feel small at first. Every missing word is a lint error
  until someone adds it to `runners/`.
- Log-backed verdicts trust the instrumentation. That's documented, labelled in the
  report, and not solved.
- Log attribution depends on serial execution and a shared clock. Parallel testing would
  break it, and the acceptance scheme never enables it.
- Liveness depends on XCTest's in-process agent logging from the app PID, which is
  undocumented behaviour. It fails closed if that changes, but every `log_event`
  criterion would then go `not_verifiable` until fixed.
- Rule 4 (absent element without `exists` → `no_observable`) means a genuinely missing
  feature reads as not-verifiable unless the author asserted `exists: true`. Lint warns;
  authors will still miss it sometimes.

## Open questions

- **Executors for `network_call` and `persisted_state`.** `network_call` probably wants
  an app-side URLProtocol hook, which raises the same instrumentation-trust question as
  logs. Owner: Kevin.
- **Device, appearance and Dynamic Type matrices for acceptance runs.** One simulator,
  one configuration in v1. Owner: Kevin.
- **Posting verdicts to a PR.** Needs a new `ForgeAdapter` method and Kevin's
  authorization (D12). Owner: Kevin.
- **Discovering criteria** from a PR body or commit trailers, and wiring into
  `sightline review` / `.sightline/config.yml`. Owner: Kevin.
- **Fallback log mechanism** (`simctl spawn … log show`) if a future Xcode drops the
  stderr mirror. Owner: whoever hits it first.
- **Whether CI's Xcode (26.4.1) emits the same diagnostics layout as 27.1.** Unverified;
  V7 notes it. Owner: first CI run.
