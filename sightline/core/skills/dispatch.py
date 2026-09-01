"""The three-stage deterministic dispatch filter (ADR-0001 §2).

globs -> triggers -> admission. Every skill gets a recorded decision with a reason,
because a skill that quietly did not run is the failure mode that makes the bot's
silence untrustworthy (ADR-0001 §2, decision D6).

No model is consulted here. Dispatch must be replayable from the trajectory.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from fnmatch import fnmatch

from sightline.core.impact.analyzer import ImpactReport
from sightline.core.skills.frontmatter import Maturity, ModelTier, Tier
from sightline.core.skills.loader import Skill


class Outcome(StrEnum):
    FIRED = "fired"
    NO_GLOB_MATCH = "no_glob_match"
    NO_TRIGGER_MATCH = "no_trigger_match"
    TIER_DISABLED = "tier_disabled"
    OVER_BUDGET = "over_budget"
    MATRIX_NOT_ALLOWED = "matrix_not_allowed"
    NOT_MATURE = "not_mature"


@dataclass(frozen=True)
class Decision:
    skill_id: str
    outcome: Outcome
    reason: str
    matched_paths: tuple[str, ...] = ()
    matched_triggers: tuple[str, ...] = ()
    reserved_usd: float = 0.0

    @property
    def fired(self) -> bool:
        return self.outcome is Outcome.FIRED


@dataclass(frozen=True)
class RunPolicy:
    """What this run is willing to do. Comes from config plus the ADR-0003 gate."""

    enabled_tiers: frozenset[Tier]
    pr_budget_usd: float = 0.50  # D6
    simulator_allowlist: frozenset[str] = frozenset({"se-smallest"})
    allow_experimental: bool = False

    @classmethod
    def static_only(cls, **kw) -> RunPolicy:
        return cls(enabled_tiers=frozenset({Tier.STATIC}), **kw)

    @classmethod
    def full(cls, **kw) -> RunPolicy:
        return cls(enabled_tiers=frozenset(Tier), **kw)


def _matches_globs(path: str, globs: list[str], excludes: list[str]) -> bool:
    if any(fnmatch(path, pattern) for pattern in excludes):
        return False
    return any(fnmatch(path, pattern) for pattern in globs)


def dispatch(
    skills: list[Skill],
    diff_paths: list[str],
    impact: ImpactReport,
    policy: RunPolicy,
) -> list[Decision]:
    """Decide which skills run. Returns one Decision per skill, in input order.

    Budget is reserved in declaration order and is not re-sorted by priority: a
    deterministic order is worth more than a marginally better packing, because the
    trajectory has to be replayable.
    """
    decisions: list[Decision] = []
    remaining = policy.pr_budget_usd

    for skill in skills:
        fm = skill.frontmatter

        # --- stage 1: globs (cheapest, runs first) ---
        matched = tuple(p for p in diff_paths if _matches_globs(p, fm.globs, fm.excludes))
        if not matched:
            decisions.append(
                Decision(fm.id, Outcome.NO_GLOB_MATCH, "no changed path matched globs")
            )
            continue

        # --- stage 2: triggers ---
        declared = {t for t in fm.triggers}
        hit = declared & impact.triggers
        satisfied = (declared <= impact.triggers) if fm.trigger_mode == "all" else bool(hit)
        if not satisfied:
            missing = sorted(t.value for t in declared - impact.triggers)
            decisions.append(
                Decision(
                    fm.id,
                    Outcome.NO_TRIGGER_MATCH,
                    f"trigger_mode={fm.trigger_mode}; unmet {missing}",
                    matched_paths=matched,
                )
            )
            continue
        triggers = tuple(sorted(t.value for t in hit))

        # --- stage 3: admission ---
        if fm.tier not in policy.enabled_tiers:
            decisions.append(
                Decision(
                    fm.id,
                    Outcome.TIER_DISABLED,
                    f"tier {fm.tier} not enabled for this run",
                    matched_paths=matched,
                    matched_triggers=triggers,
                )
            )
            continue

        if fm.maturity is Maturity.EXPERIMENTAL and not policy.allow_experimental:
            decisions.append(
                Decision(
                    fm.id,
                    Outcome.NOT_MATURE,
                    "maturity=experimental; runs but does not post",
                    matched_paths=matched,
                    matched_triggers=triggers,
                )
            )
            continue

        disallowed = sorted(set(fm.simulator_matrix) - policy.simulator_allowlist)
        if disallowed:
            decisions.append(
                Decision(
                    fm.id,
                    Outcome.MATRIX_NOT_ALLOWED,
                    f"simulator(s) not in repo allowlist: {disallowed}",
                    matched_paths=matched,
                    matched_triggers=triggers,
                )
            )
            continue

        cost = fm.cost_budget_usd if fm.model_tier is not ModelTier.NONE else 0.0
        if cost > remaining:
            decisions.append(
                Decision(
                    fm.id,
                    Outcome.OVER_BUDGET,
                    f"declares ${cost:.2f}, ${remaining:.2f} left of ${policy.pr_budget_usd:.2f}",
                    matched_paths=matched,
                    matched_triggers=triggers,
                )
            )
            continue

        remaining -= cost
        decisions.append(
            Decision(
                fm.id,
                Outcome.FIRED,
                f"matched {len(matched)} path(s) on {list(triggers)}",
                matched_paths=matched,
                matched_triggers=triggers,
                reserved_usd=cost,
            )
        )

    return decisions
