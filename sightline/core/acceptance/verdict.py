"""Per-criterion verdicts.

ADR-0004 §3. Three verdicts, and the rule that matters more than the table: **ambiguity
never resolves to pass or fail.** A ``pass`` or ``fail`` is constructible only by
``core.acceptance.derive``, from records the driver wrote into the ``.xcresult`` — the
same speed bump ``VerifiedFinding`` uses. ``not_verifiable`` claims nothing and anyone
may build one.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sightline.core.acceptance.criteria import Evidence
from sightline.core.evidence.models import ArtifactRef

_DERIVE_TOKEN = object()
"""Private capability token. ``core.acceptance.derive`` holds the only other reference."""


class VerdictKind(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    NOT_VERIFIABLE = "not_verifiable"


class NotVerifiableReason(StrEnum):
    """Closed set. Listed in derivation order (ADR-0004 §3): the first that applies wins."""

    MANUAL = "manual"
    EVIDENCE_TYPE_UNSUPPORTED = "evidence_type_unsupported"
    BUILD_FAILED = "build_failed"
    RUNNER_UNAVAILABLE = "runner_unavailable"
    APP_CRASHED = "app_crashed"
    SURFACE_UNREACHABLE = "surface_unreachable"
    STEP_UNREACHABLE = "step_unreachable"
    AMBIGUOUS_ELEMENT = "ambiguous_element"
    NO_OBSERVABLE = "no_observable"
    NO_RECORD = "no_record"
    LOG_UNAVAILABLE = "log_unavailable"


UNREACHABLE = frozenset(
    {NotVerifiableReason.SURFACE_UNREACHABLE, NotVerifiableReason.STEP_UNREACHABLE}
)
NOTHING_RAN = frozenset({NotVerifiableReason.BUILD_FAILED, NotVerifiableReason.RUNNER_UNAVAILABLE})


class ObservationResult(BaseModel):
    """One expectation, as observed. ``kind`` is the evidence that produced it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Evidence
    target: str
    """The accessibility identifier, or the log event name."""
    expected: dict[str, Any]
    actual: dict[str, Any] | None = None
    """``None`` when nothing was observed — the element was missing, the record absent."""
    met: bool | None = None
    """``None`` when there is no observation to judge."""
    lines: list[str] = Field(default_factory=list)
    """For log observations: the matching ``SIGHTLINE-AC|`` lines, verbatim (redacted)."""


class CriterionResult(BaseModel):
    """One row of the report, keyed on ``id``. Part of the public contract (ADR-0004 §7)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    given: str
    when: str
    then: str
    evidence_type: Evidence
    verdict: VerdictKind
    reason: NotVerifiableReason | None = None
    detail: str = ""
    observations: list[ObservationResult] = Field(default_factory=list)
    evidence: list[ArtifactRef] = Field(default_factory=list)
    duration_s: float = 0.0

    def __init__(self, /, _token: Any = None, **data: Any) -> None:
        verdict = data.get("verdict")
        if verdict in (VerdictKind.PASS, VerdictKind.FAIL, "pass", "fail") and (
            _token is not _DERIVE_TOKEN
        ):
            raise TypeError(
                "a pass or fail CriterionResult is constructible only by "
                "sightline.core.acceptance.derive, from records in the .xcresult. "
                "Use CriterionResult.not_verifiable(...) for anything else."
            )
        super().__init__(**data)

    @model_validator(mode="after")
    def _verdict_is_backed(self) -> CriterionResult:
        if self.verdict is VerdictKind.NOT_VERIFIABLE:
            if self.reason is None:
                raise ValueError("not_verifiable needs a reason from the closed set")
            return self
        if self.reason is not None:
            raise ValueError(f"{self.verdict} carries no not_verifiable reason")
        if not self.observations or any(o.met is None for o in self.observations):
            raise ValueError(f"{self.verdict} needs every observation judged")
        if not self.evidence:
            raise ValueError(f"{self.verdict} needs at least one artifact")
        met = [o.met for o in self.observations]
        if self.verdict is VerdictKind.PASS and not all(met):
            raise ValueError("pass with an unmet observation")
        if self.verdict is VerdictKind.FAIL:
            if all(met):
                raise ValueError("fail with every observation met")
            if any(o.met is False and o.actual is None for o in self.observations):
                raise ValueError("fail needs the observed actual value for every unmet observation")
        return self

    @classmethod
    def not_verifiable(
        cls,
        *,
        id: str,
        given: str,
        when: str,
        then: str,
        evidence_type: Evidence,
        reason: NotVerifiableReason,
        detail: str = "",
        observations: list[ObservationResult] | None = None,
        evidence: list[ArtifactRef] | None = None,
        duration_s: float = 0.0,
    ) -> CriterionResult:
        return cls(
            id=id,
            given=given,
            when=when,
            then=then,
            evidence_type=evidence_type,
            verdict=VerdictKind.NOT_VERIFIABLE,
            reason=reason,
            detail=detail,
            observations=observations or [],
            evidence=evidence or [],
            duration_s=duration_s,
        )


def rehydrate(data: dict[str, Any]) -> CriterionResult:
    """Rebuild a result read back from a written report.

    Not a way to *make* a verdict: the file was written from a derived result, and
    reading it must not demote a pass to an error. Anything that edits a report on disk
    has left Sightline's guarantees behind, and the report schema cannot prevent that.
    """
    return CriterionResult(_DERIVE_TOKEN, **data)
