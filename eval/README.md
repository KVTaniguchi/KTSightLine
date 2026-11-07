# eval

Three cases against the vendored fixture: two with known defects, one clean. Scored for
precision and recall.

```bash
uv run sightline eval --udid <simulator-udid>
```

Exits non-zero on any false positive or false negative, so it works as a gate on our own
PRs.

## Current score

```
[PASS] 001-unlabelled-icon-button   4 audit issues → 1 finding  ✔ sufficientElementDescription:checkout.options
[PASS] 002-undersized-tap-target    4 audit issues → 1 finding  ✔ hitRegion:payment.editCards
[PASS] 003-clean-accessible-button  3 audit issues → 0 findings

precision 1.00  recall 1.00  (tp 2 · fp 0 · fn 0)
```

**Read that with the sample size in mind.** Three cases, one skill, one device, one
content size. It says the pipeline works end to end and that the clean case stays quiet.
It says nothing yet about behaviour on a real app. The corpus grows from here; the number
only starts meaning something in the dozens.

## A real app: Nearby Meows

`eval/nearby-meows/` holds two cases against
[KTCatPokemon](https://github.com/KVTaniguchi/KTCatPokemon) rather than the vendored
fixture. Each `case.yml` names a `base:` commit; the harness exports the target repo at
that commit with `git archive`, applies the patch, and reads the Sightline config from
`config.yml` next to the cases (the app's repo carries none).

```bash
eval/run-nearby-meows.sh [path/to/KTCatPokemon] [simulator-udid]
```

```
[PASS] 001-seeded-unlabelled-help-button   14 audit issues → 1 finding  ✔ sufficientElementDescription:dex.help
[PASS] 002-clean-labelled-help-button      13 audit issues → 0 findings
precision 1.00  recall 1.00  (tp 1 · fp 0 · fn 0)
```

The two patches are identical except for one `.accessibilityLabel` line. The 13 issues
both share are the Dex screen's pre-existing sub-threshold warnings.

## The acceptance corpus

`eval/acceptance/` scores `sightline accept` ([ADR-0004](../docs/adr/0004-acceptance-tier.md)),
separately, so the review score above keeps its meaning.

```bash
uv run sightline eval --suite acceptance --udid <simulator-udid>
```

```
expected \ observed   pass   fail   not_verifiable
pass                      6      0      0
fail                      0      4      0
not_verifiable            0      0      6

false passes 0 · false fails 0
```

Measured 2026-10-11, Xcode 27.1, iOS 26.5 simulator. Exits non-zero on any false pass,
false fail, wrong `not_verifiable` reason, missing expected `actual`, or criterion the
case does not score.

| Case | Why it exists |
|---|---|
| `001-checkout-baseline` | The unmodified fixture: two passes, then one criterion per `not_verifiable` path that has nothing to do with the app being wrong — manual, `network_call`, a control the build lacks, and `cart.removeItem`, which three rows share |
| `002-subtotal-ignores-quantity` | The brief's example criteria against a patch that drops quantity: AC1 must fail with `Subtotal $377.00` recorded |
| `003-fixture-launch-args` | A `fixtures:` entry (in the case's own config) starts the app with a one-line cart; the patch is what makes the app honour it |
| `004`–`006` | `order_submitted` logged once, twice, never: pass, fail with 2, fail with 0 |
| `007-app-silences-its-logs` | The app logs the event but redirects stdout/stderr. Must be `log_unavailable`: the negative control for the liveness rule |

A case is `case.yml` (`criteria:`, optional `patch:` and `config:`, and `expected:` per
criterion ID with `verdict`, optional `reason` and `actual`), plus its patch. Generate
patches the same way as below.

## What each case is for

| Case | Why it exists |
|---|---|
| `001-unlabelled-icon-button` | The canonical missing-label defect, and a different claim template + suggestion path from 002 |
| `002-undersized-tap-target` | Geometry rather than labelling. The control **is** correctly labelled — a check that only looked for missing labels would call it clean |
| `003-clean-accessible-button` | The regression test for "posts nothing". It **touches a UI surface**, so the runtime tier fires and pays a full build and simulator run, and must still produce nothing. A no-op diff would not test that |

## Adding a case

1. `mkdir eval/corpus/00N-short-name`
2. Generate a real diff — copy the fixture, `git init`, make the change, `git diff > change.patch`. Hand-written patches drift from reality.
3. Write `case.yml`:

```yaml
id: 00N-short-name
title: One line
kind: true_positive        # or: clean
patch: change.patch
expected:
  - audit_type: hitRegion
    identifier: some.identifier
    file: CheckoutDemo/SomeView.swift
```

4. Run it. If it does not fire, find out why before adjusting the expectation — the
   two lessons below were both learned that way.

## Two things the corpus taught us while being built

**SF Symbols' built-in accessibility descriptions are uneven.** `square.and.arrow.up`,
`ellipsis.circle`, and `line.3.horizontal.decrease` all produce readable labels and do
*not* fire the audit; `questionmark.circle` does. "Image-only button" is not a synonym for
"defect", and a static check that assumed so would false-positive on stock iconography.

**Diff alignment can attribute pre-existing code to a PR.** The first version of case 001
inserted a `.toolbar` block immediately before an existing one. Git aligned them so the
*pre-existing* block was marked added and the new one context — so the harness correctly
reported a finding on pre-existing code. Nothing was wrong with the scoping rule; the
case was badly built. Worth knowing that `added_lines` is a good scope heuristic, not a
guarantee, when a change inserts a near-duplicate block.
