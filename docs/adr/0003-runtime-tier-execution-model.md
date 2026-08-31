# ADR-0003: Runtime tier execution model

- **Status:** Proposed
- **Date:** 2026-08-31
- **Deciders:** Kevin Taniguchi

> **Unverified inputs.** This ADR is built on four factual claims from `PROMPT.md` that
> have **not** been checked against Apple/GitHub documentation in this pass. They are
> tracked in [`docs/open-questions.md`](../open-questions.md) and each is marked
> `[UNVERIFIED]` below. If any is wrong, the affected section is wrong. Verify before
> this ADR moves to `Accepted`.

## Context

The runtime tier is the product. It is also the only part that costs real money, takes
real wall-clock time, and can flake. Everything about how it is scheduled has to be
designed on the assumption that it is expensive and unreliable, because it is.

Forces:

- `[UNVERIFIED]` GitHub-hosted macOS runners bill at a 10× minutes multiplier.
- `[UNVERIFIED]` `macos-26` went GA Feb 2026; `macos-latest` points at it as of Jun 2026.
- `[UNVERIFIED]` `xcresulttool get object --format json` is deprecated as of Xcode 16;
  `get test-results summary|tests --format json` is the supported path.
- `[UNVERIFIED]` `performAccessibilityAudit(for:)` throws and runs only inside a UI test
  target.
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

The math, parameterized so it can be re-run when the rates are verified. Let `M` = macOS
minutes multiplier `[UNVERIFIED: 10]`, `R` = base per-minute rate for the runner class.

| Stage | Wall-clock (est.) | Notes |
|---|---|---|
| Runner acquisition | 0.5–3 min | Queue time; not always billed, always felt |
| Checkout + toolchain select | 1 min | |
| Build head (warm DerivedData cache) | 3–6 min | Cold cache: 12–25 min |
| Build base (warm) | 2–5 min | Only when a `differential_*` verifier is in play |
| Boot simulator matrix (3 devices, parallel) | 1–3 min | Pre-warmed; `simctl bootstatus -b` |
| Drive + capture per surface per config | 20–60 s | Multiplied by matrix size |
| Verify + model calls | 0.5–2 min | Bounded by per-skill `cost_budget_usd` |
| **Total, typical gated PR** | **10–20 min** | Billed as `10–20 × M × R` |

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

`performAccessibilityAudit` only runs inside a UI test target `[UNVERIFIED]`, and most
repos will not have one shaped the way we need.

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

Parse via `xcrun xcresulttool get test-results summary --format json` and
`... get test-results tests --format json` `[UNVERIFIED]`. Never `--legacy`.

The adapter (`runners/xcode/xcresult.py`) is the *only* place that knows the JSON shape,
it records the `xcresulttool` version in the trajectory, and it is tested against a real
committed `.xcresult` fixture. When Xcode changes the schema — and it will — exactly one
file and one fixture change. Do not let this shape leak into `Finding` construction.

### 7. Degradation ladder

| Condition | Behavior |
|---|---|
| No runtime skill matched | Skip; summary says "no runtime skill matched" |
| Budget exhausted | Skip; summary names the budget and when it resets |
| Runner unavailable / queue timeout | Neutral; static findings post; summary says so |
| Build fails on head | Post *that* as a finding (with the build log as evidence) — it is the most useful comment we could make |
| Build fails on base only | Run head-only skills; suppress every `differential_*` skill with reason `no_baseline` |
| Simulator boot fails after 3 retries | Neutral for runtime; static findings post |
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
  do we also need to mask the status bar region? Determine empirically; it changes the
  masking config every repo has to write.
- Cache scoping across forks: PRs from forks cannot read the base-build cache on GitHub's
  default token permissions. Either the runtime tier does not run on fork PRs in v1, or
  we accept a cold base build for them. Leaning: does not run, stated clearly.
