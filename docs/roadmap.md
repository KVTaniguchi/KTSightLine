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
4. ~~`runners/xcode/`~~ **done (parsing)** — `xcresult.py` splits shelling-out from pure
   parsing, pins `--schema-version`, and is tested against JSON captured from real runs
   (plus an Xcode-gated schema-drift test). `xcodebuild` build/test invocation is still
   to come.
5. ~~`runners/simulator/`~~ **done** — `bootstatus -b` (never sleep), retry-on-erased-
   device, `freeze_status_bar` pinning every mutable element, content size / appearance /
   increase-contrast, screenshot, and a `revoke_privacy` that refuses `camera` with a
   message instead of silently pretending.
6. ~~UI test target discovery/injection~~ **done** — `project.py` reads and edits
   `.xcodeproj` (JSON in via `plutil`, XML plist out — `xcodebuild` accepts it, and the
   lost formatting is fine because we only ever edit a scratch clone). `injection.py`
   discovers a UI test target, **generates one when the project has none**, or degrades
   with a message naming the config key. Proven by building and running both paths.
7. ~~`core/evidence/`~~ **done** — content-addressed store; redaction runs *before*
   addressing, so unmasked bytes never reach disk (asserted, not assumed). An
   undecodable capture is refused with a typed error rather than crashing the run.
8. ~~`core/verify/`~~ **done** — `structured_oracle` looks up an `oracle_key` in the
   tool's own output rather than reading the claim, so an invented finding dies here.
   Unanchorable findings are dropped and *counted* (OQ-FIXTURE-1).
9. ~~`adapters/forge/github.py`~~ **done (code + read-only verification)** — anchors are
   validated against the parsed diff before any network call; `position` is never sent.
   Cross-checked against 26 real accepted GitHub comments: 25/26 agree, and the
   disagreement found a real bug in our LEFT-side handling. **Posting a comment is still
   unverified — it needs authorization.**
10. ~~`core/telemetry/`~~ **done** — trajectory JSON with a suppression summary that
    carries counts and reasons only (D8), plus a SQLite addressal ledger whose rate
    excludes open comments from the denominator.
11. ~~`cli`~~ **done** — `sightline review` (dry run by default), `check-anchors`, and
    `diff`. Posting is opt-in behind `--post`.

**Done when:** `sightline review --pr <url>` against the fixture repo posts one
true-positive accessibility comment with a screenshot attached, and writes a trajectory
JSON you can read.

**Reached 2026-08-31**, with one caveat. The full chain ran against
[PR #1](https://github.com/KVTaniguchi/KTSightLine/pull/1) and posted a verified,
correctly-positioned comment on the defect line — build → boot → audit → parse →
evidence → gate → anchor validation → post → ledger → trajectory
([sample](examples/trajectory-slice.json)).

The caveat from that first run is now closed: `sightline audit` orchestrates the whole
runtime tier in one command — scratch clone, target discovery or generation, driver
injection, deterministic simulator state, build, run, parse, gate. What remains is
joining `audit` to `review` so a single invocation goes from PR URL to posted comment.

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
