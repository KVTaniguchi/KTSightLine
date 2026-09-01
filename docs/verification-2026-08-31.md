# Verification pass — 2026-08-31 (D1)

Nine premises from `PROMPT.md`. Local checks ran against **Xcode 26.6 (17F113)**,
iphonesimulator SDK 26.5, `xcresulttool` version 24757 (schema 0.1.0) on this machine.
Web claims checked against GitHub docs and the source paper.

**Result: five confirmed, four wrong.** Three of the four wrong ones would have cost
real time — the audit type names in particular were about to be compiled.

| # | Claim | Verdict |
|---|---|---|
| P1 | macOS runners bill at a 10× *minutes multiplier* | ⚠️ **Right magnitude, wrong mechanism** |
| P2 | `macos-26` GA Feb 2026; `macos-latest` → it as of Jun 2026 | ✅ Confirmed |
| P3 | `get object` deprecated; use `get test-results summary\|tests` | ✅ Confirmed, **and the schema *is* published** |
| P4 | `performAccessibilityAudit` throws, UI-test-target only | ✅ Confirmed |
| P5 | Audit types `clippedText`, `traits`, `elementDescription`, … | ❌ **Three names wrong** |
| P6 | `simctl ui <udid> content_size` / `appearance dark` | ✅ Confirmed verbatim |
| P7 | `simctl status_bar override` for a fixed status bar | ✅ Mechanism confirmed; sufficiency still empirical |
| P8 | GitHub positioning via `line` + `side` + `start_line` | ⚠️ **Confirmed, but `start_side` is missing from our schema** |
| P9 | arXiv 2607.21997, ~11pp lift from concrete suggestions | ✅ Confirmed — 10.9pp, with a caveat |

---

## P1 — macOS runner cost ⚠️

**Claim:** "GitHub-hosted macOS runners bill at a 10× minutes multiplier."

**Finding:** There is no minutes-multiplier table in current GitHub billing docs. Runners
are billed at flat per-minute prices:

| Runner | $/min |
|---|---|
| Linux 2-core (x64) | $0.006 |
| Windows 2-core (x64) | $0.010 |
| **macOS 3/4-core** | **$0.062** |

macOS is **10.3× the Linux price**, and included/free minutes appear to be consumed 1:1
regardless of OS rather than at a multiplied rate. The historical "Linux 1× / Windows 2×
/ macOS 10×" multiplier model is not what the current docs describe.

**Consequence:** the magnitude in ADR-0003 survives; the mechanism sentence was wrong.
Real numbers now in ADR-0003 §3.

## P2 — Runner image ✅

`macos-26` went GA **2026-02-26**. The `macos-latest` migration to it **began 2026-06-15**
and took 30 days, so `macos-latest` = macOS 26 from roughly mid-July 2026. Runners are
native Apple Silicon (arm64); Intel x64 also available.

**Worth recording:** the `macos-26` image's default Xcode is **26.4.1**. This machine has
**26.6**. Since `xcresulttool` output is schema-versioned (P3), the adapter must pin and
record the schema version rather than assume the local toolchain matches CI.

## P3 — xcresulttool ✅ (and better than claimed)

`xcrun xcresulttool get object` is confirmed deprecated: *"This subcommand is deprecated
and will be removed in a future release."* `get test-results summary` and
`get test-results tests` both exist. Do not use `--legacy`.

Three corrections/additions the brief did not have:

1. **The schema is published.** Every modern subcommand takes `--schema` (emits JSON
   Schema) and `--schema-version <major.minor.patch>`, erroring on an unknown version.
   The brief said "the schema is no longer published." We can pin explicitly instead of
   reverse-engineering, and a schema change becomes a loud error rather than a silent
   parse drift.
2. **The subcommand surface is wider than `summary|tests`:** `test-details`,
   `activities`, `insights`, and **`metrics`** (performance metrics, `--test-id`
   filterable) under `get test-results`, plus a separate **`get build-results`** for
   build warnings and issues.
3. **`xcresulttool compare` exists** — `compare <path> --baseline-path <path>` with
   `--summary`, `--test-failures`, `--tests`, `--build-warnings`, `--analyzer-issues`.

Apple ships the differential. This is an architecture simplification: the
`differential_metric` verifier's warning-delta and test-delta work is a `compare` call
plus a threshold, not a diffing engine we write. See ADR-0002 follow-up.

**Stale Apple help text, noted so nobody chases it:** the deprecation message points at
`xcresulttool get test-report`, which **does not exist**. The real subcommand is
`get test-results`.

## P4 — performAccessibilityAudit ✅

From `XCUIAutomation.framework`'s Swift interface:

```swift
@available(macOS 14.0, iOS 17.0, tvOS 17.0, watchOS 10.0, *)
@MainActor public func performAccessibilityAudit(
    for auditTypes: XCUIAccessibilityAuditType = .all,
    _ issueHandler: ((XCUIAccessibilityAuditIssue) throws -> Bool)? = nil
) throws
```

Throws ✅. `@MainActor` (not in the brief, matters for the driver). On `XCUIApplication`
in `XCUIAutomation.framework`, which lives under `Platforms/…/Developer/Library/
Frameworks` — linked by test targets only, so "UI test target required" is confirmed
structurally. iOS 17.0+.

`XCUIAccessibilityAuditIssue` gives `element` (`XCUIElement?`), `compactDescription`,
`detailedDescription`, `auditType`. Identifier and frame come off `element`, not the
issue.

## P5 — Audit type names ❌

The brief's names, and the ones already written into `skills/accessibility-audit.md`,
were wrong. Actual `XCUIAccessibilityAuditType` (an `NS_OPTIONS` **bitmask**, combined
with bitwise OR):

| Written | Actual |
|---|---|
| `clippedText` | **`textClipped`** |
| `traits` | **`trait`** |
| `elementDescription` | **`sufficientElementDescription`** |
| `contrast` | `contrast` ✅ |
| `elementDetection` | `elementDetection` ✅ |
| `hitRegion` | `hitRegion` ✅ |
| `dynamicType` | `dynamicType` ✅ |

Plus `.all` (`~0UL`). `dynamicType`, `textClipped`, and `trait` are iOS/tvOS/watchOS
only; `contrast`, `elementDetection`, `hitRegion`, `sufficientElementDescription` are
cross-platform. macOS-only members (`action`, `parentChild`) exist but are out of scope.

Because it is a bitmask, the frontmatter list has to be OR-folded into a single mask —
not passed as an array. Fixed in `skills/accessibility-audit.md` and the catalog.

## P6 — simctl ui ✅

`simctl ui <device> appearance [light | dark]` ✅ and
`simctl ui <device> content_size <value>` ✅, with
`accessibility-extra-extra-extra-large` confirmed in the accepted value list. The
brief's invocation was verbatim correct.

**Bonus not in the brief:** `simctl ui <device> increase_contrast [enabled | disabled]`.
Directly useful for the contrast audit.

## P7 — simctl status_bar ✅ (mechanism)

`simctl status_bar <device> override` accepts `--time`, `--dataNetwork`, `--wifiMode`,
`--wifiBars`, `--cellularMode`, `--cellularBars`, `--operatorName`, `--batteryState`,
`--batteryLevel`. That covers every mutable element of the status bar, so the mechanism
for a deterministic status bar exists.

Whether it makes renders *byte-stable* remains empirical and stays an open question —
but we now know the fallback (region-mask the status bar) is a fallback, not the plan.

## P8 — GitHub comment positioning ⚠️

`line` + `side` confirmed. `side` takes `LEFT` (deletions, red) or `RIGHT` (additions in
green, or unchanged context in white). `position` is explicitly deprecated — *"This
parameter is closing down. Use line instead."* — so it is not "the old API" we might
fall back to; it is a thing to never send.

**Correction to our schema:** multi-line comments require **`start_side`** as well as
`start_line`. Our `Anchor` model had only `start_line`. Fixed.

## P9 — arXiv 2607.21997 ✅

*"Go Home Copilot, You're Drunk": Understanding Developer Responses to Agent-Generated
Code Review Comments.* Real, and the numbers hold:

- **54,713** agent comments ✅, three agents (Copilot, Cursor, Codex).
- Inline code suggestion: **75.5%** resolution vs **64.6%** without — **10.9pp**. The
  brief's "~11 percentage points" is accurate. Holds for functional (74.4% vs 61.3%) and
  evolvability (76.1% vs 67%) issues separately.
- Length: non-accepted comments averaged **807 characters**; useful ones **616**.
  Logistic regression OR = 0.926 — real, but the paper calls it *marginal*. "Longer
  comments do worse" is directionally right and weaker than the suggestion effect.

**Caveat the brief omitted:** the corpus is **341 Python repositories**. We are
generalizing a Python-repo finding to Swift. The direction is almost certainly
transferable; the magnitudes are not evidence about iOS.

**Design number worth keeping:** 616 characters is the observed mean length of a useful
comment. That is a concrete target for our format, not a vibe.

---

## Corrections applied

- `skills/accessibility-audit.md` — audit type names (P5)
- `docs/check-catalog.md` — audit type names; `simctl privacy` service list (below)
- `sightline/core/findings/models.py` — `Anchor.start_side` (P8)
- `docs/adr/0003-runtime-tier-execution-model.md` — real cost numbers (P1), Xcode
  version skew (P2), `compare`/`--schema` (P3)
- `docs/adr/0002-evidence-and-verification.md` — `xcresulttool compare` as the
  `differential_metric` primitive (P3)

## Found while verifying, not in the nine

**`simctl privacy` has no `camera` and no `notifications` service.** The full list is:
`all`, `calendar`, `contacts-limited`, `contacts`, `location`, `location-always`,
`photos-add`, `photos`, `media-library`, `microphone`, `motion`, `reminders`, `siri`.

The catalog's permission-denied check named camera and notifications specifically. Photos
and microphone are reachable via `simctl privacy revoke`; **camera and notification
denial need another mechanism** — new open question.

---

## P8 follow-up — cross-checked against real accepted comments (2026-08-31)

Reading the docs is not verification. Since positioning is the disqualifying risk, our
`commentable_lines` was cross-checked against comments **GitHub itself accepted** — every
existing review comment on a PR is, by definition, a position GitHub allowed. Read-only,
nothing posted.

Sampled 26 anchored review comments across `cli/cli`, `pallets/click`, `astral-sh/ruff`,
`pydantic/pydantic`, `encode/httpx`, `tiangolo/fastapi`, `psf/black`, and `python/mypy`.

**Result: 25/26 agree. The two initial disagreements were both informative.**

### Found a real bug: LEFT-side comments on context lines

`astral-sh/ruff#28200` carries an accepted comment at `ci.yaml:597, side=LEFT`. Our
validator refused it, because `commentable_left_lines` was originally just
`removed_lines`. That is wrong: an unchanged context line appears on **both** sides of a
split diff, and GitHub accepts a LEFT comment on it. Fixed —
`commentable_left_lines = removed_lines | context_old_lines`, with a regression test.

This is exactly the class of error the brief warned about, and it would not have been
caught by any amount of reading the docs.

### Found a feature we were not using: `subject_type: "file"`

The same PR carries comments with `subject_type: "file"` and a placeholder `line: 1`.
These are **file-level** comments — a legitimate anchoring mode that needs no line.

Relevant beyond bookkeeping: this is the honest third option for **OQ-FIXTURE-1**. An
audit issue arriving with `element = nil` currently gets dropped, because ADR-0002
forbids inventing a line. A file-level comment attributes it to the file without
inventing anything. `Anchor.file_level` now exists and `comment_payload` emits
`subject_type: "file"` for it.

### The one remaining disagreement is not ours

`python/mypy#21888` has an accepted comment at `check-sentinels.test:178`, while the
diff GitHub serves us has a single hunk starting at line 181. Our parse matches the raw
diff exactly. The explanation is GitHub's UI letting a reviewer expand context beyond
the hunk and comment there.

We deliberately do not follow: our validator refuses anchors outside the served diff.
Commenting only where we are certain the position is right is the conservative choice,
and the cost is losing a capability we have no use for.

**Still not verified:** that a comment we post lands where we intend. That requires
actually posting one, which needs Kevin's authorization.
