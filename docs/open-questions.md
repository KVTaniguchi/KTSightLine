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

### Not yet assigned to an ADR
- **Model routing config format.** The tier→model/effort mapping is decided (D5); it
  lives in config and still has no schema.
- **GitHub account/org for the private repo** (D9). Needed before the first real PR.
- **Camera and notification permission denial.** Not `simctl privacy` services (see the
  verification report). Needs another mechanism before that catalog row is implementable.
- **OQ-FIXTURE-1 — anchoring unattributable audit issues.** Many issues arrive with
  `element = nil`, so there is no identifier and no frame. A `Finding` needs a file and
  a line. Fall back to the screen's entry-point symbol, or suppress? Blocks the
  accessibility skill's anchoring logic.
- **OQ-FIXTURE-2 — is `textClipped` UIKit-only in practice?** It did not fire on a
  SwiftUI view clipped mid-glyph at AX5, across four clipping shapes. If it is
  UIKit-only, every Dynamic Type clipping check must go through `differential_render`
  and the audit can never be the cheap path for it.

### Resolved — see [decisions.md](decisions.md)
D1 verification scope · D2 fixture app · D3 license · D4 vendor vs MCP ·
D5 model routing · D6 budgets · D7 fork PRs · D8 suppression visibility · D9 eval PRs
