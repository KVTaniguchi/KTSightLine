# Open questions

Two kinds of entry: **unverified premises** (factual claims the design rests on that
nobody has checked) and **unsettled decisions** (things the ADRs deliberately deferred).

An ADR cannot move to `Accepted` while it depends on an unverified premise.

## Unverified premises

These come from `PROMPT.md` and were **not** checked in the planning pass. Each blocks an
ADR. Verify against Apple/GitHub documentation or `--help`, then record the answer here
with a date and a link.

| # | Claim | Blocks | Status |
|---|---|---|---|
| P1 | GitHub-hosted macOS runners bill at a **10× minutes multiplier** | ADR-0003 §3 cost model | Unverified |
| P2 | `macos-26` went GA Feb 2026; `macos-latest` points at it as of Jun 2026 | ADR-0003 §1 job config | Unverified |
| P3 | `xcresulttool get object --format json` is deprecated as of Xcode 16; `get test-results summary\|tests --format json` is the supported path | ADR-0003 §6 | Unverified |
| P4 | `performAccessibilityAudit(for:)` throws and runs only inside a UI test target | ADR-0003 §5 injection strategy | Unverified |
| P5 | The audit type names (`clippedText`, `contrast`, `dynamicType`, `elementDetection`, `hitRegion`, `traits`) match the current SDK | `skills/accessibility-audit.md` | Unverified |
| P6 | `simctl ui <udid> content_size <value>` and `simctl ui <udid> appearance dark` exist with those exact spellings | check catalog, Tier 2 | Unverified |
| P7 | `simctl status_bar override` is sufficient to make renders byte-stable | ADR-0002 §5 stage 1 | Unverified |
| P8 | GitHub review comments position via `line` + `side` + `start_line` on the current API | vertical slice | Unverified |
| P9 | arXiv 2607.21997 exists and reports ~11pp adoption lift from concrete suggestions | README, comment format | Unverified |

**Do not write code against P3–P8 before checking them.** A confidently hallucinated
`simctl` flag costs an afternoon.

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
- **Which fixture app.** Needs a small, buildable, permissively-licensed open-source iOS
  app to vendor into `eval/fixtures/`. Kevin's call.
- **Depend on `XcodeBuildMCP` / `ios-simulator-mcp`, or vendor the subset?** Leaning
  vendor: we need a handful of `simctl`/`xcodebuild` invocations under our own retry and
  determinism policy, and an MCP dependency in a CI harness is a lot of surface for that.
  Revisit after the vertical slice proves what we actually call.
- **Model routing config format.** Per-skill `model_tier` is decided; the tier→model-id
  mapping lives in config and has no schema yet.
