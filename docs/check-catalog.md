# The iOS check catalog

The map, not the backlog. One route across it gets built first (the accessibility audit
slice); everything else is scoped here so we can see what the harness has to be able to
express.

**Status key:** `planned` · `slice` (in the first vertical slice) · `built` · `shipped`

Every row names its **verifier** (ADR-0002) because a check with no verifier is a check
that cannot post. If a row's verifier column is `—`, we do not yet know how to falsify it
and it is not implementable.

---

## Tier 0 — static, no build

Runs on Linux, on every PR, ungated. Most of these are `model_tier: none` and cost
nothing.

| Check | Verifier | Model | Status |
|---|---|---|---|
| Missing `Info.plist` usage description (camera/photos/location/mic/contacts/Bluetooth/tracking) — guaranteed hard crash on first use, invisible to unit tests | `structured_oracle` (resolved effective plist) | none | slice-adjacent |
| Privacy manifest drift — newly used required-reason API with no reason in `PrivacyInfo.xcprivacy`. App Store rejection found weeks later | `structured_oracle` | none | planned |
| Availability gaps — API above deployment target with no `@available`/`#available` guard | `structured_oracle` (compiler/AST) | none | planned |
| Concurrency suppressions — new `@unchecked Sendable`, `nonisolated(unsafe)`, `@preconcurrency import`, `MainActor.assumeIsolated`, `-strict-concurrency` downgrade | `structured_oracle` | cheap (naming what was silenced) | planned |
| Test relaxation — assertions removed, `XCTAssertEqual`→`XCTAssertNotNil`, added `XCTSkip`, `continueAfterFailure = true`, snapshot baselines re-recorded in a PR claiming no UI change | `structured_oracle` | cheap | planned |
| Deadlock/crash shapes — `DispatchQueue.main.sync` reachable from main; `try!`/force-unwrap on decoded network payloads | `structured_oracle` + reachability | cheap | planned |
| Retain-cycle shapes — escaping closure capturing `self` strongly, stored on the capturing object | `structured_oracle` | standard | planned |
| Hardcoded user-facing strings bypassing the String Catalog | `structured_oracle` | none | planned |

Not automatically wrong ≠ not worth a comment. The suppression checks follow
`swarm-orchestrator`'s rule: name what was silenced, do not assert it was a mistake.

## Tier 1 — build-time

| Check | Verifier | Notes | Status |
|---|---|---|---|
| Warning **delta** attributable to the diff | `differential_metric` | Never absolute counts. `xcresulttool compare --baseline-path --build-warnings` does the diff natively — we supply the threshold | planned |
| App size delta | `differential_metric` | From the thinned App Store size report | planned |
| Build-time regression on touched modules | `differential_metric` | Noise floor unmeasured — see open-questions | planned |
| Architectural drift — a new import crossing a layer boundary declared in config | `structured_oracle` | Config-driven; generic, so it works on any repo | planned |

## Tier 2 — runtime on simulator

The reason the project exists. Gated per ADR-0003.

| Check | Verifier | Evidence | Status |
|---|---|---|---|
| **Accessibility audit** — `performAccessibilityAudit(for:)` across `contrast`, `elementDetection`, `hitRegion`, `sufficientElementDescription`, `dynamicType`, `textClipped`, `trait` (exact `XCUIAccessibilityAuditType` spellings, verified 2026-08-31; it is a bitmask, OR-folded). First-party API, structured output, effectively zero false positives | `structured_oracle` | `xcresult` + `screenshot` | **slice** |
| Dynamic Type matrix — render at AX5, compare against base-branch render for clipping, truncation, overlap | `differential_render` | 2× `screenshot` + `render_diff` | planned |
| Appearance matrix — `simctl ui <device> appearance dark`. Catches hardcoded colors that vanish or go unreadable. `simctl ui <device> increase_contrast enabled` is also available and unplanned | `differential_render` | 2× `screenshot` + `render_diff` | planned |
| RTL and pseudolocalization — `-AppleLanguages "(ar)"` and `(en-XA)`. Unmirrored layouts, string-expansion overflow | `differential_render` | 2× `screenshot` | planned |
| Device matrix — smallest supported, largest, iPad split view. Most layout bugs are a smallest-device bug | (multiplier on the above) | — | planned |
| Console diagnostics harvest — `UIViewAlertForUnsatisfiableConstraints`, "Publishing changes from within view updates", "Modifying state during view update", Main Thread Checker. Printed on every run, read by nobody | `reexecution` | `console_log` ×2 | planned |
| Sanitizers — Thread Sanitizer and Main Thread Checker on the affected test plan. Data races otherwise found in production crash reports | `reexecution` | `xcresult` + `console_log` | planned |
| Performance regression — `XCTApplicationLaunchMetric`, `XCTMemoryMetric`, `XCTOSSignpostMetric.scrollDecelerationHitches`, both branches, same job, same runner | `differential_metric` | `metric_series` ×2 | blocked on noise floor |
| Leak check — navigate to the changed screen and back N times, assert the VC/VM deallocates | `reexecution` | `console_log` / `instruments_trace` | planned |
| Degraded-network and offline — airplane mode, slow-network profile. Loading, empty, and error states, or a permanent spinner? | `reexecution` | `screenshot` + `console_log` | planned |
| Permission-denied paths — the single most common untested branch in iOS apps. `simctl privacy revoke` covers photos, contacts, location, microphone, calendar, reminders, motion, media-library, siri. **Camera and notifications are not `simctl privacy` services** — they need another mechanism (open question) | `reexecution` | `screenshot` + `crash_report` | planned, partially blocked |
| Persistence migration — install base build, seed a store, install PR build over it. Scariest class of iOS bug, essentially never caught in review | `reexecution` | `crash_report` + `console_log` | planned |
| Backgrounding and state restoration — background, terminate, relaunch. Is state lost? | `differential_render` | `screenshot` ×2 | planned |
| Deep link replay — if routing changed, replay a configured corpus of universal links | `reexecution` | `console_log` + `screenshot` | planned |

## Tier 3 — evidence a human reviewer normally has to go get

| Deliverable | Notes | Status |
|---|---|---|
| **Before/after screenshot table** in the PR body, every changed screen, across the configured matrix | Posts with **zero findings**. This alone makes people want the bot. It is not a finding, so ADR-0002's gate does not apply — but every image still carries its `context` block | planned, ship early |

Tier 3 is the one thing here that produces value on a clean PR. Worth prioritizing above
most of Tier 2 for exactly that reason.
