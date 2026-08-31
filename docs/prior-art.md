# Prior art

What we borrowed, from where, and what specifically to take. Read before designing
anything — most of the hard thinking is already public.

| Source | What to take |
|---|---|
| Uber `uReview` (engineering blog) | The five architectural properties below. The reason this repo exists in this shape. |
| `alibaba/open-code-review` | The deterministic/agent split. Engineering owns file selection, bundling, rule matching, and comment positioning; the model only does judgment. This is our spine. |
| `Agent-Field/pr-af` | The falsifiability gate — a finding must survive programmatic verification before it can be posted. Generalized into ADR-0002. |
| `imbue-ai/vet` | Dedupe + filter as a *named pipeline stage*, not a prompt instruction. Also: reviews agent trajectories, not just diffs. |
| `moonrunnerkc/swarm-orchestrator` | Eleven "agent faked done" cheat patterns — swallowed errors, stripped assertions, added suppressions, relaxed tests. Nearly all have iOS analogues; they are the Tier 0 cheat-pattern checks. |
| `The-PR-Agent/pr-agent` | `SKILL.md` convention; a clean forge-adapter layer across five forges. |
| `withmartian/code-review-benchmark` | Its *online* mode measures which bot suggestions developers actually implemented. That is our north-star metric, not cost or NPS. |
| `reviewdog/reviewdog` | Diff-to-line comment positioning across forges. Study how it maps tool output onto hunks; do not rebuild it badly. |
| `getsentry/XcodeBuildMCP`, `joshuayoes/ios-simulator-mcp` | Agent-facing wrappers around `xcodebuild` / `simctl`. Open question: depend, or vendor the subset. |
| `a7ex/xcresultparser` | `.xcresult` parsing prior art, including how it survived schema changes. |
| `pointfreeco/swift-snapshot-testing` | Snapshot comparison prior art. We reject committed baselines (ADR-0002 §5) but should consume a repo's existing ones as evidence. |
| arXiv 2607.21997, *"Go Home Copilot, You're Drunk"* | 54,713 real agent review comments. A concrete code suggestion raises adoption ~11pp; longer comments do worse. The comment format is designed around this. ⚠️ unverified — see [open-questions.md](open-questions.md) P9. |

## The five properties from uReview

These had to be in the architecture from commit #1, not bolted on later. Where each one
landed:

1. **Multi-agent dispatch, not one prompt.** A controller decides which skills run based
   on what the change actually is. → ADR-0001 §2, the three-stage deterministic filter.
2. **Distributed ownership.** Teams author their own skills as files in their own repo.
   The harness is shared; the rules are not. → ADR-0001 §1 discovery precedence.
3. **Addressal observability.** Cost and NPS are vanity metrics. The real ones are
   addressal rate (Uber sits around 67%), reply sentiment, and agent trajectories.
   → `core/telemetry/`, instrumented before there is a single user.
4. **Precision over recall.** A reviewer that posts 40 comments and is right 12 times
   gets muted. → ADR-0002's binary gate, plus `max_findings` in ADR-0001 §4.
5. **Forge-agnostic core.** GitHub first, but the review engine never imports a GitHub
   type. → `adapters/forge/`, and `ForgeAdapter.post_review` takes `VerifiedFinding`.
