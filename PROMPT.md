# Project kickoff: Sightline — an evidence-grounded PR reviewer for iOS

> Save this file as `PROMPT.md` in an empty repo, then start Claude Code and say:
> **"Read PROMPT.md and follow it. Start in plan mode."**
> Rename "Sightline" to whatever you want before you hand it over.

---

## Your role

You are the founding engineer on a new open-source project. I am a veteran e-commerce iOS engineer; assume I know Swift, Xcode, and iOS release engineering deeply, and that I do **not** need Swift or iOS concepts explained to me. Where you need domain judgment about my app or my team, ask me — don't guess and don't pad.

Work spec-first. Plan before you code, write down decisions, and build one thing end to end before you build ten things halfway.

---

## The thesis

Every AI code reviewer on the market reads the diff. Some read the repo. **None of them run the app.**

That is fine for backend code, where the diff mostly *is* the behavior. It is close to useless for iOS, where the highest-cost bugs are the ones an engineer only finds by building the branch in Xcode, booting a simulator or plugging in a device, and *looking at it*:

- text that clips at Accessibility XXXL but not at default size
- a hardcoded `UIColor` that disappears in dark mode
- a constraint conflict that logs to the console and is never read by anyone
- a Core Data migration that crashes only when you install over the previous build
- a missing `NSCameraUsageDescription` that crashes on first tap, in a code path no unit test covers
- a permanent spinner because the new screen has no error state and nobody tested with the network off
- a data race that Thread Sanitizer catches in ten seconds and code review never will

**Sightline builds the PR branch, boots simulators, drives the app, captures what it sees, and reviews the evidence.** A finding that is not backed by an artifact — a screenshot, a log line, a symbolicated crash, an `.xcresult` bundle, a measured metric — does not get posted. That constraint is the product.

---

## What we're borrowing (and from where)

This project's architecture takes its cues from Uber's `uReview` (see their engineering blog post of the same name) plus a handful of open-source systems worth reading before you design anything. Read these first — most of the hard thinking is already done in public:

| Read | For |
|---|---|
| `alibaba/open-code-review` | The deterministic/agent split. Engineering owns file selection, bundling, rule matching, and comment positioning; the model only does judgment. This is our spine. |
| `Agent-Field/pr-af` | The falsifiability gate — a finding must survive programmatic verification before it can be posted. This is our evidence rule, generalized. |
| `imbue-ai/vet` | Dedupe + filter as a *named pipeline stage*, not a prompt instruction. Also reviews agent trajectories, not just diffs. |
| `moonrunnerkc/swarm-orchestrator` | Its eleven "agent faked done" cheat patterns — swallowed errors, stripped assertions, added suppressions, tests relaxed. Nearly all have iOS-specific analogues. |
| `The-PR-Agent/pr-agent` | `SKILL.md` convention and a clean forge-adapter layer across five forges. |
| `withmartian/code-review-benchmark` | Its *online* mode measures which bot suggestions developers actually implemented. That's our north-star metric. |
| `reviewdog/reviewdog` | Diff-to-line comment positioning across forges. Do not rebuild this badly; study how it maps tool output onto hunks. |
| `getsentry/XcodeBuildMCP`, `joshuayoes/ios-simulator-mcp` | Existing agent-facing wrappers around `xcodebuild` / `simctl`. Decide whether to depend on one or vendor the subset we need. |
| `a7ex/xcresultparser`, `pointfreeco/swift-snapshot-testing` | `.xcresult` parsing and snapshot comparison prior art. |
| arXiv 2607.21997 (*"Go Home Copilot, You're Drunk"*) | 54,713 real agent review comments. Key finding: **including a concrete code suggestion raises adoption by ~11 percentage points; longer comments do worse.** Design the comment format around this. |

And the five things from the Uber talk that must be in the architecture from commit #1, not bolted on later:

1. **Multi-agent dispatch, not one prompt.** A controller decides which skills run based on what the change actually is.
2. **Distributed ownership.** Teams author their own skills as files in the repo. The harness is shared; the rules are not.
3. **Addressal observability.** Cost and NPS are vanity metrics. The real ones are addressal rate (Uber sits around 67%), reply sentiment, and agent trajectories. Instrument these before you have a single user.
4. **Precision over recall.** A reviewer that posts 40 comments and is right 12 times gets muted. Suppression is a feature.
5. **Forge-agnostic core.** GitHub first, but the review engine never imports a GitHub type.

---

## Decisions already made — do not relitigate these

- **Language:** Python 3.12+. Shell out to `xcodebuild`, `xcrun simctl`, `xcrun xctrace`. Use `uv` for dependency management.
- **Forge:** GitHub, via GitHub Actions, using `macos-26` runners. But the core must sit behind a `ForgeAdapter` protocol so Phabricator/Gerrit/Bitbucket can be added without touching the engine.
- **Runtime tier:** v1 **does** build the branch and boot simulators. This is the whole point; do not talk me into a static-only v1.
- **Audience:** generic and open-sourceable. It must work on any iOS repo. Anything specific to my app lives in config, never in code.
- **Model access:** assume Anthropic API. Design for model *routing* — cheap model for triage and classification, frontier model for reasoning-heavy skills — with the tier declared per-skill.

---

## Architecture you should propose (and then argue with)

Treat this as my opening bid, not a requirement. Push back where I'm wrong.

```
sightline/
  core/
    diff/          # parse PR diff → changed files, symbols, targets, test plans
    impact/        # deterministic: which UI surfaces does this change reach?
    skills/        # skill registry, glob matching, dispatch
    evidence/      # artifact store: screenshots, logs, xcresult, traces
    findings/      # Finding model, fingerprinting, dedupe, severity, suppression
    verify/        # the falsifiability gate
    telemetry/     # trajectories, cost, addressal ledger
  adapters/
    forge/         # ForgeAdapter protocol; github.py implements it
    model/         # model routing
  runners/
    xcode/         # build, test, xcresult parsing
    simulator/     # boot, configure, drive, capture
  skills/          # built-in skills, as .md files
  eval/            # benchmark corpus + scoring harness
```

**Non-negotiable design rules:**

1. **The deterministic layer never calls a model.** Choosing which files matter, which targets rebuild, which screens a change can reach, which simulator configs to boot, and where a comment lands on a diff — all of that is engineering. If a model is doing it, it's in the wrong layer.

2. **Every `Finding` carries evidence or dies.** Schema roughly:
   ```python
   Finding(
     rule_id: str,              # skill that produced it
     fingerprint: str,          # stable across pushes; NOT line-number based
     file: str, line: int,
     severity: Literal["blocking","high","medium","low"],
     claim: str,                # one sentence
     evidence: list[ArtifactRef],   # >= 1 required, enforced by the type system
     suggestion: str | None,    # concrete code suggestion where possible
     verified_by: str | None,   # which verifier passed it
   )
   ```
   A skill that cannot produce an `ArtifactRef` cannot post. Make this a hard constraint in the model layer, not a convention.

3. **Fingerprints are content-derived, not position-derived.** Hash `(rule_id, file, enclosing symbol, normalized claim)`. Line numbers move on every push; a bot that re-posts the same comment three times is dead on arrival.

4. **Skills are files, not code.** Markdown with YAML frontmatter, discovered from `.sightline/skills/**.md` in the *target* repo plus built-ins. Frontmatter declares at minimum:
   ```yaml
   id: dynamic-type-clipping
   tier: runtime            # static | build | runtime
   globs: ["**/*.swift", "**/*.xib", "**/*.storyboard"]
   triggers: [ui_surface_changed]
   requires_evidence: [screenshot]
   simulator_matrix: [se-smallest, pro-max, ipad-split]
   model_tier: standard
   cost_budget_usd: 0.15
   ```
   A team should be able to add a skill by writing one file and opening a PR. No plugin registration, no Python.

5. **Telemetry is a first-class module, not logging.** Every run writes a trajectory record: which skills fired, why (which trigger matched), what they cost, wall clock, what evidence they gathered, what got suppressed and by which rule. Every posted comment goes into an addressal ledger. On each subsequent push, resolve prior comments: did the flagged line change? was the thread resolved? what did the human reply? SQLite locally, but keep the write path behind an interface so it can go somewhere real later.

---

## The iOS check catalog

This is the actual differentiator. Do **not** implement all of it now — this is the map, and I want you to build one route across it first.

### Tier 0 — static, no build (runs on every PR, costs nothing)

- **Missing `Info.plist` usage descriptions.** New reference to camera / photos / location / mic / contacts / Bluetooth / tracking APIs without the matching `NS*UsageDescription`. Guaranteed hard crash on first use, invisible to every unit test.
- **Privacy manifest drift.** A newly used required-reason API (`UserDefaults`, file timestamp, disk space, active keyboard, system boot time) with no declared reason in `PrivacyInfo.xcprivacy`. This is an App Store rejection, found weeks later.
- **Availability gaps.** API used above the deployment target without `@available` / `#available` guard.
- **Concurrency suppressions as cheat patterns.** New `@unchecked Sendable`, `nonisolated(unsafe)`, `@preconcurrency import`, `MainActor.assumeIsolated`, or `-strict-concurrency` downgrade. Treat exactly like `swarm-orchestrator` treats added lint suppressions: not automatically wrong, always worth a comment naming what was silenced.
- **Test relaxation.** Assertions removed, `XCTAssertEqual` → `XCTAssertNotNil`, added `XCTSkip`, `continueAfterFailure = true`, snapshot baselines re-recorded in a PR that claims no UI change.
- **Deadlock and crash shapes.** `DispatchQueue.main.sync` on a path reachable from main; `try!` / force-unwrap on decoded network payloads.
- **Retain-cycle shapes.** Escaping closure capturing `self` strongly and stored on the capturing object.
- **Hardcoded user-facing strings** bypassing the String Catalog.

### Tier 1 — build-time

- Warning **delta** attributable to the diff (never absolute counts).
- App size delta from the thinned App Store size report.
- Build-time regression on touched modules.
- Architectural drift: a new import that crosses a layer boundary declared in config.

### Tier 2 — runtime on simulator (the reason this project exists)

- **Accessibility audit.** `XCUIApplication.performAccessibilityAudit(for:)` across all audit types — contrast, dynamic type, element description, hit region, trait, clipped text — run against the screens the diff reaches. First-party API, structured output, effectively zero false positives. **This is your first vertical slice.**
- **Dynamic Type matrix.** `xcrun simctl ui <udid> content_size accessibility-extra-extra-extra-large`, screenshot, compare against the base-branch render for clipping, truncation, and overlap.
- **Appearance matrix.** `simctl ui <udid> appearance dark`. Catches hardcoded colors that vanish or go unreadable.
- **RTL and pseudolocalization.** Launch with `-AppleLanguages "(ar)"` and `(en-XA)`. Catches unmirrored layouts and string-expansion overflow.
- **Device matrix.** Smallest supported device, largest, and iPad split view. Most layout bugs are a smallest-device bug.
- **Console diagnostics harvest.** Scrape the run log for `UIViewAlertForUnsatisfiableConstraints`, "Publishing changes from within view updates", "Modifying state during view update", Main Thread Checker violations. These are printed on every run and read by nobody.
- **Sanitizers.** Thread Sanitizer and Main Thread Checker on the affected test plan. Data races surfaced here are otherwise found in production crash reports.
- **Performance regression.** `XCTApplicationLaunchMetric`, `XCTMemoryMetric`, `XCTOSSignpostMetric.scrollDecelerationHitches` — measured on *both* branches, in the same job, on the same runner. A benchmark comparing across runners is noise.
- **Leak check.** Navigate to the changed screen and back N times; assert the view controller / view model deallocates.
- **Degraded-network and offline paths.** Airplane mode and a slow-network profile. Does the new screen have loading, empty, and error states, or does it spin forever?
- **Permission-denied paths.** Launch with camera / photos / notifications pre-denied. The single most common untested branch in iOS apps.
- **Persistence migration.** Install the base-branch build, seed a store, install the PR build over it. Core Data / SwiftData migration crashes are the scariest class of iOS bug and are essentially never caught in review.
- **Backgrounding and state restoration.** Background, terminate, relaunch. Is state lost?
- **Deep link replay.** If routing changed, replay a configured corpus of universal links.

### Tier 3 — the evidence a human reviewer normally has to go get

- **Before/after screenshot table** in the PR body for every changed screen, across the configured matrix. Even with zero findings, this alone will make people want the bot. Ship it early.

---

## Explicit non-goals

- Not a general-purpose code reviewer. If a finding would apply equally to a Django repo, it is out of scope. Generic reviewers exist and are good; we are the layer they cannot reach.
- Not a linter. SwiftLint and SwiftFormat exist. Do not reimplement them — consume their output as evidence.
- No style comments. Ever.
- No comment without evidence. This is not a guideline.
- No LLM-generated Swift refactors of my source. Suggestions are minimal, surgical, and mechanically checkable.

---

## Traps — you will get these wrong unless you check

- **`xcresulttool get object --format json` is deprecated** as of Xcode 16 and the schema is no longer published. Use `xcrun xcresulttool get test-results summary --format json` and `... get test-results tests --format json`. Pin the parsing behind an adapter and test it against a real bundle; do not build on `--legacy`.
- **GitHub-hosted macOS runners bill at a 10× minutes multiplier.** `macos-26` went GA in Feb 2026 and `macos-latest` points at it as of June 2026. Architecture consequence: the static tier must run on every PR on Linux, and the runtime tier must be *gated* — by changed-path globs, by label, or by a cheap classifier — and must fail open, never blocking a merge.
- **Simulator boot is slow and flaky in CI.** Pre-warm, use `simctl bootstatus -b`, add retries, and cache derived data aggressively. Budget for this in the design, not in a hotfix.
- **`performAccessibilityAudit` throws and only runs inside a UI test target.** The harness has to generate or discover a UI test target; plan the injection strategy.
- **GitHub review comment positioning** takes `line` + `side` + `start_line` on the newer API, or a diff-hunk `position` on the old one. Get this wrong and every comment lands on the wrong line, which is instantly disqualifying. Test it against a real PR early.
- **Do not invent Apple APIs.** If you are not certain a symbol, `simctl` subcommand, or audit type exists in the current SDK, verify it against Apple's docs or `--help` before you write code against it. A confidently hallucinated `simctl` flag will cost me an afternoon.
- **Screenshot comparison is not `==`.** Anti-aliasing, animation timing, and system UI make naive pixel diffs useless. Decide deliberately: perceptual threshold, region masking, or a vision model as a judge — and write down why.

---

## How a comment should read

Short, specific, and carrying a fix. From the arXiv study: concrete suggestions get adopted; prose gets ignored; length hurts.

````
⚠️ Clipped at Accessibility XXXL — `CheckoutSummaryView.swift:142`

"Estimated delivery" truncates to "Estimated de…" at AX5 on iPhone SE.
Evidence: screenshot-ax5-se.png · accessibility audit: clippedText

```suggestion
    .lineLimit(nil)
    .fixedSize(horizontal: false, vertical: true)
```
````

Rules: one claim per comment. Evidence link always. Suggestion whenever mechanically derivable. Never explain iOS to the reader. Never hedge with "consider possibly".

---

## What I want from THIS session

Do not start writing the harness. In order:

1. **Research pass.** Read the prior art above — at minimum `alibaba/open-code-review`'s architecture, `pr-af`'s verification gate, and one of the Xcode MCP servers. Verify the Xcode 26 / `xcresulttool` / `macos-26` facts I asserted above; tell me if I'm wrong.
2. **Write three ADRs** in `docs/adr/`:
   - Skill format and dispatch — how a team-authored file becomes a running check.
   - Evidence and verification — what an `ArtifactRef` is, how the falsifiability gate works, what happens to an unverifiable finding.
   - Runtime tier execution model — what boots, when, how it's gated, how cost is bounded, and how it degrades when a runner is unavailable.
3. **Propose the finding schema and the skill frontmatter schema** concretely, as code. This is the contract everything else hangs off; I want to review it before anything is built on it.
4. **Build exactly one vertical slice, end to end:**
   > A PR touching a SwiftUI view → impact analysis identifies the affected screen → boot one simulator → run an accessibility audit against that screen → produce one `Finding` with a screenshot artifact → post one correctly-positioned GitHub review comment → write the trajectory and addressal-ledger records.

   Prove it against a small open-source iOS app with a deliberately introduced accessibility defect. Vendor the sample app into `eval/fixtures/`.
5. **Stand up `eval/` with three PRs in it.** Two with known real defects, one clean. Score precision and recall. Three is enough to keep us honest; the corpus grows from here.
6. **Write the README** as if the project were already good — thesis, what it catches that nothing else does, how to add a skill. Writing it now will expose where the design is vague.

**Definition of done for this session:** I can run `sightline review --pr <url>` against the fixture repo, get one true-positive accessibility comment posted on a real PR with a screenshot attached, see the trajectory JSON, and read three ADRs that tell me why it's shaped the way it is.

---

## How to work with me

- Start in plan mode. Show me the plan before you build.
- Ask when a decision is genuinely mine — naming, scope cuts, which app to use as a fixture, anything that costs money. Don't ask permission for obvious engineering choices.
- Commit in small, reviewable units with real messages.
- If something in this brief is wrong, say so directly. I'd rather rewrite the spec now than find out in week three. In particular: if you think the runtime tier is too expensive or too flaky to be the v1 differentiator, make that case with numbers before you build it.
