# ADR-0001: Skill format and dispatch

- **Status:** Proposed
- **Date:** 2026-08-31
- **Deciders:** Kevin Taniguchi

## Context

Design rule 4 of the brief says skills are **files, not code**: a team adds a check by
writing one Markdown file with YAML frontmatter and opening a PR. Design rule 1 says the
**deterministic layer never calls a model**. Those two rules are in tension the moment a
skill needs to do something in the runtime tier, because "boot an iPhone SE, set content
size to AX5, navigate to the checkout screen, screenshot it" is *behavior*, and behavior
in a Markdown file is either (a) a prompt handed to a model, which violates rule 1, or
(b) an embedded script, which violates rule 4.

This ADR resolves that tension, and defines the path from a file on disk to a check that
actually ran.

Constraints inherited from the brief:

- No plugin registration. No Python for skill authors.
- Dispatch decides *which* skills run based on *what the change actually is* — a
  controller, not one mega-prompt.
- Cost has to be bounded per skill, and the runtime tier is expensive enough that
  admission control is a first-class stage, not a `try/except`.
- Precision over recall. A skill that fires on everything is worse than one that never
  fires.

## Decision

### 1. A skill is a Markdown file with a strictly-validated YAML frontmatter header

Discovered from, in increasing precedence:

1. built-ins shipped in `sightline/skills/**.md`
2. `.sightline/skills/**.md` in the target repo
3. `.sightline/config.yml` `skills:` overrides (enable/disable/severity pinning only)

A repo skill whose `id` collides with a built-in **replaces** it wholesale. There is no
merging of frontmatter — partial overrides produce checks nobody can reason about.
Collisions are logged into the trajectory so an author can see they shadowed something.

Frontmatter is parsed into a Pydantic model with `extra="forbid"`. An unknown key is a
**hard load error**, not a warning: a typo'd `trigger:` that silently means "never fires"
is exactly the failure mode that makes people stop trusting the harness. Load errors fail
the skill, not the run; the run continues with the remaining skills and reports the
broken ones in the PR summary.

The Markdown **body** is the model prompt, used only by skills that have a judgment step.
It is never executed and never parsed for control flow.

### 2. `triggers` is a closed vocabulary emitted by the deterministic impact layer

This is the load-bearing decision. Skill authors do not write free text like
`triggers: [when the checkout screen changes]` — that would put a model in the dispatch
path. They select from a versioned enum that `core/impact/` emits as facts about the diff:

```
file_changed              swift_symbol_changed        ui_surface_changed
view_added                view_modified               navigation_graph_changed
info_plist_changed        privacy_manifest_changed    entitlements_changed
localization_changed      asset_catalog_changed       core_data_model_changed
package_resolved_changed  build_settings_changed      test_target_changed
concurrency_annotation_added  permission_api_referenced
```

Adding a trigger means adding an emitter in the deterministic layer *and* a test that
proves it fires on a fixture diff and doesn't fire on a control. The vocabulary is
versioned (`trigger_schema: 1` in frontmatter); a skill written against v1 keeps working
when v2 adds triggers.

Dispatch is a three-stage deterministic filter, in order, each stage recorded in the
trajectory with the reason:

1. **Glob match** — does any changed path match `globs`? Cheap, runs first.
2. **Trigger match** — does the impact layer's emitted trigger set intersect `triggers`?
   Semantics are `any`, not `all`; a skill needing conjunction declares
   `trigger_mode: all`.
3. **Admission** — is the skill's `tier` enabled for this run (see ADR-0003), is its
   `cost_budget_usd` inside the remaining run budget, is its `simulator_matrix` inside
   the repo's configured allowlist?

Only after all three does anything execute, and only *inside* execution does a model see
anything.

### 3. Runtime behavior is composed declaratively from named harness primitives

This is how we keep rule 4 without smuggling a scripting language into YAML. The harness
owns a small, closed set of **capabilities**; a skill declares which ones it needs and
with what parameters. The harness performs them.

```yaml
capabilities:
  - reach_surface:            # navigate to the screens the diff touched
      strategy: impact_derived
  - set_environment:
      content_size: [default, accessibility-extra-extra-extra-large]
      appearance: [light, dark]
  - capture: [screenshot, console_log]
  - run_audit:
      kind: accessibility
      types: [clippedText, contrast, dynamicType, elementDetection, hitRegion, traits]
```

Capabilities are Python, in `runners/`, versioned and tested. Skills are data. A skill
that needs a capability that does not exist fails to load with a message naming the
capability — which is also the feature-request channel: the missing capability name is
the ticket title.

**Escape hatch, deliberately narrow:** a repo may register a `custom_harness` by path,
but only from `.sightline/harnesses/*.py`, only when the repo config sets
`allow_custom_harnesses: true`, and never from a fork's PR. It exists so a team is not
blocked on us; it is not the paved road.

### 4. Frontmatter schema (v1)

See [`sightline/core/skills/frontmatter.py`](../../sightline/core/skills/frontmatter.py)
for the authoritative, executable version. Shape:

```yaml
id: dynamic-type-clipping          # required, kebab-case, unique, stable forever
trigger_schema: 1                  # required
tier: runtime                      # static | build | runtime
globs: ["**/*.swift", "**/*.xib", "**/*.storyboard"]
excludes: ["**/Generated/**", "**/*.generated.swift"]
triggers: [ui_surface_changed, view_modified]
trigger_mode: any                  # any | all
requires_evidence: [screenshot]    # >=1; gate in ADR-0002 enforces it
verifier: differential_render      # named verifier; see ADR-0002
capabilities: [...]                # see above
simulator_matrix: [se-smallest, pro-max, ipad-split]
model_tier: standard               # none | cheap | standard | frontier
cost_budget_usd: 0.15
severity_default: high             # blocking | high | medium | low
max_findings: 3                    # hard cap per run; noise control
owners: ["@checkout-ios"]          # routed on failure, surfaced in comment footer
```

Two fields the brief did not have, and why:

- **`verifier`** — every skill must name the verifier that will try to kill its findings.
  Without this, "requires_evidence" degrades into "attached a screenshot of something."
  ADR-0002 defines the verifier set.
- **`max_findings`** — precision over recall, enforced structurally. A skill that wants
  to post nine comments about the same screen is wrong about something; make it choose.

### 5. `model_tier: none` is legal and encouraged

Most Tier 0 checks are pure static analysis with a deterministic oracle. They should not
touch a model at all. Making `none` a first-class tier keeps the cheap checks cheap and
makes it obvious in review when a skill is spending money.

## Alternatives considered

**Python entry-point plugins.** Loses because it makes the skill author's bar "can write
and package Python," which kills distributed ownership — the exact property that makes
this scale past me. Also an arbitrary-code supply chain in CI.

**One mega-prompt with all the rules.** Loses on cost, on attribution (which rule fired?),
on per-skill budgets, and on the Uber lesson that dispatch is a controller. Also
unfalsifiable: you cannot regression-test a paragraph.

**Free-text triggers interpreted by a cheap model.** Genuinely tempting — it makes
authoring frictionless. Loses on rule 1: dispatch becomes nondeterministic, the trajectory
stops being replayable, and eval scores stop being comparable across runs. Revisit only
with a cached, pinned classifier and a recorded decision, never live.

**Embedded shell/JS in frontmatter.** Loses on the same supply-chain grounds as plugins,
with worse ergonomics.

## Consequences

**Good.** A team ships a check with one file and one PR. Dispatch is replayable from the
trajectory — you can diff why a skill fired last week and not today. Cost is knowable
before anything boots. Skills are trivially unit-testable: fixture diff in, expected
`fired: yes/no` + reason out.

**Bad.** The capability vocabulary is now a bottleneck we own. Every novel runtime check
needs harness work before a skill can express it, and skill authors will feel that. We are
choosing that friction over an executable-YAML mess; the mitigation is that missing
capabilities are logged by name so the backlog writes itself.

**Also bad.** Closed-vocabulary triggers mean the impact layer's precision *is* the
product's precision. If `ui_surface_changed` over-fires, every runtime skill over-fires
with it. The impact layer needs its own eval fixtures from day one, separate from the
end-to-end corpus.

## Open questions

- Do skills compose? (Skill A's finding as Skill B's input.) Defer. If it turns up, model
  it as a capability, not as skill-to-skill imports.
- Versioning a skill's *body* for addressal tracking: when the prompt changes, is a prior
  comment's fingerprint still comparable? Leaning yes — the fingerprint is claim-derived,
  not prompt-derived (ADR-0002) — but the eval corpus needs to prove it.
- Should `owners` gate posting (only the owning team sees it until the skill graduates)?
  Probably yes as a rollout mechanism. Needs a `maturity: experimental|stable` field.
