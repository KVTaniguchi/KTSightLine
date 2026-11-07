"""The acceptance-tier contract: criteria schema, verdict construction, report shape.

Each test names the ADR-0004 rule it defends.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from sightline.core.acceptance.criteria import (
    CriteriaFile,
    Criterion,
    Evidence,
    LogObservation,
    Observation,
    Step,
    method_name,
)
from sightline.core.acceptance.report import AcceptanceReport, render_markdown
from sightline.core.acceptance.verdict import (
    _DERIVE_TOKEN,
    CriterionResult,
    NotVerifiableReason,
    ObservationResult,
    VerdictKind,
)
from sightline.core.evidence.models import ArtifactKind, ArtifactRef

REPO = Path(__file__).resolve().parent.parent
EXAMPLE = REPO / "eval" / "acceptance" / "examples" / "CHK-7.yml"


def _ref() -> ArtifactRef:
    return ArtifactRef.for_content(
        b"xcresult-info-plist",
        kind=ArtifactKind.XCRESULT,
        produced_by="xcodebuild.test",
        run_id="run-1",
        context={"device": "iPhone SE (3rd generation)"},
        created_at=datetime(2026, 10, 11, tzinfo=UTC),
    )


def _prose() -> dict[str, str]:
    return {"given": "g", "when": "w", "then": "t"}


def _derived(verdict: VerdictKind, *observations: ObservationResult, **over) -> CriterionResult:
    kw = {
        "id": "CHK-7.AC1",
        **_prose(),
        "evidence_type": Evidence.UI_STATE,
        "verdict": verdict,
        "observations": list(observations),
        "evidence": [_ref()],
    }
    kw.update(over)
    return CriterionResult(_DERIVE_TOKEN, **kw)


def _obs(met: bool, actual: dict | None = None) -> ObservationResult:
    return ObservationResult(
        kind=Evidence.UI_STATE,
        target="checkout.subtotal",
        expected={"label": "Subtotal $441.00"},
        actual=actual if actual is not None else {"label": "Subtotal $441.00"},
        met=met,
    )


# --- §2: the criteria file -------------------------------------------------------------


def test_example_criteria_parse():
    criteria = CriteriaFile.load(EXAMPLE)
    assert criteria.spec_id == "CHK-7"
    assert criteria.provenance == {"harness_version": "3f9c2e1", "stage": "implement"}
    ac2 = criteria.criteria[1]
    assert ac2.steps == [Step(action="tap", target="payment.placeOrder")]
    assert criteria.criteria[2].evidence is Evidence.LOG_EVENT
    assert criteria.criteria[3].manual and not criteria.criteria[3].executable


def test_typo_key_is_rejected_not_ignored():
    """§2: extra='forbid'. A typo that silently means "never checked" is the failure."""
    with pytest.raises(ValidationError, match="expectt"):
        Criterion.model_validate({"id": "A1", **_prose(), "expectt": []})
    with pytest.raises(ValidationError):
        CriteriaFile.model_validate(
            {"acceptance_schema": 1, "spec_id": "X", "criteria": [], "provenence": {}}
        )


def test_unknown_step_names_the_missing_word():
    with pytest.raises(ValidationError, match="step `swipe` is not in the v1 vocabulary"):
        Step.model_validate({"swipe": "cart.continue"})


def test_type_step_normalizes():
    step = Step.model_validate({"type": {"into": "search.field", "text": 'a "b" \\(c)'}})
    assert (step.action, step.target, step.text) == ("type", "search.field", 'a "b" \\(c)')


def test_unknown_attribute_names_the_missing_word():
    with pytest.raises(ValidationError, match="attribute `color` is not in the v1 vocabulary"):
        Observation.model_validate({"id": "x", "color": "red"})


def test_observation_must_check_something():
    with pytest.raises(ValidationError, match="checks nothing"):
        Observation.model_validate({"id": "x"})


def test_absent_element_has_no_attributes():
    with pytest.raises(ValidationError, match="absent element"):
        Observation.model_validate({"id": "x", "exists": False, "label": "y"})


def test_log_observation_needs_exactly_one_bound():
    with pytest.raises(ValidationError, match="exactly one of count, min_count"):
        LogObservation.model_validate({"event": "order_submitted"})
    with pytest.raises(ValidationError, match="exactly one of count, min_count"):
        LogObservation.model_validate({"event": "order_submitted", "count": 1, "min_count": 1})
    assert LogObservation(event="e", count=0).met_by(0)
    assert not LogObservation(event="e", count=1).met_by(2)
    assert LogObservation(event="e", min_count=1).met_by(3)


def test_manual_needs_a_reason():
    with pytest.raises(ValidationError, match="needs a `reason`"):
        Criterion.model_validate({"id": "A1", **_prose(), "manual": True})


@pytest.mark.parametrize("bad", ["CHK_7.AC1", "7.AC1", "CHK-7.", "CHK 7", "CHK-7..AC1", ""])
def test_criterion_id_pattern(bad):
    """§2: IDs map onto Swift method names, so the alphabet is constrained."""
    with pytest.raises(ValidationError):
        Criterion.model_validate({"id": bad, **_prose()})


def test_method_names():
    assert method_name("CHK-7.AC1") == "test_AC_CHK_7_AC1"
    assert method_name("CHK-7.AC1") == method_name("CHK.7-AC1")  # lint's job to reject


# --- §3: verdicts ----------------------------------------------------------------------


def test_pass_and_fail_cannot_be_built_outside_derive():
    """§3 rule 1: a pass or fail exists only because derive saw a record."""
    for verdict in (VerdictKind.PASS, VerdictKind.FAIL, "pass", "fail"):
        with pytest.raises(TypeError, match="constructible only by"):
            CriterionResult(
                id="A1",
                **_prose(),
                evidence_type=Evidence.UI_STATE,
                verdict=verdict,
                observations=[_obs(True)],
                evidence=[_ref()],
            )


def test_not_verifiable_is_free_to_build_and_needs_a_reason():
    nv = CriterionResult.not_verifiable(
        id="A1", **_prose(), evidence_type=Evidence.UI_STATE, reason=NotVerifiableReason.MANUAL
    )
    assert nv.verdict is VerdictKind.NOT_VERIFIABLE
    with pytest.raises(ValidationError, match="needs a reason"):
        CriterionResult(
            id="A1", **_prose(), evidence_type=Evidence.UI_STATE, verdict="not_verifiable"
        )


def test_pass_needs_every_observation_met_and_an_artifact():
    assert _derived(VerdictKind.PASS, _obs(True)).verdict is VerdictKind.PASS
    with pytest.raises(ValidationError, match="unmet"):
        _derived(VerdictKind.PASS, _obs(True), _obs(False, {"label": "x"}))
    with pytest.raises(ValidationError, match="at least one artifact"):
        _derived(VerdictKind.PASS, _obs(True), evidence=[])
    with pytest.raises(ValidationError, match="every observation judged"):
        _derived(VerdictKind.PASS)


def test_fail_needs_an_observed_actual():
    """§3: a fail record carries expected and actual."""
    assert _derived(VerdictKind.FAIL, _obs(False, {"label": "Subtotal $377.00"})).verdict == "fail"
    unobserved = ObservationResult(
        kind=Evidence.UI_STATE, target="x", expected={"label": "y"}, actual=None, met=False
    )
    with pytest.raises(ValidationError, match="observed actual"):
        _derived(VerdictKind.FAIL, unobserved)
    with pytest.raises(ValidationError, match="every observation met"):
        _derived(VerdictKind.FAIL, _obs(True))


def test_pass_cannot_carry_a_reason():
    with pytest.raises(ValidationError, match="carries no not_verifiable reason"):
        _derived(VerdictKind.PASS, _obs(True), reason=NotVerifiableReason.MANUAL)


# --- §7: the report --------------------------------------------------------------------


def _report(*results: CriterionResult, **over) -> AcceptanceReport:
    kw = {"run_id": "r1", "spec_id": "CHK-7", "provenance": {"stage": "implement"}}
    kw.update(over)
    return AcceptanceReport(**kw).with_results(list(results))


def _nv(cid: str, reason: NotVerifiableReason) -> CriterionResult:
    return CriterionResult.not_verifiable(
        id=cid, **_prose(), evidence_type=Evidence.UI_STATE, reason=reason, detail=f"{cid} detail"
    )


def test_report_shape_is_the_public_contract():
    """§7: other tools key on these names. Renaming one is a report_schema bump."""
    report = _report(
        _derived(VerdictKind.PASS, _obs(True)),
        _nv("CHK-7.AC4", NotVerifiableReason.MANUAL),
    ).finish()
    data = json.loads(report.model_dump_json())
    assert set(data) == {
        "report_schema", "run_id", "spec_id", "provenance", "head_sha", "status",
        "started_at", "finished_at", "context", "notes", "criteria", "summary",
    }  # fmt: skip
    assert data["report_schema"] == 1
    assert set(data["criteria"][0]) == {
        "id", "given", "when", "then", "evidence_type", "verdict", "reason", "detail",
        "observations", "evidence", "duration_s",
    }  # fmt: skip
    assert set(data["criteria"][0]["observations"][0]) == {
        "kind", "target", "expected", "actual", "met", "lines",
    }  # fmt: skip
    assert data["summary"]["by_verdict"] == {"pass": 1, "fail": 0, "not_verifiable": 1}
    assert data["summary"]["by_reason"] == {"manual": 1}


def test_not_verifiable_listed_by_id_not_just_counted():
    """§7, the deliberate difference from D8."""
    s = _report(_nv("A1", NotVerifiableReason.STEP_UNREACHABLE)).summary
    assert [(e.id, e.reason) for e in s.not_verifiable] == [
        ("A1", NotVerifiableReason.STEP_UNREACHABLE)
    ]
    assert s.unreachable == ["A1"]


def test_crashed_criteria_come_first():
    """§3 rule 5."""
    report = _report(
        _derived(VerdictKind.PASS, _obs(True)),
        _nv("A2", NotVerifiableReason.APP_CRASHED),
    )
    assert [c.id for c in report.criteria] == ["A2", "CHK-7.AC1"]
    assert report.summary.crashed == ["A2"]


def test_report_round_trips_through_json(tmp_path):
    report = _report(_derived(VerdictKind.FAIL, _obs(False, {"label": "Subtotal $377.00"})))
    path = report.finish().write(tmp_path / "acceptance-r1.json")
    back = AcceptanceReport.read(path)
    assert back.criteria[0].verdict is VerdictKind.FAIL
    assert back.criteria[0].observations[0].actual == {"label": "Subtotal $377.00"}


@pytest.mark.parametrize(
    ("results", "status", "strict", "code"),
    [
        (["fail"], "completed", False, 0),
        (["fail"], "completed", True, 1),
        (["pass", "nv"], "completed", True, 0),
        (["build"], "build_failed", True, 2),
        (["runner"], "runner_unavailable", True, 2),
        (["build"], "build_failed", False, 0),
    ],
)
def test_exit_codes(results, status, strict, code):
    """§8: 0 by default; under --strict, 2 when nothing ran, 1 on any fail."""
    made = {
        "fail": lambda: _derived(VerdictKind.FAIL, _obs(False, {"label": "x"})),
        "pass": lambda: _derived(VerdictKind.PASS, _obs(True)),
        "nv": lambda: _nv("A2", NotVerifiableReason.MANUAL),
        "build": lambda: _nv("A3", NotVerifiableReason.BUILD_FAILED),
        "runner": lambda: _nv("A4", NotVerifiableReason.RUNNER_UNAVAILABLE),
    }
    report = _report(*(made[r]() for r in results), status=status)
    assert report.exit_code(strict=strict) == code


def test_markdown_labels_evidence_and_survives_hostile_text():
    hostile = _nv("A1", NotVerifiableReason.NO_OBSERVABLE).model_copy(
        update={"then": "pipes | and\nnewlines", "detail": "a|b"}
    )
    md = render_markdown(_report(hostile, _derived(VerdictKind.PASS, _obs(True))))
    assert "| ui_state |" in md
    assert "pipes \\| and newlines" in md
    row = next(line for line in md.splitlines() if line.startswith("| `A1`"))
    assert row.count(" | ") == 3  # still four cells
