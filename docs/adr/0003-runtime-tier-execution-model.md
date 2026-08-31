# ADR-0003: Runtime tier execution model

- **Status:** Proposed
- **Date:** 2026-08-31
- **Deciders:** Kevin Taniguchi

> **Verified 2026-08-31** — see [`docs/verification-2026-08-31.md`](../verification-2026-08-31.md).
> P1's mechanism was wrong (price ratio, not a minutes multiplier) and the cost model
> below now uses real per-minute rates. P2, P3, P4 confirmed, with P3 better than
> claimed. Corrections are marked inline.

## Context

The runtime tier is the product. It is also the only part that costs real money, takes
real wall-clock time, and can flake. Everything about how it is scheduled has to be
designed on the assumption that it is expensive and unreliable, because it is.

Forces:

- macOS runners cost **$0.062/min** against Linux at **$0.006/min** — a 10.3× price
  ratio, not a minutes multiplier (P1, corrected).
- `macos-26` GA 2026-02-26; `macos-latest` migrated to it starting 2026-06-15 over 30
  days. Its default Xcode is **26.4.1** (P2).
- `xcresulttool get object` is deprecated; `get test-results summary|tests` is supported,
  the schema **is** published via `--schema`/`--schema-version`, and `xcresulttool
  compare --baseline-path` ships a native differential (P3).
- `performAccessibilityAudit(for:)` throws, is `@MainActor`, and lives on
  `XCUIApplication` in `XCUIAutomation.framework` — test targets only (P4).
- ADR-0002's `differential_*` verifiers require **base and head, same runner, same job**.
  That is not an optimization we can skip; cross-runner comparison is noise.
- Simulator boot is slow and flaky. Xcode builds are slow.
- A reviewer that blocks merges when it flakes gets removed from the org within a week.

## Decision

### 1. Two jobs, one gate

```
PR opened/pushed
  │
  ├─ job: static        (ubuntu-latest, every PR, no gate)
  │     diff parse → impact analysis → tier-0 skills → post
  │     ~1–2 min, ~$0 at the Linux rate. Never gated. Never skipped.
  │
  └─ job: runtime       (macos-26, GATED)
        admission → checkout base+head → build both → boot matrix
        → drive → capture → verify → post
```

The static job also **emits the trigger set** (ADR-0001) and writes it as a job output.
The runtime job's gate reads that output. Impact analysis therefore runs exactly once,
on cheap hardware, and the expensive job inherits its conclusions.

### 2. The gate, in order, all deterministic

1. **Path globs.** Union of `globs` across enabled runtime-tier skills. No match → skip.
   This alone eliminates the majority of PRs in a typical iOS repo (tests-only,
   strings-only, CI-config-only, backend-adjacent).
2. **Trigger intersection.** Does the emitted trigger set intersect any runtime skill's
   `triggers`? No → skip.
3. **Label override.** `sightline:full` forces the runtime job on; `sightline:skip`
   forces it off. Human override is always available and always wins, in both directions.
4. **Budget check.** Per-repo daily and per-PR run budget from `.sightline/config.yml`.
   Exhausted → skip with a summary line saying so, never silently.
5. **Cheap classifier — optional, off by default.** A cheap-tier model call that answers
   "does this diff plausibly change rendered UI?" over the *file list and hunk headers
   only*. It may only ever **narrow**, never widen: it can veto a run that the globs
   admitted, and it can never admit one they rejected. Its verdict is written to the
   trajectory with the input hash so it is auditable. Off by default because a
   nondeterministic gate makes eval scores incomparable; a repo with a big monorepo diff
   surface can turn it on.

**Fail open, always.** A gate that errors admits the run. A runtime job that fails,
times out, or cannot get a runner reports `neutral` and **never** blocks the merge. The
static job's findings still post. The PR summary says plainly what did not run and why —
"runtime tier: skipped (no runtime skill matched)" is a different message from "runtime
tier: failed (simulator boot timeout after 3 attempts)", and conflating them destroys
trust in the bot's silence.

### 3. Cost model

Rates verified 2026-08-31: macOS **$0.062/min**, Linux **$0.006/min**.

| Stage | Wall-clock (est.) | Notes |
|---|---|---|
| Runner acquisition | 0.5–3 min | Queue time; not always billed, always felt |
| Checkout + toolchain select | 1 min | |
| Build head (warm DerivedData cache) | 3–6 min | Cold cache: 12–25 min |
| Build base (warm) | 2–5 min | Only when a `differential_*` verifier is in play |
| Boot simulator matrix (3 devices, parallel) | 1–3 min | Pre-warmed; `simctl bootstatus -b` |
| Drive + capture per surface per config | 20–60 s | Multiplied by matrix size |
| Verify + model calls | 0.5–2 min | Bounded by per-skill `cost_budget_usd` |
| **Total, typical gated PR** | **10–20 min** | **$0.62 – $1.24** at $0.062/min |

The static job is **$0.006–$0.012** — free in practice, which is why it is ungated.

**A correction worth stating plainly:** runner cost does *not* dwarf model cost. At the
D6 cap of $0.50/PR, a gated PR is roughly $0.62–$1.24 of runner plus up to $0.50 of
model — the same order of magnitude, within ~2×. Both levers matter; neither one can be
ignored as noise.

Two consequences fall out immediately:

- **The base build is the single largest avoidable cost.** Mitigation: cache the *base
  branch build product* keyed on `(base_sha, build_settings_hash, Package.resolved
  hash)`. On a repo where most PRs target the same base commit, the base build is paid
  once per base commit, not once per PR. This is the highest-leverage optimization in
  the system and should be in v1, not deferred.
- **Matrix size multiplies the only thing we cannot cache** (drive + capture). Hence the
  per-skill `simulator_matrix` cap in ADR-0001 and a repo-level allowlist. The default
  matrix is **one** device (smallest supported), not three. Three is opt-in.

Hard bounds, enforced in code and in the workflow:

- `timeout-minutes` on the runtime job, defaulting to 30.
- Per-run `cost_budget_usd` for model calls; when the remaining budget cannot cover a
  skill's declared budget, the skill is not admitted (ADR-0001 stage 3) rather than
  being killed halfway.
- `concurrency: group: sightline-${{ github.ref }}, cancel-in-progress: true`. A force-push
  cancels the in-flight run. Paying twice for a superseded commit is pure waste.

### 4. Simulator lifecycle

`xcodebuild` and `simctl` are wrapped by our own thin runners rather than by
`XcodeBuildMCP` or `ios-simulator-mcp` (D4). We need roughly a dozen invocations, and
every one of them carries the retry, gating, and determinism policy below — policy an
upstream wrapper has no reason to implement our way. It also keeps a Node/MCP process
out of every CI job. Cost accepted: we own the flag surface across Xcode releases.


- **Pre-warm.** Boot the matrix in a background step that starts *concurrently with the
  build*, not after it. Boot time and build time overlap almost perfectly and there is no
  reason to serialize them.
- **`simctl bootstatus -b`** to block on readiness rather than sleeping. Sleeping is how
  you get a 15% flake rate.
- **Retries with a clean device.** Up to 3 attempts; each retry `simctl erase`s and
  re-creates the device rather than re-booting the sick one. A simulator that failed to
  boot once will usually fail again.
- **Deterministic device state**, set before every capture: fixed status bar via
  `simctl status_bar override`, animations disabled in the UI test host, fixed locale and
  timezone, pasteboard cleared, keyboard prediction off. Non-determinism here is
  indistinguishable from a real regression to the differential verifier.
- **Hard kill and teardown** in an `always()` step. A leaked simulator poisons the next
  job on a self-hosted runner.

### 5. UI test target injection

`performAccessibilityAudit` lives on `XCUIApplication` in `XCUIAutomation.framework`,
which only test targets link — confirmed (P4). It is also `@MainActor` and `throws`, so
the injected driver must be main-actor-isolated and handle the throw. Most repos will not
have a UI test target shaped the way we need.

Strategy, in order of preference:

1. **Discover.** If the repo declares `ui_test_target:` in `.sightline/config.yml`, or
   exactly one UI test target exists, use it and inject our driver as an additional
   `.swift` file plus a generated test plan.
2. **Generate into a scratch copy.** Otherwise, generate an ephemeral UI test target.
   **Never mutate the user's checkout in place** — operate on a scratch clone under the
   runner's temp dir. The repo working tree we were handed stays pristine, so a failure
   cannot leave the user's branch modified and nothing we do can be accidentally
   committed.
3. **Degrade.** If neither works (unusual project generators, Tuist/XcodeGen setups we
   cannot round-trip), the runtime tier reports `unavailable` with an actionable message
   naming the config key that would fix it. It does not guess.

Project-file manipulation is behind an adapter with implementations for `.xcodeproj`,
`Package.swift`, and (later) XcodeGen/Tuist manifests. Assume we will get this wrong on
someone's project and make the failure legible.

### 6. `.xcresult` parsing behind an adapter

Parse via `xcrun xcresulttool get test-results summary` and `... get test-results tests`.
Never `get object`, never `--legacy`.

**Pin the schema.** Every modern subcommand takes `--schema-version <major.minor.patch>`
and errors on an unknown version; `--schema` emits the JSON Schema itself. So the adapter
pins a version explicitly and a schema change is a loud error, not silent parse drift.
This matters concretely: the `macos-26` runner ships Xcode 26.4.1 while a developer
machine may be on 26.6, so "it parsed locally" proves nothing about CI.

**Use `compare` instead of writing a differential.** `xcresulttool compare <path>
--baseline-path <path>` supports `--summary`, `--test-failures`, `--tests`,
`--build-warnings`, and `--analyzer-issues`. The warning-delta check in Tier 1 and the
test-delta half of `differential_metric` are a `compare` invocation plus a threshold —
not a diffing engine we own. Also available and unplanned for: `get build-results`
(build warnings/issues) and `get test-results metrics` (performance metrics, filterable
by `--test-id`).

The adapter (`runners/xcode/xcresult.py`) is the *only* place that knows the JSON shape,
it records the `xcresulttool` version **and pinned schema version** in the trajectory,
and it is tested against a real committed `.xcresult` fixture. When Apple changes the
schema, exactly one file and one fixture change. Do not let this shape leak into
`Finding` construction.

**Stale help text, flagged so nobody chases it:** the `get object` deprecation message
recommends `xcresulttool get test-report`, which does not exist. The real subcommand is
`get test-results`.

### 7. Degradation ladder

| Condition | Behavior |
|---|---|
| No runtime skill matched | Skip; summary says "no runtime skill matched" |
| Budget exhausted | Skip; summary names the budget and when it resets |
| Runner unavailable / queue timeout | Neutral; static findings post; summary says so |
| Build fails on head | Post *that* as a finding (with the build log as evidence) — it is the most useful comment we could make |
| Build fails on base only | Run head-only skills; suppress every `differential_*` skill with reason `no_baseline` |
| Simulator boot fails after 3 retries | Neutral for runtime; static findings post |
| PR is from a fork | Skip; summary says "fork PR — no cache or credentials". Policy, not failure (D7) |
| A single skill throws | That skill is `errored` in the trajectory; every other skill still posts |
| Evidence capture fails | Findings from that skill are suppressed by ADR-0002's gate — no evidence, no post |

Nothing in this table blocks a merge. There is no configuration that makes the runtime
tier a required check in v1.

## Alternatives considered

**Static-only v1.** Explicitly ruled out by the brief, and correctly — it is the whole
differentiator. Noting for the record that it would be ~50× cheaper, and that if the
economics turn out worse than this ADR estimates, the honest fallback is
"runtime tier on label only," not "drop the runtime tier."

**Self-hosted Mac runners.** Removes the multiplier, adds fleet ops, and makes the
open-source story worse — a project that requires a Mac mini rack to try is a project
nobody tries. Revisit for our own high-volume repo, keep GitHub-hosted as the paved road.

**Run the runtime tier on merge to main instead of on PRs.** Cheaper, and genuinely
useful for trend metrics. Loses because a finding after merge is not a review, and the
addressal metric — the north star — only exists pre-merge.

**Nightly full-matrix run, PR-time single-device run.** Actually good, and probably where
we end up. Deferred only because it needs the addressal ledger to be running first to
know which findings the matrix is buying us.

## Consequences

**Good.** Cost is bounded at four independent points (gate, matrix size, model budget,
job timeout). Failure never blocks a merge. The expensive job inherits a cheap job's
analysis instead of redoing it.

**Bad.** The base-build cache is now load-bearing for both cost and correctness of every
differential verifier. If the cache key is subtly wrong, we compare against the wrong
baseline and produce confident false positives — the worst failure mode we have. The
cache key must include everything that affects the build product, and a cache miss must
be a clean rebuild, never a partial.

**Bad.** Wall-clock of 10–20 min means we are not part of the fast feedback loop. The
static job (1–2 min) has to carry the "the bot is responsive" perception on its own.

**Bad.** Four unverified factual premises. If the multiplier or the runner GA date is
wrong, the cost section needs rewriting.

## Open questions

- Measure actual cold/warm build times on `macos-26` for a representative app before
  committing to the 30-minute timeout.
- Is `simctl status_bar override` sufficient to make renders byte-stable across boots, or
  do we also need to mask the status bar region? The mechanism is confirmed to cover every
  mutable status-bar element (P7), so region masking is now a known fallback rather than
  the plan. Still needs an empirical answer; it changes the masking config every repo
  writes.
- **Camera and notification permission denial have no `simctl privacy` service** (found
  during the P6/P7 checks — the service list is calendar, contacts, location, photos,
  media-library, microphone, motion, reminders, siri). The catalog's permission-denied
  check needs another mechanism for those two, which are the two that matter most.
- ~~Cache scoping across forks~~ — **resolved 2026-08-31 (D7): the runtime tier does not
  run on fork PRs.** No base-build cache, no credentials. Reported as policy, not
  failure. This keeps us off `pull_request_target` and its privilege-escalation
  history entirely, which is the right posture for a tool that reads other people's code.
  Consequence: outside contributors to an open-source repo get the static tier only.
