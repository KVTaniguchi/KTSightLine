# Open questions

Two kinds of entry: **unverified premises** (factual claims the design rests on that
nobody has checked) and **unsettled decisions** (things the ADRs deliberately deferred).

An ADR cannot move to `Accepted` while it depends on an unverified premise.

## Unverified premises — RESOLVED 2026-08-31

All nine checked. Full report with evidence:
[`verification-2026-08-31.md`](verification-2026-08-31.md). Five confirmed, four wrong.
Corrections are applied; the ADRs no longer carry `[UNVERIFIED]` markers.

| # | Verdict |
|---|---|
| P1 | ⚠️ 10.3× **price ratio** ($0.062 vs $0.006/min), not a minutes multiplier |
| P2 | ✅ GA 2026-02-26; `macos-latest` migrated from 2026-06-15. Runner Xcode is 26.4.1 |
| P3 | ✅ …and better: schema **is** published (`--schema-version`), and `compare --baseline-path` ships a native differential |
| P4 | ✅ throws, `@MainActor`, test-target only |
| P5 | ❌ `textClipped` not `clippedText`; `trait` not `traits`; `sufficientElementDescription` not `elementDescription` |
| P6 | ✅ verbatim correct. Bonus: `increase_contrast` |
| P7 | ✅ mechanism confirmed; byte-stability still empirical |
| P8 | ⚠️ confirmed, but `start_side` was missing from our schema; `position` is deprecated, not a fallback |
| P9 | ✅ 54,713 comments, 10.9pp. Caveat: corpus is 341 **Python** repos |

## Unsettled decisions

### From ADR-0001 (skills and dispatch)
- Do skills compose — skill A's finding as skill B's input? Deferred. If it comes up,
  model it as a capability, not as skill-to-skill imports.
- When a skill's Markdown body changes, is a prior comment's fingerprint still
  comparable? Leaning yes (fingerprint is claim-derived, not prompt-derived), but the
  eval corpus has to prove it.
- Should `maturity: owners_only` gate posting during rollout? Probably. Needs the
  addressal ledger running to know when a skill graduates.

### From ADR-0002 (evidence and verification)
- Noise floor for `differential_metric` on GitHub-hosted runners is unknown and probably
  bad. Measure before any performance skill posts.
- Surface suppressed findings to the author, or only to us? Leaning: collapsed
  `<details>` in the run summary, never on the diff.
- Artifact retention past CI job expiry. `EvidenceStore` must not assume a filesystem now.

### From ADR-0003 (runtime tier)
- Measure real cold and warm build times on `macos-26` before committing to the 30-min
  timeout.
- Fork PRs cannot read the base-build cache under default token permissions. Either the
  runtime tier does not run on fork PRs in v1, or they eat a cold base build. Leaning:
  does not run, stated plainly in the summary.
- Nightly full-matrix + PR-time single-device is probably the right end state. Blocked on
  having addressal data to justify the matrix.

### From ADR-0004 (acceptance tier)
- Executors for `network_call` and `persisted_state` evidence. `network_call` likely
  wants an app-side hook, which raises the same instrumentation-trust question as logs.
- Device, appearance and Dynamic Type matrices for acceptance runs (one simulator, one
  configuration in v1).
- Posting verdicts to a PR — needs a `ForgeAdapter` method and authorization (D12).
- Discovering criteria from a PR body or commit trailers; wiring into `sightline review`
  and `.sightline/config.yml`.
- Fallback log capture (`simctl spawn … log show`) if a future Xcode drops the stderr
  mirror (verification V1). Liveness fails closed meanwhile.
- Does CI's Xcode (26.4.1) emit the same diagnostics layout and XCTest agent lines as
  27.1? Unverified (verification V7).

### Not yet assigned to an ADR
- **OQ-REAL-1 — anchoring without identifiers.** Findings anchor by finding the audited
  element's `accessibilityIdentifier` on an added line. Nearby Meows has none, so on its
  real PRs every issue is `unanchorable` or `not_in_diff`; the eval cases add one to the
  new control. Is that an acceptable ask of adopting repos, or does anchoring need
  another key (label text, the view's `body` span, a source-location attribute)?
- **Does `simctl privacy revoke` actually take effect?** D14 found `grant location` is
  a no-op on the iOS 26.5 simulator. The catalog's permission-denied row assumes
  `revoke` works for nine services; that was also verified from `--help` only, and
  still is — `revoke` has not been tested by effect. The service list was re-confirmed
  on Xcode 27.1 (2026-10-20): calendar, contacts, contacts-limited, location,
  location-always, photos, photos-add, media-library, microphone, motion, reminders,
  siri.
- **Model routing config format.** The tier→model/effort mapping is decided (D5); it
  lives in config and still has no schema.
- **GitHub account/org for the private repo** (D9). Needed before the first real PR.
- **Camera and notification permission denial.** Not `simctl privacy` services
  (re-confirmed on Xcode 27.1, 2026-10-20). **Mechanism added 2026-10-20 (D21):**
  `deny_alerts:` plus `sightline audit --deny-permissions` answers every prompt with
  its deny button, and a prompt with no configured deny title fails the run rather than
  being granted. **Not yet measured on a device:** the vendored fixture cannot exercise
  it, because D-003 deliberately omits `NSCameraUsageDescription`, so it crashes instead
  of prompting. Confirming it needs a probe app carrying valid usage descriptions, or a
  real app that prompts (Nearby Meows prompts for location).
- ~~**OQ-FIXTURE-1**~~ — **resolved 2026-08-31 (D11): drop them.** Not posted as
  file-level comments, not anchored to a guessed line. Costs ~3 of 5 real failures on
  the fixture's Cart screen; every drop is counted under `unanchorable` so the cost is
  measured rather than assumed. Revisit if the suppression log says the loss is worse
  than the precision is worth.
- ~~**OQ-FIXTURE-2 — `textClipped` fires, but unattributably.**~~ — **resolved
  2026-10-20 (D19): the premise was wrong.** It does not arrive with `element = nil`.
  Re-read from the committed fixture (`tests/fixtures/xcresult/activities-cart.json`):

  ```
  SIGHTLINE|Cart|textClipped|id=|label=Price $185.00|frame=(252.5, 256.75, 66.5, 20.5)|Text clipped
  SIGHTLINE|Cart|hitRegion|id=|label=|frame=nil|Hit area is too small          ← this one is element = nil
  ```

  The element exists and carries a label and a frame; it has no
  `accessibilityIdentifier`. `is_attributable` tested `bool(identifier)`, so the two
  cases were indistinguishable and both were dropped. Anchoring by label literal
  recovers it, so **Dynamic Type clipping is not permanently a `differential_render`
  job** and `dynamic-type-clipping` can use `structured_oracle`. Still true: it did not
  fire for the CheckoutSummary shapes across four variants, so `differential_render`
  remains the only route for clipping the audit does not report at all.

- **OQ-CONTRAST-1 — can a contrast failure be attributed to the diff rather than to a
  system style?** Blocks admitting `contrast` to `LABEL_ANCHORABLE_AUDIT_TYPES` (D20).
  Measured on eval case 003, 2026-10-20: the audit reports `Contrast failed` against
  the inner label of a stock `.buttonStyle(.bordered)` button — tinted text on a
  near-white fill, ~3.5:1 against a 4.5:1 threshold. The finding is correct and must
  still not post, because every adopting repo would get it on every `.bordered` button.
  Admitting it needs a way to tell author-chosen colours from inherited ones. Until
  then the fixture's "clean" case is clean only because `contrast` is excluded, which
  is a thinner guarantee than it looks and is worth remembering before the allowlist
  grows.

### Resolved — see [decisions.md](decisions.md)
D1 verification scope · D2 fixture app · D3 license · D4 vendor vs MCP ·
D5 model routing · D6 budgets · D7 fork PRs · D8 suppression visibility · D9 eval PRs ·
D16 acceptance mode and exit codes · D17 log evidence · D18 crash, unreachable and absent verdicts
