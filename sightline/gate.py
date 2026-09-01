"""The runtime-tier gate (ADR-0003 §2).

Deterministic, ordered, and cheap. Every stage records *why*, because "the bot said
nothing" has to be distinguishable from "the bot did not run" — conflating them is what
makes silence untrustworthy.

**Fail open.** A gate that cannot decide admits the run. A runtime job that fails,
times out, or cannot get a runner reports neutral and never blocks a merge.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch

from sightline.adapters.forge.base import PullRequest
from sightline.core.impact.analyzer import ImpactReport
from sightline.core.skills.frontmatter import Tier
from sightline.core.skills.loader import Skill
from sightline.core.telemetry.trajectory import GateTrace

FORCE_LABEL = "sightline:full"
SKIP_LABEL = "sightline:skip"


@dataclass(frozen=True)
class GateInputs:
    pr: PullRequest
    impact: ImpactReport
    changed_paths: list[str]
    skills: list[Skill]
    budget_remaining_usd: float = 1.0


def _runtime_skills(skills: list[Skill]) -> list[Skill]:
    return [s for s in skills if s.frontmatter.tier is Tier.RUNTIME]


def decide(inputs: GateInputs) -> GateTrace:
    """Should the runtime tier run for this PR?

    Order matters: the cheapest checks run first, and a human override beats every
    automatic decision in *both* directions.
    """
    runtime = _runtime_skills(inputs.skills)
    if not runtime:
        return GateTrace(
            runtime_enabled=False,
            reason="no runtime-tier skills are enabled",
            stage="triggers",
        )

    # A human override always wins, both ways. Someone who has looked at the PR knows
    # more than the gate does.
    if SKIP_LABEL in inputs.pr.labels:
        return GateTrace(runtime_enabled=False, reason=f"`{SKIP_LABEL}` label", stage="label")

    forced = FORCE_LABEL in inputs.pr.labels

    # D7: fork PRs cannot read the build cache or repo credentials. Policy, not failure —
    # and not overridable, because the credentials genuinely are not there.
    if inputs.pr.is_fork:
        return GateTrace(
            runtime_enabled=False,
            reason="fork PR — no build cache or credentials",
            stage="fork",
        )

    if forced:
        return GateTrace(runtime_enabled=True, reason=f"`{FORCE_LABEL}` label", stage="label")

    globs = [g for s in runtime for g in s.frontmatter.globs]
    excludes = [e for s in runtime for e in s.frontmatter.excludes]
    matched = [
        p
        for p in inputs.changed_paths
        if any(fnmatch(p, g) for g in globs) and not any(fnmatch(p, e) for e in excludes)
    ]
    if not matched:
        return GateTrace(
            runtime_enabled=False,
            reason="no changed path matches a runtime skill's globs",
            stage="path_globs",
        )

    declared = {t for s in runtime for t in s.frontmatter.triggers}
    if not declared & inputs.impact.triggers:
        return GateTrace(
            runtime_enabled=False,
            reason=(
                "no runtime trigger fired "
                f"(diff emitted {sorted(t.value for t in inputs.impact.triggers)})"
            ),
            stage="triggers",
        )

    cheapest = min((s.frontmatter.cost_budget_usd for s in runtime), default=0.0)
    if cheapest > inputs.budget_remaining_usd:
        return GateTrace(
            runtime_enabled=False,
            reason=(
                f"budget exhausted: cheapest runtime skill needs ${cheapest:.2f}, "
                f"${inputs.budget_remaining_usd:.2f} remains"
            ),
            stage="budget",
        )

    return GateTrace(
        runtime_enabled=True,
        reason=f"{len(matched)} path(s) matched a runtime skill",
        stage="ok",
    )
