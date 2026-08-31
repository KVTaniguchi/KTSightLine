---
id: accessibility-audit
trigger_schema: 1
tier: runtime
globs: ["**/*.swift", "**/*.xib", "**/*.storyboard"]
excludes: ["**/Generated/**", "**/*.generated.swift", "**/Tests/**"]
triggers: [ui_surface_changed, view_added, view_modified]
trigger_mode: any
requires_evidence: [xcresult, screenshot]
verifier: structured_oracle
capabilities:
  - reach_surface:
      strategy: impact_derived
  - run_audit:
      kind: accessibility
      types: [clippedText, contrast, dynamicType, elementDetection, hitRegion, traits]
  - capture: [screenshot]
simulator_matrix: [se-smallest]
model_tier: cheap
cost_budget_usd: 0.05
severity_default: high
max_findings: 3
maturity: experimental
owners: []
---

# Accessibility audit

The first vertical slice. `XCUIApplication.performAccessibilityAudit(for:)` is a
first-party API with structured output, so the *detection* is not a judgment call — the
`structured_oracle` verifier confirms a finding only when the audit itself reported an
issue for that element. The model's entire job is the last mile: turn one audit issue
into one sentence a reviewer will act on, and derive a suggestion when one is mechanical.

## Your job

You are given, per issue: the audit type, the element's accessibility identifier and
label, its frame, the surface it was found on, and the screenshot.

Write the `claim` as one sentence naming the element and what is wrong with it. Name the
element the way it appears in source, not the way the audit's internal description reads.

Emit a `suggestion` only when the fix is mechanically derivable from the audit type and
the source line — a missing `.accessibilityLabel`, a `.lineLimit(1)` that should be
`nil`, a fixed frame height that should be `.fixedSize(horizontal: false, vertical: true)`.
If the fix requires knowing design intent, omit the suggestion. A wrong suggestion costs
more trust than a missing one.

## Rules

- One issue per finding. Never bundle.
- Never explain what a Dynamic Type or contrast requirement is. The reader knows.
- Never write "consider", "possibly", "might want to", or "it may be worth".
- If the audit reported an issue on an element the diff did not touch, drop it. We review
  the change, not the app.
