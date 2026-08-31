---
id: missing-usage-description
trigger_schema: 1
tier: static
globs: ["**/*.swift", "**/*.m", "**/Info.plist"]
excludes: ["**/Tests/**", "**/UITests/**"]
triggers: [permission_api_referenced, info_plist_changed]
trigger_mode: any
requires_evidence: [source_span]
verifier: structured_oracle
model_tier: none
cost_budget_usd: 0.0
severity_default: blocking
max_findings: 5
maturity: experimental
owners: []
---

# Missing Info.plist usage description

Pure static, no model, no cost. A new reference to a protected API without the matching
`NS*UsageDescription` key in the target's Info.plist is a guaranteed hard crash on first
use, in a code path no unit test covers.

The oracle is the plist itself: the verifier resolves the effective Info.plist for the
target that contains the referencing file (including `INFOPLIST_KEY_*` build settings)
and confirms the key is absent. Evidence is the `source_span` of the API reference plus
the resolved plist.

Claim template — this skill emits it deterministically, no model:

> `<API>` is referenced in `<symbol>` but `<KEY>` is not set for target `<target>`.
> This crashes on first use.

Suggestion is the plist entry. Always mechanically derivable, so always emitted.
