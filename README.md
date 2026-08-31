# Sightline

**Every AI code reviewer reads the diff. Sightline runs the app.**

Sightline builds your PR branch, boots simulators, drives the app, captures what it sees,
and reviews the evidence. A finding that is not backed by an artifact — a screenshot, a
log line, a symbolicated crash, an `.xcresult` bundle, a measured metric — does not get
posted.

That constraint is the product, not a slogan. The type that the GitHub adapter accepts
cannot be constructed without a passing verification.

> **Status: pre-alpha.** This repo currently contains the plan — three ADRs, the two
> schemas everything hangs off, and the check catalog. No harness yet. Start with
> [`PROMPT.md`](PROMPT.md), then [`docs/adr/`](docs/adr/).

---

## Why this exists

Diff-reading review works for backend code, where the diff mostly *is* the behavior. On
iOS the expensive bugs are the ones you only find by building the branch, booting a
simulator, and looking at it:

- text that clips at Accessibility XXXL but not at default size
- a hardcoded `UIColor` that disappears in dark mode
- a constraint conflict that logs to the console and is never read by anyone
- a Core Data migration that crashes only when you install over the previous build
- a missing `NSCameraUsageDescription` that crashes on first tap
- a permanent spinner, because the new screen has no error state and nobody tested offline
- a data race Thread Sanitizer catches in ten seconds and code review never will

None of those are visible in a diff. All of them are visible in a screenshot or a log.

## What a comment looks like

````
⚠️ Clipped at Accessibility XXXL — `CheckoutSummaryView.swift:142`

"Estimated delivery" truncates to "Estimated de…" at AX5 on iPhone SE.
Evidence: screenshot-ax5-se.png · accessibility audit: clippedText

```suggestion
    .lineLimit(nil)
    .fixedSize(horizontal: false, vertical: true)
```
````

One claim. One evidence link. A suggestion when the fix is mechanically derivable.
No style comments, ever. No "consider possibly."

The format is not taste. [arXiv 2607.21997](docs/prior-art.md) measured 54,713 real agent
review comments: a concrete code suggestion raises adoption by ~11 percentage points, and
longer comments do worse.

## How it works

```
PR ──▶ static job (Linux, every PR, ~1–2 min)
       diff parse → impact analysis → tier-0 skills → post
                          │
                          └── emits trigger set ──▶ runtime job (macOS, GATED)
                                                    build base + head → boot matrix
                                                    → drive → capture → verify → post
```

Four rules the architecture is built on:

1. **The deterministic layer never calls a model.** Which files matter, which targets
   rebuild, which screens a change reaches, which simulators boot, where a comment lands
   on a diff — that is engineering. If a model is doing it, it is in the wrong layer.
2. **Every finding carries evidence, and evidence is not enough.** A screenshot proves a
   screenshot was taken. A *verifier* independently confirms the claim from the artifacts,
   and it is adversarial: ambiguity resolves to reject. See
   [ADR-0002](docs/adr/0002-evidence-and-verification.md).
3. **Fingerprints are content-derived, never line-derived.** A bot that re-posts the same
   comment on every push is dead on arrival.
4. **Precision over recall.** Suppression is a feature. Everything we drop is logged, and
   that log is the roadmap.

## Adding a check

A skill is one Markdown file. No Python, no plugin registration.

```yaml
---
id: dynamic-type-clipping
trigger_schema: 1
tier: runtime
globs: ["**/*.swift", "**/*.xib", "**/*.storyboard"]
triggers: [ui_surface_changed]
requires_evidence: [screenshot]
verifier: differential_render
capabilities:
  - reach_surface: {strategy: impact_derived}
  - set_environment: {content_size: [default, accessibility-extra-extra-extra-large]}
  - capture: [screenshot]
simulator_matrix: [se-smallest]
model_tier: standard
cost_budget_usd: 0.15
---

# Dynamic Type clipping

<the markdown body is the model's instructions — never executed, never parsed for
control flow>
```

Drop it in `.sightline/skills/` in your repo and open a PR. The harness is shared; the
rules are yours. Full schema:
[`sightline/core/skills/frontmatter.py`](sightline/core/skills/frontmatter.py),
rationale: [ADR-0001](docs/adr/0001-skill-format-and-dispatch.md).

Worked examples: [`skills/accessibility-audit.md`](skills/accessibility-audit.md) (runtime,
model-assisted) and [`skills/missing-usage-description.md`](skills/missing-usage-description.md)
(static, `model_tier: none`, spends nothing).

## What it will not do

- It is not a general-purpose code reviewer. If a finding would apply equally to a Django
  repo, it is out of scope.
- It is not a linter. SwiftLint and SwiftFormat exist; Sightline consumes their output as
  evidence rather than reimplementing them.
- It does not post style comments.
- It does not generate Swift refactors of your source. Suggestions are minimal, surgical,
  and mechanically checkable.
- It never blocks a merge. The runtime tier fails open, always.

## Docs

| | |
|---|---|
| [`PROMPT.md`](PROMPT.md) | The founding brief. Thesis, constraints, and the decisions that are not up for relitigation. |
| [`docs/adr/`](docs/adr/) | Why the system is shaped the way it is. Read 0002 first if you only read one. |
| [`docs/check-catalog.md`](docs/check-catalog.md) | Every check, tiered, with status. The map. |
| [`docs/roadmap.md`](docs/roadmap.md) | The one vertical slice, then what. |
| [`docs/prior-art.md`](docs/prior-art.md) | What we borrowed and from where. |
| [`docs/open-questions.md`](docs/open-questions.md) | Unverified premises and unsettled decisions, with owners. |

## Requirements

Python 3.12+, `uv`, Xcode 26+, macOS runners for the runtime tier. GitHub first; the
review engine never imports a GitHub type, so other forges are an adapter away.
