"""Run trajectories.

Brief rule 5: telemetry is a first-class module, not logging. A trajectory answers, for
one run: which skills fired and *why* (which trigger matched), what each cost, how long
it took, what evidence it gathered, and what got suppressed by which rule.

The suppression log is the most valuable thing we produce for our own development
(ADR-0002 §3) — it is the list of checks we almost got right, and it is what tells us
which verifier to build next. It is not a debug aid.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA = 1


class SkillTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_id: str
    outcome: str  # dispatch Outcome
    reason: str
    matched_paths: list[str] = Field(default_factory=list)
    matched_triggers: list[str] = Field(default_factory=list)
    reserved_usd: float = 0.0
    spent_usd: float = 0.0
    duration_s: float = 0.0
    proposed: int = 0
    verified: int = 0
    posted: int = 0
    artifacts: list[str] = Field(default_factory=list)  # sha256s
    error: str | None = None


class Suppression(BaseModel):
    """One finding that did not get posted, and the rule that killed it."""

    model_config = ConfigDict(extra="forbid")

    skill_id: str
    fingerprint: str | None = None
    claim: str
    reason: str
    verifier: str | None = None
    artifacts_present: list[str] = Field(default_factory=list)


class GateTrace(BaseModel):
    """Why the runtime tier ran, or did not. ADR-0003 §2 and §7."""

    model_config = ConfigDict(extra="forbid")

    runtime_enabled: bool
    reason: str
    stage: Literal[
        "path_globs", "triggers", "label", "budget", "classifier", "fork", "runner", "ok"
    ] = "ok"


class Trajectory(BaseModel):
    """One run. Written as JSON; the write path is an interface elsewhere."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA
    run_id: str
    repo: str
    pr_number: int | None = None
    base_sha: str = ""
    head_sha: str = ""
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None

    triggers: list[str] = Field(default_factory=list)
    trigger_evidence: list[dict[str, Any]] = Field(default_factory=list)
    gate: GateTrace | None = None
    skills: list[SkillTrace] = Field(default_factory=list)
    suppressions: list[Suppression] = Field(default_factory=list)

    tool_versions: dict[str, str] = Field(default_factory=dict)
    budget_usd: float = 0.0
    spent_usd: float = 0.0
    notes: list[str] = Field(default_factory=list)

    # --- summary numbers the PR comment and the metrics both read ---

    @property
    def fired(self) -> list[SkillTrace]:
        return [s for s in self.skills if s.outcome == "fired"]

    @property
    def posted_count(self) -> int:
        return sum(s.posted for s in self.skills)

    def suppression_summary(self) -> dict[str, int]:
        """`{"missing_evidence": 2, "no_baseline": 1}` — the D8 run-summary line.

        Counts and reasons only. Never claim text: surfacing suppressed claims in a
        fold is posting unverified findings with extra steps.
        """
        out: dict[str, int] = {}
        for s in self.suppressions:
            out[s.reason] = out.get(s.reason, 0) + 1
        return dict(sorted(out.items()))

    def summary_line(self) -> str:
        parts = [f"{self.posted_count} posted"]
        if self.suppressions:
            detail = ", ".join(f"{n} {reason}" for reason, n in self.suppression_summary().items())
            parts.append(f"{len(self.suppressions)} suppressed — {detail}")
        denied = [s for s in self.skills if s.outcome in {"over_budget", "matrix_not_allowed"}]
        for s in denied:
            parts.append(f"skill `{s.skill_id}` not run: {s.reason}")
        return " · ".join(parts)

    def finish(self) -> Trajectory:
        self.finished_at = datetime.now(UTC)
        return self

    def write(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2))
        return path

    @classmethod
    def read(cls, path: Path) -> Trajectory:
        return cls.model_validate(json.loads(Path(path).read_text()))
