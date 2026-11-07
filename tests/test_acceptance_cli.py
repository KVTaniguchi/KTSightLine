"""`sightline accept` and `sightline lint`, on the paths that need no simulator.

The report is written on every run (ADR-0004 §7), including the ones that never got a
simulator — those must say so per criterion, never read as "nothing failed".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from sightline.acceptance import AcceptOptions, CriteriaInvalid, run_accept
from sightline.cli import main
from sightline.core.acceptance.verdict import NotVerifiableReason as R

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "eval" / "fixtures" / "CheckoutDemo"
EXAMPLE = REPO / "eval" / "acceptance" / "examples" / "CHK-7.yml"


def _no_simulator(tmp_path: Path, **over) -> AcceptOptions:
    """A config with no UDID: the run reaches the simulator step and stops there."""
    kw = {
        "criteria": EXAMPLE,
        "checkout": FIXTURE,
        "udid": None,
        "out_dir": tmp_path / "out",
        "scratch_dir": tmp_path / "scratch",
    }
    kw.update(over)
    return AcceptOptions(**kw)


def test_unavailable_runner_still_writes_a_report(tmp_path):
    result = run_accept(_no_simulator(tmp_path))
    report = json.loads(result.report_path.read_text())
    assert report["status"] == "runner_unavailable"
    assert report["spec_id"] == "CHK-7"
    assert report["provenance"] == {"harness_version": "3f9c2e1", "stage": "implement"}
    verdicts = {c["id"]: (c["verdict"], c["reason"]) for c in report["criteria"]}
    assert verdicts == {
        "CHK-7.AC1": ("not_verifiable", "runner_unavailable"),
        "CHK-7.AC2": ("not_verifiable", "runner_unavailable"),
        "CHK-7.AC3": ("not_verifiable", "runner_unavailable"),
        "CHK-7.AC4": ("not_verifiable", "manual"),
    }
    assert any("no simulator UDID" in n for n in report["notes"])
    assert result.trajectory_path.exists()
    assert result.report.exit_code(strict=True) == 2


def test_manual_only_criteria_need_no_simulator(tmp_path):
    criteria = tmp_path / "manual.yml"
    criteria.write_text(
        "acceptance_schema: 1\nspec_id: M\ncriteria:\n"
        "  - {id: A1, given: g, when: w, then: t, manual: true, reason: outside the app}\n"
        "  - {id: A2, given: g, when: w, then: t, evidence: persisted_state}\n"
    )
    result = run_accept(_no_simulator(tmp_path, criteria=criteria))
    assert result.report.status == "completed"
    assert [c.reason for c in result.report.criteria] == [R.MANUAL, R.EVIDENCE_TYPE_UNSUPPORTED]
    assert result.report.exit_code(strict=True) == 0


def test_lint_errors_stop_the_run(tmp_path):
    criteria = tmp_path / "bad.yml"
    criteria.write_text(
        "acceptance_schema: 1\nspec_id: B\ncriteria:\n"
        "  - {id: A1, given: g, when: w, then: t, surface: Nowhere}\n"
    )
    with pytest.raises(CriteriaInvalid) as caught:
        run_accept(_no_simulator(tmp_path, criteria=criteria))
    assert any("surface `Nowhere` is not configured" in str(i) for i in caught.value.issues)
    assert not (tmp_path / "out").exists() or not any((tmp_path / "out").rglob("acceptance-*.json"))


def test_summary_file_is_appended(tmp_path):
    summary = tmp_path / "summary.md"
    summary.write_text("existing\n")
    run_accept(_no_simulator(tmp_path, summary_file=summary))
    text = summary.read_text()
    assert text.startswith("existing\n")
    assert "### Acceptance: CHK-7" in text
    assert "runner unavailable" in text


# --- the CLI ---------------------------------------------------------------------------


def _invoke(*args: str):
    return CliRunner().invoke(main, list(args))


def test_cli_accept_exit_codes(tmp_path):
    base = [
        "accept", "--criteria", str(EXAMPLE), "--path", str(FIXTURE),
        "--out", str(tmp_path / "out"), "--scratch", str(tmp_path / "scratch"),
    ]  # fmt: skip
    default = _invoke(*base)
    assert default.exit_code == 0, default.output
    assert "runner_unavailable" in default.output
    strict = _invoke(*base, "--strict")
    assert strict.exit_code == 2


def test_cli_accept_rejects_bad_criteria_with_exit_2(tmp_path):
    bad = tmp_path / "bad.yml"
    bad.write_text(
        "acceptance_schema: 1\nspec_id: B\ncriteria:\n  - {id: A1, given: g, when: w, then: t}\n"
    )
    result = _invoke("accept", "--criteria", str(bad), "--path", str(FIXTURE))
    assert result.exit_code == 2
    assert "needs at least one `expect`" in result.output
    assert "nothing ran" in result.output


def test_cli_lint(tmp_path):
    clean = _invoke("lint", "--criteria", str(EXAMPLE), "--path", str(FIXTURE))
    assert clean.exit_code == 0 and "0 error(s)" in clean.output
    bad = tmp_path / "bad.yml"
    bad.write_text(
        "acceptance_schema: 1\nspec_id: B\ncriteria:\n"
        "  - {id: A1, given: g, when: w, then: t, steps: [{swipe: x}], expect: [{id: x, exists: true}]}\n"
    )
    result = _invoke("lint", "--criteria", str(bad), "--path", str(FIXTURE))
    assert result.exit_code == 1
    assert "step `swipe` is not in the v1 vocabulary" in result.output
