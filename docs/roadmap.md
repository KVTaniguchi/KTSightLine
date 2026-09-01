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
- [x] **Verified P1–P9** (D1) — five confirmed, four wrong; corrections applied.
      See [verification-2026-08-31.md](verification-2026-08-31.md)
- [x] Contract invariants under test — 22 tests in `tests/test_contracts.py` covering
      fingerprint line-independence, the gate-only `VerifiedFinding` constructor,
      evidence minimums, `start_side`, and strict frontmatter
- [x] **Built the SwiftUI fixture** (D2) — `eval/fixtures/CheckoutDemo`, 5 screens,
      5 seeded defects, 1 clean control, hand-maintained `.xcodeproj`, builds and runs
      its UI tests on iPhone SE / iOS 26.5. Ground truth is **observed**, not intended
- [x] Pushed to [KVTaniguchi/KTSightLine](https://github.com/KVTaniguchi/KTSightLine) (D9, public per D10)
- [x] Re-scoped the first slice around D-004, not D-001 — see below
- [x] **Deterministic layer built and tested** — 68 tests. `diff → symbols → triggers →
      dispatch` runs end to end on the committed fixture PR: `accessibility-audit`
      fires, `missing-usage-description` correctly stays quiet

## Next: the vertical slice

The whole slice, nothing beside it:

> A PR touching a SwiftUI view → impact analysis identifies the affected screen → boot
> one simulator → run an accessibility audit against that screen → produce one `Finding`
> with a screenshot artifact → post one correctly-positioned GitHub review comment →
> write the trajectory and addressal-ledger records.

**Re-scoped after building the fixture.** The slice's finding is **D-004** (an
image-only button whose derived label is the SF Symbol name), not D-001 (Dynamic Type
clipping). Two reasons, both measured:

- `performAccessibilityAudit` does not report `textClipped` for SwiftUI views that are
  visibly clipped at AX5 — four shapes tried, none fired. Clipping needs
  `differential_render`, which is a later verifier.
- D-004 arrives with a populated `identifier` and `frame`, so it can be anchored to a
  file and line. Several audit issues — including the contrast defect we seeded on
  purpose — arrive with `element = nil` and cannot currently be anchored at all.

D-004 is the one defect that is detected, attributable, and mechanically fixable
(`.accessibilityLabel("Help")`). That makes it the honest first end-to-end case.

Build order, each step independently testable:

1. ~~`core/diff/`~~ **done** — unified-diff parser with per-side line arithmetic, plus
   `swift.py`, a brace-tracking outline answering "what declaration encloses line N" for
   ADR-0002 fingerprints. Handles adds, deletes, renames, multi-hunk, `\ No newline`.
   `commentable_lines` is the P8 positioning contract.
2. ~~`core/impact/`~~ **done** — `analyze(diff) -> ImpactReport`. Emits the closed
   trigger vocabulary from paths and **added lines only**, each with `TriggerEvidence`
   naming why it fired. Tested on what must *not* fire as hard as on what must.
3. ~~`core/skills/`~~ **done** — `loader.py` plus `dispatch.py`, the three-stage filter.
   Every skill gets a `Decision` with a reason; budget denials name the numbers (D6).
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
