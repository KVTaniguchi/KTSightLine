# Decision log

Decisions made outside an ADR, or that narrow one. Newest last. An entry here that
changes an ADR's content is also patched into that ADR — this file is the chronology,
the ADR is the reasoning.

## 2026-08-31 — planning session

| # | Decision | Why | Lands in |
|---|---|---|---|
| D1 | **Verify all nine unverified premises (P1–P9) before writing code against them** | Every ADR rests on them, and ADR-0003's cost model is meaningless if the runner multiplier is wrong. **Done 2026-08-31: five confirmed, four wrong.** | [verification-2026-08-31.md](verification-2026-08-31.md) |
| D2 | **Fixture is a purpose-built minimal SwiftUI app**, vendored to `eval/fixtures/`. **Built and verified 2026-08-31** — see [`eval/fixtures/CheckoutDemo/README.md`](../eval/fixtures/CheckoutDemo/README.md) | Builds in seconds, no license question, and seeded defects give exact ground truth for precision/recall. Explicitly *not* a proof that we survive a real project's build graph — a real repo becomes target #2 before we claim that. | [roadmap.md](roadmap.md) |
| D3 | **Apache-2.0** | Explicit patent grant is what gets a CI tool through corporate legal review. | `LICENSE`, `NOTICE` |
| D4 | **Vendor the `xcodebuild`/`simctl` subset; no MCP dependency** | We need ~a dozen invocations, and every one carries ADR-0003's retry-with-erase, `bootstatus` gating, and deterministic-device-state policy that an upstream wrapper won't have. Also keeps a Node/MCP process out of the CI job. | ADR-0003 §4, `runners/` |
| D5 | **One model, vary effort.** `claude-opus-5` for every non-`none` tier; `cheap→effort: low`, `standard→effort: high`, `frontier→effort: xhigh` | Prompt caches are model-scoped, so a model cascade forfeits cache reuse across tiers — and our stable prefix (system prompt + skill body) is exactly what caches well. Measure the capable model at low effort before building a cascade. | ADR-0001 §5 |
| D6 | **Budgets: $0.50 per PR, $20/day per repo** | Tight on purpose: forces `model_tier: none` discipline, and most Tier 0 checks should be `none` anyway. **Mitigation required** — an admission denial must be loud in the run summary, naming the skill and the budget. A silently skipped skill is the failure mode this cap risks. | ADR-0001 §2, ADR-0003 §2 |
| D7 | **Runtime tier does not run on fork PRs** | No base-build cache, no credentials. Reported as policy (`skipped (fork PR — no cache or credentials)`), not failure. Keeps us off `pull_request_target` entirely, which is the right call for a tool that reads other people's code. | ADR-0003 §7 |
| D8 | **Suppressed findings: counts and reasons only, collapsed** | `3 findings suppressed — 2 missing_evidence, 1 no_baseline`. Transparency without putting unverified claim text in front of a human; showing the claims in a fold is posting unverified findings with extra steps. | ADR-0002 §3 |
| D9 | **Eval PRs are real PRs against the KTSightLine repo**, modifying `eval/fixtures/` | Real diffs, real hunks, real comment positioning. Forces the app-not-at-repo-root case from day one instead of hardcoding it away. | [roadmap.md](roadmap.md) |
| D11 | **Findings the audit cannot attribute to an element are dropped, not posted** | Resolves OQ-FIXTURE-1. On the fixture's Cart screen that discards 3 of 5 real failures — a real recall cost, taken knowingly. Rejected alternatives: a file-level comment (`subject_type: "file"`, which does work) and guessing the view's `body` line. Precision over recall, and a comment on a wrong line is disqualifying. Every drop is counted under `unanchorable` so the cost stays measured. | ADR-0002, `core/verify/gate.py` |
| D12 | **Authorized one test PR and one posted comment** on `KVTaniguchi/KTSightLine` | The only way to prove a comment lands on the line we aimed at. Read-only cross-checking got us 25/26 against real accepted comments and found a real bug, but it cannot prove placement on write. | this repo |
| D10 | **The repo is public** — supersedes D9's "private" | `KVTaniguchi/KTSightLine` already existed as a public repo with an Apache-2.0 LICENSE. Building in the open from commit one. Accepted consequences: the plan is visible before the harness works, the README describes a project that does not run yet, and eval fixture PRs are public. | this repo |
