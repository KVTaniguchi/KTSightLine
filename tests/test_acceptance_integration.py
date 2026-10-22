"""The acceptance tier end to end, against CheckoutDemo on a real simulator.

Slow (a build plus four app launches) and needs Xcode, so it is opt-in, with the same
switches as tests/test_injection.py:

    SIGHTLINE_INTEGRATION=1 SIGHTLINE_SIM_UDID=<udid> uv run pytest tests/test_acceptance_integration.py
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from sightline.acceptance import AcceptOptions, run_accept
from sightline.core.acceptance.verdict import NotVerifiableReason as R
from sightline.core.acceptance.verdict import VerdictKind as V
from sightline.eval_harness import _apply_patch

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "eval" / "fixtures" / "CheckoutDemo"
EXAMPLE = REPO / "eval" / "acceptance" / "examples" / "CHK-7.yml"
CASES = REPO / "eval" / "acceptance"

_INTEGRATION = os.environ.get("SIGHTLINE_INTEGRATION") == "1"
_UDID = os.environ.get("SIGHTLINE_SIM_UDID", "")

integration = pytest.mark.skipif(
    not (_INTEGRATION and _UDID and shutil.which("xcodebuild")),
    reason="set SIGHTLINE_INTEGRATION=1 and SIGHTLINE_SIM_UDID to run",
)


def _accept(tmp_path: Path, checkout: Path):
    return run_accept(
        AcceptOptions(
            criteria=EXAMPLE,
            checkout=checkout,
            udid=_UDID,
            out_dir=tmp_path / "out",
            scratch_dir=tmp_path / "scratch",
        )
    ).report


def _verdicts(report) -> dict[str, tuple[V, R | None]]:
    return {c.id: (c.verdict, c.reason) for c in report.criteria}


@integration
def test_example_criteria_against_the_fixture(tmp_path):
    report = _accept(tmp_path, FIXTURE)
    assert report.status == "completed"
    verdicts = _verdicts(report)
    assert verdicts["CHK-7.AC1"] == (V.PASS, None)
    assert verdicts["CHK-7.AC2"] == (V.PASS, None)
    # The base fixture logs nothing: capture is live, the event never came.
    assert verdicts["CHK-7.AC3"] == (V.FAIL, None)
    assert verdicts["CHK-7.AC4"] == (V.NOT_VERIFIABLE, R.MANUAL)
    for c in report.criteria:
        if c.verdict is V.PASS:
            kinds = {ref.kind.value for ref in c.evidence}
            assert {"xcresult", "screenshot"} <= kinds, c.id


@integration
def test_logged_order_and_broken_subtotal(tmp_path):
    """The two definition-of-done patches, applied together: AC3 passes on the log line,
    AC1 fails with the observed wrong subtotal in the report."""
    app = tmp_path / "app"
    shutil.copytree(FIXTURE, app)
    _apply_patch(app, CASES / "004-order-logged-once" / "change.patch")
    _apply_patch(app, CASES / "002-subtotal-ignores-quantity" / "change.patch")
    report = _accept(tmp_path, app)
    verdicts = _verdicts(report)
    assert verdicts["CHK-7.AC3"] == (V.PASS, None)
    assert verdicts["CHK-7.AC1"] == (V.FAIL, None)
    ac1 = next(c for c in report.criteria if c.id == "CHK-7.AC1")
    (obs,) = ac1.observations
    assert obs.expected["label"] == "Subtotal $441.00"
    assert obs.actual is not None and obs.actual["label"] == "Subtotal $377.00"
