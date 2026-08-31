# Roadmap

One thing end to end before ten things halfway.

---

## Now: planning (this pass)

- [x] `PROMPT.md` — the founding brief, preserved verbatim
- [x] ADR-0001 skill format and dispatch
- [x] ADR-0002 evidence and the falsifiability gate
- [x] ADR-0003 runtime tier execution model
- [x] Finding schema and skill frontmatter schema, as code
- [x] Check catalog, prior art, open questions
- [x] README written as if the project were already good
- [x] Nine decisions taken — see [decisions.md](decisions.md)
- [ ] **Verify P1–P9** in [open-questions.md](open-questions.md). Blocks everything below (D1)
- [ ] Build the minimal SwiftUI fixture into `eval/fixtures/` with seeded defects (D2)
- [ ] Push to a private GitHub repo; eval PRs modify `eval/fixtures/` (D9)

## Next: the vertical slice

The whole slice, nothing beside it:

> A PR touching a SwiftUI view → impact analysis identifies the affected screen → boot
> one simulator → run an accessibility audit against that screen → produce one `Finding`
> with a screenshot artifact → post one correctly-positioned GitHub review comment →
> write the trajectory and addressal-ledger records.

Build order, each step independently testable:

1. `core/diff/` — PR diff → changed files, symbols, targets. Fixture-driven.
2. `core/impact/` — changed symbols → trigger set. **Its own eval fixtures**, separate
   from the end-to-end corpus: the impact layer's precision *is* the product's precision.
3. `core/skills/` — load, validate, dispatch. Assert fired/not-fired with a reason.
4. `runners/xcode/` — build; `xcresult.py` adapter tested against a committed real bundle.
   Our own thin wrappers, not an MCP dependency (D4).
5. `runners/simulator/` — boot with `bootstatus -b`, retry-with-erase, deterministic
   device state, capture.
6. UI test target discovery/injection into a **scratch clone**, never the checkout.
7. `core/evidence/` — content-addressed store with the redaction pass in front of it.
8. `core/verify/` — the `structured_oracle` verifier and the gate.
9. `adapters/forge/github.py` — comment positioning. **Test against a real PR early**;
   getting `line`/`side`/`start_line` wrong is instantly disqualifying.
10. `core/telemetry/` — trajectory JSON + addressal ledger (SQLite behind an interface).
11. `cli` — `sightline review --pr <url>`.

**Done when:** `sightline review --pr <url>` against the fixture repo posts one
true-positive accessibility comment with a screenshot attached, and writes a trajectory
JSON you can read.

## Then: eval

Three PRs against the vendored fixture — two with known real defects, one clean. Score
precision and recall. Three is enough to keep us honest; the corpus grows from there.

The clean PR is the important one. It is the regression test for "posts nothing."

## Then: Tier 3 before more of Tier 2

The before/after screenshot table is the only thing in the catalog that produces value on
a PR with zero findings. It is also the cheapest way to get a team to leave the bot
installed long enough to accumulate addressal data. Ship it ahead of the rest of Tier 2.

## Then: breadth

- Tier 0 in bulk — cheap, ungated, `model_tier: none`, runs on every PR
- Dynamic Type and appearance matrices (`differential_render`, the second verifier class)
- Console diagnostics harvest (`reexecution`, the third verifier class)
- Base-build cache — the single largest cost lever in ADR-0003

Deliberately deferred until addressal data exists: the performance metrics (noise floor
unmeasured), the full device matrix, and the nightly-vs-PR-time split.

## Metrics we watch from day one

Not cost. Not NPS.

- **Addressal rate** — did the flagged line change, or the thread resolve? (Uber ~67%)
- **Reply sentiment** — what did the human say back?
- **Suppression log** — what did the gate drop, and was it right to? This is the roadmap.
- **Precision on the eval corpus** — recall is secondary and stays that way.
