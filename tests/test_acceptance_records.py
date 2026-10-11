"""Record parsing and verdict derivation (ADR-0004 §3–§5).

Two halves:

* **Real records.** ``tests/fixtures/xcresult/acceptance/`` is JSON captured from a real
  run of the generated driver against CheckoutDemo on Xcode 27.1 (iOS 26.5 simulator),
  with a patch that logs ``order_submitted`` and crashes on a fixture flag. The app
  output is a verbatim excerpt: every ``SIGHTLINE-AC|`` line plus each process's first
  and last line. Parsing and deriving over it must give the verdicts the run showed.
* **The table.** Every ``not_verifiable`` reason, plus pass and fail, from synthetic
  records — derivation is a pure function and is tested as one.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sightline.core.acceptance.criteria import CriteriaFile, Criterion
from sightline.core.acceptance.derive import CriterionFacts, attribute_log_lines, derive
from sightline.core.acceptance.verdict import NotVerifiableReason as R
from sightline.core.acceptance.verdict import VerdictKind
from sightline.core.evidence.models import ArtifactKind, ArtifactRef
from sightline.runners.xcode.xcresult import (
    AcceptanceRecord,
    AppLogLine,
    CriterionRun,
    decode_acceptance_title,
    parse_acceptance_activities,
    parse_acceptance_shot,
    parse_app_output,
    parse_attachments_manifest,
)

CAPTURE = Path(__file__).parent / "fixtures" / "xcresult" / "acceptance"
REF = ArtifactRef.for_content(
    b"info-plist",
    kind=ArtifactKind.XCRESULT,
    produced_by="xcodebuild.test",
    run_id="r",
    context={},
    created_at=datetime(2026, 10, 11, tzinfo=UTC),
)


def _captured(name: str) -> CriterionRun:
    return parse_acceptance_activities(
        json.loads((CAPTURE / f"activities-{name}.json").read_text())
    )


def _lines() -> tuple[AppLogLine, ...]:
    return parse_app_output((CAPTURE / "app-output-excerpt.txt").read_text())


def _facts(run: CriterionRun, lines: tuple[AppLogLine, ...] | None = None) -> CriterionFacts:
    surface = run.phase("surface")
    log = attribute_log_lines(
        _lines() if lines is None else lines,
        launch=run.launch_time,
        counting_from=surface[-1].time if surface else None,
        end=run.done_time or run.teardown_time,
    )
    return CriterionFacts(run=run, log=log, evidence=(REF,))


CRITERIA = {
    c.id.removeprefix("CAP."): c for c in CriteriaFile.load(CAPTURE / "criteria.yml").criteria
}


# --- real records -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "verdict", "reason"),
    [
        ("pass", VerdictKind.PASS, None),
        ("fail", VerdictKind.FAIL, None),
        ("steps", VerdictKind.PASS, None),
        ("log", VerdictKind.PASS, None),
        ("ambiguous", VerdictKind.NOT_VERIFIABLE, R.AMBIGUOUS_ELEMENT),
        ("unreachable", VerdictKind.NOT_VERIFIABLE, R.STEP_UNREACHABLE),
        ("absent", VerdictKind.NOT_VERIFIABLE, R.NO_OBSERVABLE),
        ("crash", VerdictKind.NOT_VERIFIABLE, R.APP_CRASHED),
    ],
)
def test_captured_run_derives_the_observed_verdict(name, verdict, reason):
    run = _captured(name)
    result = derive(CRITERIA[name], _facts(run))
    assert (result.verdict, result.reason) == (verdict, reason), result.detail


def test_captured_fail_carries_expected_and_actual():
    result = derive(CRITERIA["fail"], _facts(_captured("fail")))
    (obs,) = result.observations
    assert obs.expected == {"label": "Subtotal $377.00"}
    assert obs.actual == {"label": "Subtotal $441.00"}
    assert obs.met is False


def test_captured_log_pass_carries_the_line():
    result = derive(CRITERIA["log"], _facts(_captured("log")))
    (obs,) = result.observations
    assert obs.actual == {"count": 1}
    assert obs.lines == ["[acceptance] SIGHTLINE-AC|order_submitted|total=$441.00"]


def test_captured_log_without_any_app_output_is_log_unavailable():
    """§5 liveness: no lines in the window is not "observed zero"."""
    result = derive(CRITERIA["log"], _facts(_captured("log"), lines=()))
    assert result.reason is R.LOG_UNAVAILABLE


def test_captured_crash_is_recognized_with_its_log():
    run = _captured("crash")
    assert run.crashed
    assert run.crash_detail == "com.sightline.fixtures.CheckoutDemo crashed"
    assert run.crash_attachments == ("Crash Log 2026-10-11 at 01.36.53 PM.ips",)
    assert run.done_time is None and run.teardown_time is not None


def test_captured_records_have_launch_and_records_in_order():
    run = _captured("steps")
    assert run.launch_time is not None and run.done_time is not None
    assert [r.phase for r in run.records] == [
        "launched", "surface", "step", "step", "expect", "expect", "shot", "done",
    ]  # fmt: skip
    assert [r.time for r in run.records] == sorted(r.time for r in run.records)


def test_screenshots_map_to_criterion_index():
    manifest = parse_attachments_manifest(
        json.loads((CAPTURE / "attachments-manifest.json").read_text())
    )
    shots = {parse_acceptance_shot(a.suggested_name): a.test_id for a in manifest}
    assert shots[0] == "SightlineAcceptance/test_AC_CAP_pass()"
    assert shots[4] == "SightlineAcceptance/test_AC_CAP_ambiguous()"


def test_app_output_lines_carry_pid_and_time():
    lines = _lines()
    events = [line for line in lines if line.event]
    assert {line.event for line in events} == {"order_submitted"}
    assert all(line.pid > 0 and line.time > 1.7e9 for line in lines)


# --- record decoding --------------------------------------------------------------------


def _title(body: dict) -> str:
    raw = base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip("=")
    return "SIGHTLINE-ACC|" + raw


def test_hostile_observed_text_round_trips():
    """Observed values are arbitrary app text: `|`, newlines, emoji must survive."""
    body = {"i": 0, "id": "A1", "phase": "expect", "actual": {"label": 'a|b\nc 🐱 \\(x) "q"'}}
    assert decode_acceptance_title(_title(body)) == body


@pytest.mark.parametrize("title", ["SIGHTLINE-ACC|not base64!", "SIGHTLINE-ACC|bm90IGpzb24", "x"])
def test_unreadable_records_are_not_records(title):
    assert decode_acceptance_title(title) is None


# --- the table: every reason ------------------------------------------------------------


def _criterion(**over) -> Criterion:
    data = {
        "id": "A1",
        "given": "g",
        "when": "w",
        "then": "t",
        "surface": "Checkout",
        "steps": [{"tap": "payment.placeOrder"}],
        "expect": [{"id": "checkout.subtotal", "exists": True, "label": "Subtotal $441.00"}],
    }
    data.update(over)
    return Criterion.model_validate(data)


def _rec(phase: str, t: float, **fields) -> AcceptanceRecord:
    return AcceptanceRecord(index=0, criterion_id="A1", phase=phase, fields=fields, time=t)


OBSERVED_OK = {"status": "observed", "actual": {"exists": True, "label": "Subtotal $441.00"}}


def _run(*records: AcceptanceRecord, crashed: bool = False) -> CriterionRun:
    return CriterionRun(
        test_id="t",
        records=records,
        launch_time=100.0,
        teardown_time=200.0,
        crashed=crashed,
        crash_detail="app crashed" if crashed else "",
    )


def _happy(**expect_fields) -> CriterionRun:
    return _run(
        _rec("launched", 101),
        _rec("surface", 102, status="ok"),
        _rec("step", 103, n=0, status="ok", target="payment.placeOrder"),
        _rec("expect", 104, n=0, **(expect_fields or OBSERVED_OK)),
        _rec("done", 105),
    )


def _log(t: float, pid: int, message: str) -> AppLogLine:
    return AppLogLine(time=t, pid=pid, process="App", message=message)


AGENT = "[Default] Received request to exchange capabilities"
ORDER = "[acceptance] SIGHTLINE-AC|order_submitted|id=1"
LOG_CRITERION = {
    "evidence": "log_event",
    "expect": [],
    "expect_log": [{"event": "order_submitted", "count": 1}],
}


def _log_facts(lines: list[AppLogLine]) -> CriterionFacts:
    run = _run(
        _rec("launched", 101),
        _rec("surface", 102, status="ok"),
        _rec("step", 103, n=0, status="ok", target="payment.placeOrder"),
        _rec("done", 110),
    )
    log = attribute_log_lines(tuple(lines), launch=100.0, counting_from=102.0, end=110.0)
    return CriterionFacts(run=run, log=log, evidence=(REF,))


TABLE = [
    ("pass", _criterion(), CriterionFacts(run=_happy(), evidence=(REF,)), "pass", None),
    (
        "fail: label",
        _criterion(),
        CriterionFacts(
            run=_happy(status="observed", actual={"exists": True, "label": "Subtotal $377.00"}),
            evidence=(REF,),
        ),
        "fail",
        None,
    ),
    (
        "fail: asserted exists, absent",
        _criterion(),
        CriterionFacts(run=_happy(status="absent", actual={"exists": False}), evidence=(REF,)),
        "fail",
        None,
    ),
    (
        "manual",
        _criterion(manual=True, reason="email"),
        CriterionFacts(),
        "not_verifiable",
        R.MANUAL,
    ),
    (
        "unsupported evidence",
        _criterion(evidence="network_call"),
        CriterionFacts(),
        "not_verifiable",
        R.EVIDENCE_TYPE_UNSUPPORTED,
    ),
    (
        "build failed",
        _criterion(),
        CriterionFacts(status="build_failed", detail="error: x"),
        "not_verifiable",
        R.BUILD_FAILED,
    ),
    (
        "runner unavailable",
        _criterion(),
        CriterionFacts(status="runner_unavailable", detail="no udid"),
        "not_verifiable",
        R.RUNNER_UNAVAILABLE,
    ),
    (
        "crash dominates everything after it",
        _criterion(),
        CriterionFacts(run=_run(_rec("launched", 101), crashed=True), evidence=(REF,)),
        "not_verifiable",
        R.APP_CRASHED,
    ),
    ("no records at all", _criterion(), CriterionFacts(run=None), "not_verifiable", R.NO_RECORD),
    (
        "surface unreachable",
        _criterion(),
        CriterionFacts(
            run=_run(_rec("launched", 101), _rec("surface", 102, status="unreachable", target="x")),
            evidence=(REF,),
        ),
        "not_verifiable",
        R.SURFACE_UNREACHABLE,
    ),
    (
        "undismissed system alert",
        _criterion(),
        CriterionFacts(
            run=_run(
                _rec("launched", 101),
                _rec("alert", 101.5, status="undismissed", label="Allow?"),
                _rec("surface", 102, status="unreachable", target="x"),
            ),
            evidence=(REF,),
        ),
        "not_verifiable",
        R.SURFACE_UNREACHABLE,
    ),
    (
        "ambiguous navigation",
        _criterion(),
        CriterionFacts(
            run=_run(
                _rec("launched", 101), _rec("surface", 102, status="ambiguous", target="x", count=2)
            ),
            evidence=(REF,),
        ),
        "not_verifiable",
        R.AMBIGUOUS_ELEMENT,
    ),
    (
        "step unreachable",
        _criterion(),
        CriterionFacts(
            run=_run(
                _rec("launched", 101),
                _rec("surface", 102, status="ok"),
                _rec("step", 103, n=0, status="unreachable", target="payment.placeOrder"),
            ),
            evidence=(REF,),
        ),
        "not_verifiable",
        R.STEP_UNREACHABLE,
    ),
    (
        "ambiguous step",
        _criterion(),
        CriterionFacts(
            run=_run(
                _rec("launched", 101),
                _rec("surface", 102, status="ok"),
                _rec("step", 103, n=0, status="ambiguous", target="payment.placeOrder", count=3),
            ),
            evidence=(REF,),
        ),
        "not_verifiable",
        R.AMBIGUOUS_ELEMENT,
    ),
    (
        "ambiguous observation",
        _criterion(),
        CriterionFacts(run=_happy(status="ambiguous", count=3), evidence=(REF,)),
        "not_verifiable",
        R.AMBIGUOUS_ELEMENT,
    ),
    (
        "absent, existence not asserted",
        _criterion(expect=[{"id": "checkout.subtotal", "label": "x"}]),
        CriterionFacts(run=_happy(status="absent", actual={"exists": False}), evidence=(REF,)),
        "not_verifiable",
        R.NO_OBSERVABLE,
    ),
    (
        "observation record missing",
        _criterion(),
        CriterionFacts(
            run=_run(
                _rec("launched", 101),
                _rec("surface", 102, status="ok"),
                _rec("step", 103, n=0, status="ok"),
                _rec("done", 105),
            ),
            evidence=(REF,),
        ),
        "not_verifiable",
        R.NO_RECORD,
    ),
    (
        "step record missing",
        _criterion(),
        CriterionFacts(
            run=_run(_rec("launched", 101), _rec("surface", 102, status="ok")), evidence=(REF,)
        ),
        "not_verifiable",
        R.NO_RECORD,
    ),
    (
        "log: exactly once",
        _criterion(**LOG_CRITERION),
        _log_facts([_log(100.5, 7, AGENT), _log(104, 7, ORDER)]),
        "pass",
        None,
    ),
    (
        "log: twice",
        _criterion(**LOG_CRITERION),
        _log_facts([_log(100.5, 7, AGENT), _log(104, 7, ORDER), _log(105, 7, ORDER)]),
        "fail",
        None,
    ),
    (
        "log: zero, capture proven live",
        _criterion(**LOG_CRITERION),
        _log_facts([_log(100.5, 7, AGENT)]),
        "fail",
        None,
    ),
    (
        "log: before navigation does not count",
        _criterion(**LOG_CRITERION),
        _log_facts([_log(100.5, 7, ORDER)]),
        "fail",
        None,
    ),
    (
        "log: no capture at all",
        _criterion(**LOG_CRITERION),
        _log_facts([]),
        "not_verifiable",
        R.LOG_UNAVAILABLE,
    ),
    (
        "log: two processes in the window",
        _criterion(**LOG_CRITERION),
        _log_facts([_log(100.5, 7, AGENT), _log(104, 8, ORDER)]),
        "not_verifiable",
        R.LOG_UNAVAILABLE,
    ),
    (
        "log: lines outside the window are ignored",
        _criterion(**LOG_CRITERION),
        _log_facts(
            [_log(50, 6, ORDER), _log(100.5, 7, AGENT), _log(104, 7, ORDER), _log(300, 9, ORDER)]
        ),
        "pass",
        None,
    ),
]


@pytest.mark.parametrize(
    ("criterion", "facts", "verdict", "reason"),
    [row[1:] for row in TABLE],
    ids=[row[0] for row in TABLE],
)
def test_derivation_table(criterion, facts, verdict, reason):
    result = derive(criterion, facts)
    assert (result.verdict.value, result.reason) == (verdict, reason), result.detail


def test_every_reason_is_in_the_table():
    covered = {row[4] for row in TABLE if row[4] is not None}
    assert covered == set(R), f"untested: {set(R) - covered}"


def test_log_fail_records_counts_and_lines():
    result = derive(
        _criterion(**LOG_CRITERION),
        _log_facts([_log(100.5, 7, AGENT), _log(104, 7, ORDER), _log(105, 7, ORDER)]),
    )
    (obs,) = result.observations
    assert obs.expected == {"count": 1} and obs.actual == {"count": 2}
    assert obs.lines == [ORDER, ORDER]


def test_log_lines_are_redacted():
    """V2: `.private` arrives in clear text, so evidence is scrubbed before it is kept."""
    secret = "[acceptance] SIGHTLINE-AC|order_submitted|email=kevin@example.com"
    result = derive(
        _criterion(**LOG_CRITERION), _log_facts([_log(100.5, 7, AGENT), _log(104, 7, secret)])
    )
    assert "kevin@example.com" not in result.observations[0].lines[0]


def test_no_artifact_never_passes():
    result = derive(_criterion(), CriterionFacts(run=_happy(), evidence=()))
    assert result.verdict is VerdictKind.NOT_VERIFIABLE and result.reason is R.NO_RECORD
