"""The falsifiability gate.

ADR-0002 §2: ``VerifiedFinding`` is constructible **only** here. Everything else in the
codebase can produce a ``ProposedFinding``; only this module can turn one into something
a ForgeAdapter will accept.

Verifiers are adversarial by construction — they try to reject, and ambiguity resolves
to reject. A verifier that cannot find its required evidence returns
``reject(missing_evidence)``; it never falls back to trusting the model.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from sightline.core.evidence.models import ArtifactKind
from sightline.core.findings.models import (
    _GATE_TOKEN,
    ProposedFinding,
    Verdict,
    VerifiedFinding,
)
from sightline.core.telemetry.trajectory import Suppression


class SuppressionReason:
    MISSING_EVIDENCE = "missing_evidence"
    NOT_CONFIRMED = "not_confirmed"
    UNANCHORABLE = "unanchorable"
    NO_BASELINE = "no_baseline"
    NO_VERIFIER = "no_verifier"


@dataclass(frozen=True)
class GateResult:
    verified: tuple[VerifiedFinding, ...]
    suppressed: tuple[Suppression, ...]

    @property
    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for s in self.suppressed:
            out[s.reason] = out.get(s.reason, 0) + 1
        return dict(sorted(out.items()))


class Verifier(ABC):
    name: str
    required_evidence: frozenset[ArtifactKind]

    @abstractmethod
    def check(self, proposed: ProposedFinding) -> Verdict: ...

    def _missing_evidence(self, proposed: ProposedFinding) -> bool:
        present = {ref.kind for ref in proposed.evidence}
        return not self.required_evidence <= present


class StructuredOracleVerifier(Verifier):
    """Confirms a claim only when an independent tool reported the same thing.

    The oracle is a set of keys extracted from the tool's own output — for the
    accessibility audit, ``"<auditType>:<identifier>"`` recovered from the `.xcresult`.
    The verifier looks up ``proposed.oracle_key``; it never reads the claim text. A
    model that invents a plausible finding produces a key nothing reported, and dies
    here.
    """

    name = "structured_oracle"
    required_evidence = frozenset({ArtifactKind.XCRESULT})

    def __init__(self, oracle_keys: frozenset[str]) -> None:
        self.oracle_keys = oracle_keys

    def check(self, proposed: ProposedFinding) -> Verdict:
        if self._missing_evidence(proposed):
            return Verdict(
                verifier=self.name,
                confirmed=False,
                reason=f"requires {sorted(k.value for k in self.required_evidence)}",
            )
        if not proposed.oracle_key:
            return Verdict(
                verifier=self.name,
                confirmed=False,
                reason="no oracle_key; the claim names nothing that can be looked up",
            )
        if proposed.oracle_key not in self.oracle_keys:
            return Verdict(
                verifier=self.name,
                confirmed=False,
                reason=f"oracle reported no issue for {proposed.oracle_key!r}",
            )
        return Verdict(
            verifier=self.name,
            confirmed=True,
            reason=f"oracle independently reported {proposed.oracle_key!r}",
            supporting_evidence=list(proposed.evidence),
        )


def _is_anchorable(proposed: ProposedFinding) -> bool:
    """OQ-FIXTURE-1.

    On the fixture's Cart screen, 3 of 5 real audit failures arrive with
    ``element = nil`` — no identifier, no frame, nothing to anchor a comment to.

    **Current policy: drop them.** ADR-0002 forbids inventing an anchor, and a comment
    on the wrong line is instantly disqualifying. The alternative — anchoring to the
    screen's entry-point symbol with weaker evidence — is a real option that trades
    precision for recall, and it is an open decision, not a settled one. Every drop is
    counted under ``unanchorable`` so the cost of this policy is measured rather than
    assumed.
    """
    return proposed.anchor.line > 0 and bool(proposed.enclosing_symbol)


def run_gate(
    proposed: list[ProposedFinding],
    verifier: Verifier | None,
    *,
    skill_id: str,
) -> GateResult:
    verified: list[VerifiedFinding] = []
    suppressed: list[Suppression] = []

    def drop(finding: ProposedFinding, reason: str, verdict: Verdict | None = None) -> None:
        suppressed.append(
            Suppression(
                skill_id=skill_id,
                fingerprint=finding.fingerprint,
                claim=finding.claim,
                reason=reason,
                verifier=verdict.verifier if verdict else (verifier.name if verifier else None),
                artifacts_present=[ref.kind.value for ref in finding.evidence],
            )
        )

    for finding in proposed:
        if verifier is None:
            drop(finding, SuppressionReason.NO_VERIFIER)
            continue
        if not _is_anchorable(finding):
            drop(finding, SuppressionReason.UNANCHORABLE)
            continue

        verdict = verifier.check(finding)
        if not verdict.confirmed:
            reason = (
                SuppressionReason.MISSING_EVIDENCE
                if "requires" in verdict.reason
                else SuppressionReason.NOT_CONFIRMED
            )
            drop(finding, reason, verdict)
            continue

        verified.append(
            VerifiedFinding(
                _GATE_TOKEN,
                proposed=finding,
                verdict=verdict,
                verified_by=verdict.verifier,
            )
        )

    return GateResult(verified=tuple(verified), suppressed=tuple(suppressed))
