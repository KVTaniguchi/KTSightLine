"""Skill frontmatter schema (v1).

A skill is a Markdown file: strict YAML frontmatter (this module) plus a body that is the
model prompt for skills with a judgment step. The body is never executed and never parsed
for control flow. See docs/adr/0001-skill-format-and-dispatch.md.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Trigger(StrEnum):
    """Closed vocabulary, emitted by the deterministic impact layer.

    Adding a member requires an emitter in ``core/impact/`` plus a fixture test that
    proves it fires on a positive diff and stays silent on a control. Skill authors do
    not write free text here: a model in the dispatch path breaks replayability.
    """

    FILE_CHANGED = "file_changed"
    SWIFT_SYMBOL_CHANGED = "swift_symbol_changed"
    UI_SURFACE_CHANGED = "ui_surface_changed"
    VIEW_ADDED = "view_added"
    VIEW_MODIFIED = "view_modified"
    NAVIGATION_GRAPH_CHANGED = "navigation_graph_changed"
    INFO_PLIST_CHANGED = "info_plist_changed"
    PRIVACY_MANIFEST_CHANGED = "privacy_manifest_changed"
    ENTITLEMENTS_CHANGED = "entitlements_changed"
    LOCALIZATION_CHANGED = "localization_changed"
    ASSET_CATALOG_CHANGED = "asset_catalog_changed"
    CORE_DATA_MODEL_CHANGED = "core_data_model_changed"
    PACKAGE_RESOLVED_CHANGED = "package_resolved_changed"
    BUILD_SETTINGS_CHANGED = "build_settings_changed"
    TEST_TARGET_CHANGED = "test_target_changed"
    CONCURRENCY_ANNOTATION_ADDED = "concurrency_annotation_added"
    PERMISSION_API_REFERENCED = "permission_api_referenced"


class Tier(StrEnum):
    STATIC = "static"  # no build; runs on Linux on every PR
    BUILD = "build"  # needs a compile
    RUNTIME = "runtime"  # needs a booted simulator


class ModelTier(StrEnum):
    NONE = "none"  # pure deterministic check; spends nothing. Encouraged.
    CHEAP = "cheap"  # triage, classification
    STANDARD = "standard"
    FRONTIER = "frontier"  # reserve for genuinely reasoning-heavy judgment


class Verifier(StrEnum):
    """Named verifiers from ADR-0002. Each requires specific artifact kinds."""

    STRUCTURED_ORACLE = "structured_oracle"
    DIFFERENTIAL_RENDER = "differential_render"
    REEXECUTION = "reexecution"
    DIFFERENTIAL_METRIC = "differential_metric"


class Maturity(StrEnum):
    EXPERIMENTAL = "experimental"  # runs, logs, does not post
    OWNERS_ONLY = "owners_only"  # posts only on PRs touching owned paths
    STABLE = "stable"


class SkillFrontmatter(BaseModel):
    """``extra='forbid'`` on purpose.

    A typo'd key that silently means "never fires" is the failure mode that makes people
    stop trusting the harness. A load error fails the *skill*, not the run.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: Annotated[str, Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")]
    trigger_schema: Literal[1]
    tier: Tier

    globs: Annotated[list[str], Field(min_length=1)]
    excludes: list[str] = Field(default_factory=list)

    triggers: Annotated[list[Trigger], Field(min_length=1)]
    trigger_mode: Literal["any", "all"] = "any"

    requires_evidence: Annotated[list[str], Field(min_length=1)]
    verifier: Verifier

    capabilities: list[dict[str, object]] = Field(default_factory=list)
    simulator_matrix: list[str] = Field(default_factory=list)

    model_tier: ModelTier = ModelTier.NONE
    cost_budget_usd: Annotated[float, Field(ge=0, le=5.0)] = 0.0

    severity_default: Literal["blocking", "high", "medium", "low"] = "medium"
    max_findings: Annotated[int, Field(ge=1, le=10)] = 3
    maturity: Maturity = Maturity.EXPERIMENTAL
    owners: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _tier_consistency(self) -> SkillFrontmatter:
        if self.tier is not Tier.RUNTIME and self.simulator_matrix:
            raise ValueError("simulator_matrix is only meaningful for tier: runtime")
        if self.tier is Tier.RUNTIME and not self.capabilities:
            raise ValueError(
                "a runtime skill must declare capabilities; "
                "the harness performs behavior, the skill declares it"
            )
        if self.model_tier is not ModelTier.NONE and self.cost_budget_usd <= 0:
            raise ValueError("a skill that calls a model must declare a cost budget")
        if self.model_tier is ModelTier.NONE and self.cost_budget_usd > 0:
            raise ValueError("model_tier: none cannot have a cost budget")
        return self
