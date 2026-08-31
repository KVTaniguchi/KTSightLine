# Architecture Decision Records

One file per decision. Numbered, immutable once `Accepted` — a decision that turns out
wrong gets a *new* ADR that supersedes the old one, and the old one gets a
`Superseded by ADR-NNNN` line. We do not edit history; the reasoning that was wrong is
the most useful thing in the file.

| ADR | Title | Status |
|---|---|---|
| [0001](0001-skill-format-and-dispatch.md) | Skill format and dispatch | Proposed |
| [0002](0002-evidence-and-verification.md) | Evidence and the falsifiability gate | Proposed |
| [0003](0003-runtime-tier-execution-model.md) | Runtime tier execution model | Proposed |

## Template

```markdown
# ADR-NNNN: <title>

- **Status:** Proposed | Accepted | Superseded by ADR-NNNN
- **Date:** YYYY-MM-DD
- **Deciders:** <who>

## Context
What forces are in play. What breaks if we do nothing.

## Decision
The thing we are doing, stated so a reader can implement it without asking follow-ups.

## Alternatives considered
Each one with the reason it lost. If an alternative lost on taste rather than
evidence, say so.

## Consequences
What this buys, what it costs, what it makes harder later. Include the bad ones.

## Open questions
Things this ADR does not settle, with an owner.
```
