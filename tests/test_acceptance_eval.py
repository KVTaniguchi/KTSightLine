"""Acceptance corpus scoring. Pure: reports in, confusion matrix out.

A false pass or a false fail is the disqualifying error; a not_verifiable with the wrong
reason fails the case without being either.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from sightline.config import RepoConfig
from sightline.core.acceptance.criteria import Evidence
from sightline.core.acceptance.lint import has_errors, lint_file
from sightline.core.acceptance.report import AcceptanceReport
from sightline.core.acceptance.verdict import (
    _DERIVE_TOKEN,
    CriterionResult,
    NotVerifiableReason,
    ObservationResult,
)
from sightline.core.evidence.models import ArtifactKind, ArtifactRef
from sightline.eval_harness import (
    AcceptanceCase,
    AcceptanceExpectation,
    AcceptanceScore,
    discover_acceptance_cases,
    score_acceptance,
)

REPO = Path(__file__).resolve().parent.parent
CORPUS = REPO / "eval" / "acceptance"
FIXTURE = REPO / "eval" / "fixtures" / "CheckoutDemo"


def _ref() -> ArtifactRef:
    return ArtifactRef.for_content(
        b"info-plist",
        kind=ArtifactKind.XCRESULT,
        produced_by="xcodebuild.test",
        run_id="r",
        context={},
        created_at=datetime(2026, 10, 11, tzinfo=UTC),
    )


def _result(cid: str, verdict: str, reason=None, actual=None) -> CriterionResult:
    prose = {"given": "g", "when": "w", "then": "t", "evidence_type": Evidence.UI_STATE}
    if verdict == "not_verifiable":
        return CriterionResult.not_verifiable(id=cid, reason=NotVerifiableReason(reason), **prose)
    obs = ObservationResult(
        kind=Evidence.UI_STATE,
        target="x",
        expected={"label": "want"},
        actual=actual or {"label": "want"},
        met=verdict == "pass",
    )
    return CriterionResult(
        _DERIVE_TOKEN, id=cid, verdict=verdict, observations=[obs], evidence=[_ref()], **prose
    )


def _case(**expected: AcceptanceExpectation) -> AcceptanceCase:
    return AcceptanceCase(
        id="c", title="t", directory=CORPUS, criteria=CORPUS / "x.yml", expected=dict(expected)
    )


def _report(*results: CriterionResult) -> AcceptanceReport:
    return AcceptanceReport(run_id="r", spec_id="S").with_results(list(results))


def test_agreement_passes():
    case = _case(
        A=AcceptanceExpectation("pass"), B=AcceptanceExpectation("not_verifiable", "manual")
    )
    result = score_acceptance(
        case, _report(_result("A", "pass"), _result("B", "not_verifiable", "manual"))
    )
    assert result.passed


def test_false_pass_and_false_fail_are_named():
    case = _case(
        A=AcceptanceExpectation("fail"), B=AcceptanceExpectation("not_verifiable", "manual")
    )
    result = score_acceptance(
        case, _report(_result("A", "pass"), _result("B", "fail", actual={"label": "x"}))
    )
    assert result.false_passes == ["A"]
    assert result.false_fails == ["B"]
    score = AcceptanceScore([result])
    assert score.matrix[("fail", "pass")] == 1
    assert score.matrix[("not_verifiable", "fail")] == 1
    assert not score.passed


def test_wrong_reason_is_a_mismatch_not_a_false_verdict():
    case = _case(A=AcceptanceExpectation("not_verifiable", "step_unreachable"))
    result = score_acceptance(case, _report(_result("A", "not_verifiable", "ambiguous_element")))
    assert not result.false_passes and not result.false_fails
    assert result.mismatches == ["A: expected reason step_unreachable, got ambiguous_element"]


def test_expected_actual_must_be_recorded():
    case = _case(A=AcceptanceExpectation("fail", actual={"label": "Subtotal $377.00"}))
    wrong = score_acceptance(case, _report(_result("A", "fail", actual={"label": "x"})))
    assert wrong.mismatches
    right = score_acceptance(
        case, _report(_result("A", "fail", actual={"label": "Subtotal $377.00"}))
    )
    assert right.passed


def test_unscored_and_missing_criteria_fail_the_case():
    result = score_acceptance(_case(A=AcceptanceExpectation("pass")), _report(_result("B", "pass")))
    assert "B: in the report but not scored by the case" in result.mismatches
    assert any(m.startswith("A: expected pass, missing") for m in result.mismatches)


@pytest.mark.parametrize("case", discover_acceptance_cases(CORPUS), ids=lambda c: c.id)
def test_every_corpus_case_lints_and_scores_every_criterion(case):
    """The corpus itself: criteria lint clean, every criterion has an expectation, and
    each case carries the patch and config it names."""
    config = RepoConfig.load(FIXTURE, override=case.config)
    criteria, issues = lint_file(case.criteria, config)
    assert criteria is not None and not has_errors(issues), [str(i) for i in issues]
    assert {c.id for c in criteria.criteria} == set(case.expected)
    if case.patch:
        assert case.patch.exists()


def test_corpus_covers_the_briefs_minimum():
    """Brief §5: pass, the same criterion failing on a patch, manual, unsupported
    evidence, an unreachable step, and a fixture — plus the log cases from the addendum."""
    cases = {c.id: c for c in discover_acceptance_cases(CORPUS)}
    expected = [(e.verdict, e.reason) for c in cases.values() for e in c.expected.values()]
    for needed in [
        ("pass", None),
        ("fail", None),
        ("not_verifiable", "manual"),
        ("not_verifiable", "evidence_type_unsupported"),
        ("not_verifiable", "step_unreachable"),
        ("not_verifiable", "ambiguous_element"),
        ("not_verifiable", "log_unavailable"),
    ]:
        assert needed in expected, needed
    assert any(c.config for c in cases.values()), "a case must exercise a fixture"
