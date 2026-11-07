"""Verdict derivation: records in, verdict out. Pure; no Xcode.

ADR-0004 §3. This is the only module that may build a ``pass`` or ``fail``
``CriterionResult``, and it does so only from records the driver wrote into the
``.xcresult`` (or log lines attributed to this criterion's launch). Everything else is
``not_verifiable`` with a reason, and earlier reasons dominate later ones:

    manual → evidence_type_unsupported → build_failed / runner_unavailable → app_crashed
    → surface_unreachable → step_unreachable / ambiguous_element (steps, in order)
    → per observation: ambiguous_element, no_observable, no_record, log_unavailable
    → any observation false → fail → pass

so one crash never also reports a confusing ``no_record``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from sightline.core.acceptance.criteria import Criterion, Evidence, Observation
from sightline.core.acceptance.verdict import (
    _DERIVE_TOKEN,
    CriterionResult,
    NotVerifiableReason,
    ObservationResult,
    VerdictKind,
)
from sightline.core.evidence.models import ArtifactRef
from sightline.core.evidence.redaction import RedactionPolicy, redact_text
from sightline.runners.xcode.xcresult import AcceptanceRecord, AppLogLine, CriterionRun

R = NotVerifiableReason


@dataclass(frozen=True)
class LogWindow:
    """App log lines attributed to one criterion's launch (ADR-0004 §5)."""

    pids: frozenset[int]
    """Distinct PIDs with any unified-log line in the launch window. Exactly one proves
    capture was live for this launch; zero or several is ``log_unavailable``."""
    counted: tuple[AppLogLine, ...]
    """Lines from that PID after navigation completed — the ones events are counted in."""

    @property
    def live(self) -> bool:
        return len(self.pids) == 1


def attribute_log_lines(
    lines: tuple[AppLogLine, ...],
    *,
    launch: float | None,
    counting_from: float | None,
    end: float | None,
) -> LogWindow:
    """Window by time, confirm by PID.

    One output file holds every launch in the run and XCUIApplication exposes no PID
    (verification V3, V5), so a launch's lines are the ones between its ``Launch``
    activity and its end. XCTest's in-process agent always logs from the app's PID at
    launch, so a live capture has exactly one PID in the window. If a future Xcode stops
    doing that, this fails closed — ``log_unavailable`` — never open.
    """
    if launch is None or end is None:
        return LogWindow(pids=frozenset(), counted=())
    window = [line for line in lines if launch <= line.time <= end]
    pids = frozenset(line.pid for line in window)
    if len(pids) != 1:
        return LogWindow(pids=pids, counted=())
    start = counting_from if counting_from is not None else launch
    return LogWindow(pids=pids, counted=tuple(line for line in window if line.time >= start))


@dataclass(frozen=True)
class CriterionFacts:
    """Everything known about one criterion's run. Built by the orchestrator."""

    status: Literal["ran", "build_failed", "runner_unavailable"] = "ran"
    detail: str = ""
    run: CriterionRun | None = None
    log: LogWindow | None = None
    evidence: tuple[ArtifactRef, ...] = ()
    crash_evidence: tuple[ArtifactRef, ...] = ()
    duration_s: float = 0.0
    redaction: RedactionPolicy = field(default_factory=RedactionPolicy)


class _NotVerifiable(Exception):
    def __init__(self, reason: NotVerifiableReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def _nv(
    criterion: Criterion,
    reason: NotVerifiableReason,
    detail: str,
    facts: CriterionFacts | None = None,
    observations: list[ObservationResult] | None = None,
    evidence: tuple[ArtifactRef, ...] = (),
) -> CriterionResult:
    return CriterionResult.not_verifiable(
        id=criterion.id,
        given=criterion.given,
        when=criterion.when,
        then=criterion.then,
        evidence_type=criterion.evidence,
        reason=reason,
        detail=detail,
        observations=observations,
        evidence=list(evidence),
        duration_s=facts.duration_s if facts else 0.0,
    )


def _status(record: AcceptanceRecord | None) -> str | None:
    return None if record is None else str(record.fields.get("status", ""))


def _check_navigation(criterion: Criterion, run: CriterionRun) -> None:
    for alert in run.phase("alert"):
        raise _NotVerifiable(
            R.SURFACE_UNREACHABLE,
            f"a system alert covered the app ({alert.fields.get('label', '')!r}); "
            "add its button title to `dismiss_alerts:`",
        )
    surface = run.phase("surface")
    if not surface:
        raise _NotVerifiable(R.NO_RECORD, "the driver left no navigation record")
    record = surface[-1]
    status, target = _status(record), record.fields.get("target", "")
    if status == "ambiguous":
        raise _NotVerifiable(
            R.AMBIGUOUS_ELEMENT,
            f"navigation to `{criterion.surface}`: `{target}` matched "
            f"{record.fields.get('count')} elements",
        )
    if status != "ok":
        raise _NotVerifiable(
            R.SURFACE_UNREACHABLE,
            f"could not reach surface `{criterion.surface}`: `{target}` never became tappable",
        )

    steps = {int(r.fields.get("n", -1)): r for r in run.phase("step")}
    for n, step in enumerate(criterion.steps):
        record = steps.get(n)
        status = _status(record)
        if status == "ok":
            continue
        if record is None or status is None:
            raise _NotVerifiable(
                R.NO_RECORD, f"no record for step {n + 1} ({step.action} `{step.target}`)"
            )
        if status == "ambiguous":
            raise _NotVerifiable(
                R.AMBIGUOUS_ELEMENT,
                f"step {n + 1}: `{step.target}` matched {record.fields.get('count')} elements",
            )
        raise _NotVerifiable(
            R.STEP_UNREACHABLE,
            f"step {n + 1} ({step.action}): `{step.target}` was not found or never became "
            "tappable — the build lacks it, or the criteria name the wrong identifier",
        )


def _judge(o: Observation, actual: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Compare asserted attributes against the observed snapshot. Returns (actual, met)."""
    shown: dict[str, Any] = {}
    met = True
    for name, expected in o.attributes().items():
        key = "label" if name == "label_contains" else name
        value = actual.get(key)
        shown[key] = value
        if name == "label_contains":
            ok = isinstance(value, str) and str(expected) in value
        elif name == "value":
            ok = value is not None and str(value) == expected
        else:
            ok = value == expected
        met = met and ok
    return shown, met


def _observe_ui(
    criterion: Criterion, run: CriterionRun
) -> tuple[list[ObservationResult], list[tuple[NotVerifiableReason, str]]]:
    records = {int(r.fields.get("n", -1)): r for r in run.phase("expect")}
    results: list[ObservationResult] = []
    problems: list[tuple[NotVerifiableReason, str]] = []
    for n, o in enumerate(criterion.expect):
        expected: dict[str, Any] = dict(o.attributes())
        record = records.get(n)
        status = _status(record)
        if record is None:
            problems.append((R.NO_RECORD, f"no record for the expectation on `{o.id}`"))
            results.append(
                ObservationResult(kind=Evidence.UI_STATE, target=o.id, expected=expected)
            )
            continue
        if status == "ambiguous":
            problems.append(
                (R.AMBIGUOUS_ELEMENT, f"`{o.id}` matched {record.fields.get('count')} elements")
            )
            results.append(
                ObservationResult(kind=Evidence.UI_STATE, target=o.id, expected=expected)
            )
            continue
        if status == "absent":
            if o.exists is None:
                problems.append(
                    (
                        R.NO_OBSERVABLE,
                        (
                            f"`{o.id}` was not on screen to observe; add `exists: true` if "
                            "its absence should fail the criterion"
                        ),
                    )
                )
                results.append(
                    ObservationResult(
                        kind=Evidence.UI_STATE,
                        target=o.id,
                        expected=expected,
                        actual={"exists": False},
                    )
                )
                continue
            results.append(
                ObservationResult(
                    kind=Evidence.UI_STATE,
                    target=o.id,
                    expected=expected,
                    actual={"exists": False},
                    met=o.exists is False,
                )
            )
            continue
        if status != "observed":
            problems.append((R.NO_RECORD, f"unreadable record for the expectation on `{o.id}`"))
            results.append(
                ObservationResult(kind=Evidence.UI_STATE, target=o.id, expected=expected)
            )
            continue
        snapshot = record.fields.get("actual")
        if not isinstance(snapshot, dict):
            problems.append((R.NO_RECORD, f"the record for `{o.id}` carries no observed values"))
            results.append(
                ObservationResult(kind=Evidence.UI_STATE, target=o.id, expected=expected)
            )
            continue
        shown, met = _judge(o, snapshot)
        results.append(
            ObservationResult(
                kind=Evidence.UI_STATE, target=o.id, expected=expected, actual=shown, met=met
            )
        )
    return results, problems


def _observe_logs(
    criterion: Criterion, log: LogWindow | None, policy: RedactionPolicy
) -> tuple[list[ObservationResult], list[tuple[NotVerifiableReason, str]]]:
    if not criterion.expect_log:
        return [], []
    if log is None or not log.live:
        n = len(log.pids) if log else 0
        detail = (
            "no app log output was captured for this launch"
            if n == 0
            else f"log lines from {n} processes in this criterion's window; cannot attribute"
        )
        results = [
            ObservationResult(kind=Evidence.LOG_EVENT, target=lo.event, expected=lo.expected())
            for lo in criterion.expect_log
        ]
        return results, [(R.LOG_UNAVAILABLE, detail)]
    results = []
    for lo in criterion.expect_log:
        matching = [line for line in log.counted if line.event == lo.event]
        results.append(
            ObservationResult(
                kind=Evidence.LOG_EVENT,
                target=lo.event,
                expected=lo.expected(),
                actual={"count": len(matching)},
                met=lo.met_by(len(matching)),
                lines=[redact_text(line.message, policy) for line in matching],
            )
        )
    return results, []


_OBSERVATION_ORDER = (R.AMBIGUOUS_ELEMENT, R.NO_OBSERVABLE, R.NO_RECORD, R.LOG_UNAVAILABLE)


def derive(criterion: Criterion, facts: CriterionFacts) -> CriterionResult:
    if criterion.manual:
        return _nv(criterion, R.MANUAL, criterion.reason or "", facts)
    if criterion.evidence not in (Evidence.UI_STATE, Evidence.LOG_EVENT):
        return _nv(
            criterion,
            R.EVIDENCE_TYPE_UNSUPPORTED,
            f"`{criterion.evidence.value}` has no executor in v1",
            facts,
        )
    if facts.status == "build_failed":
        return _nv(criterion, R.BUILD_FAILED, facts.detail, facts, evidence=facts.evidence)
    if facts.status == "runner_unavailable":
        return _nv(criterion, R.RUNNER_UNAVAILABLE, facts.detail, facts)

    run = facts.run
    if run is not None and run.crashed:
        return _nv(
            criterion,
            R.APP_CRASHED,
            run.crash_detail or "the app crashed during this criterion",
            facts,
            evidence=facts.crash_evidence + facts.evidence,
        )
    if run is None or not run.phase("launched"):
        return _nv(criterion, R.NO_RECORD, "the driver left no record for this criterion", facts)

    try:
        _check_navigation(criterion, run)
    except _NotVerifiable as stop:
        return _nv(criterion, stop.reason, stop.detail, facts, evidence=facts.evidence)

    ui, ui_problems = _observe_ui(criterion, run)
    logs, log_problems = _observe_logs(criterion, facts.log, facts.redaction)
    observations = ui + logs
    problems = ui_problems + log_problems
    for reason in _OBSERVATION_ORDER:
        detail = "; ".join(d for r, d in problems if r is reason)
        if detail:
            return _nv(criterion, reason, detail, facts, observations, facts.evidence)

    if not observations:
        return _nv(criterion, R.NO_RECORD, "nothing was observed", facts)
    if not facts.evidence:
        return _nv(criterion, R.NO_RECORD, "no artifact backs this run", facts, observations)

    verdict = VerdictKind.PASS if all(o.met for o in observations) else VerdictKind.FAIL
    return CriterionResult(
        _DERIVE_TOKEN,
        id=criterion.id,
        given=criterion.given,
        when=criterion.when,
        then=criterion.then,
        evidence_type=criterion.evidence,
        verdict=verdict,
        observations=observations,
        evidence=list(facts.evidence),
        duration_s=facts.duration_s,
    )
