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
