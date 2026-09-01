"""xcresult parsing and simulator lifecycle.

The xcresult tests run against JSON captured from real runs on iPhone SE (3rd gen) /
iOS 26.5 / xcresulttool 24757, so they exercise real shapes without needing a Mac. One
Xcode-gated test re-parses a live bundle to catch schema drift.

The simulator tests inject a fake runner and assert on the exact argv, because the whole
point of vendoring these wrappers (D4) is that we own the flags.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from sightline.runners.simulator.device import (
    Appearance,
    ContentSize,
    SimctlError,
    Simulator,
)
from sightline.runners.xcode.xcresult import (
    SCHEMA_VERSION,
    parse_attachments_manifest,
    parse_audit_issues,
    parse_summary,
    parse_tests,
    postable_issues,
)

F = Path(__file__).resolve().parent / "fixtures/xcresult"


def load(name: str):
    return json.loads((F / name).read_text())


# --- xcresult: summary ---------------------------------------------------------------


def test_summary_carries_the_device_context_a_finding_needs():
    s = parse_summary(load("summary.json"))
    assert s.result == "Passed"
    assert s.device is not None
    ctx = s.device.as_context()
    assert ctx["device"] == "iPhone SE (3rd generation)"
    assert ctx["os"] == "26.5"
    assert ctx["platform"] == "iOS Simulator"
    assert ctx["arch"] == "arm64"


def test_summary_counts():
    s = parse_summary(load("summary.json"))
    assert (s.total, s.passed, s.failed) == (1, 1, 0)


def test_summary_of_empty_data_does_not_explode():
    s = parse_summary({})
    assert s.device is None and s.total == 0


# --- xcresult: tests -----------------------------------------------------------------


def test_tests_tree_yields_identifiable_test_cases():
    roots = parse_tests(load("tests.json"))
    ids = [c.identifier for r in roots for c in r.test_cases()]
    assert "CheckoutDemoUITests/testCheckoutSummaryScreen()" in ids


# --- xcresult: audit issues, the part that decides what gets posted ------------------


def test_cart_screen_filters_ten_issues_down_to_the_two_seeded_defects():
    """The precision story, end to end, on real captured output.

    Ten raw issues on the Cart screen. Two survive: the seeded unlabelled button
    (D-004) and the seeded 16x16 tap target (D-005). The other eight are sub-threshold
    warnings or unattributable failures.
    """
    issues = parse_audit_issues(load("activities-cart.json"))
    assert len(issues) == 10

    postable = postable_issues(issues)
    assert {(i.audit_type, i.identifier) for i in postable} == {
        ("sufficientElementDescription", "cart.help"),
        ("hitRegion", "cart.removeItem"),
    }


def test_nearly_passed_is_suppressed():
    issues = parse_audit_issues(load("activities-cart.json"))
    warnings = [i for i in issues if "nearly passed" in i.description]
    assert warnings, "fixture should contain warnings"
    assert all(not i.is_failure for i in warnings)


def test_partially_unsupported_is_suppressed():
    issues = parse_audit_issues(load("activities-cart.json"))
    dt = [i for i in issues if i.audit_type == "dynamicType"]
    assert dt and all(not i.is_failure for i in dt)


def test_unattributable_failures_are_not_postable():
    """OQ-FIXTURE-1: element = nil means no identifier and no frame to anchor to.

    ADR-0002 forbids inventing one, so these are dropped rather than mis-anchored.
    """
    issues = parse_audit_issues(load("activities-cart.json"))
    orphans = [i for i in issues if i.is_failure and not i.identifier]
    assert orphans, "fixture should contain unattributable failures"
    assert all(i not in postable_issues(issues) for i in orphans)


def test_checkout_screen_yields_nothing_postable():
    """Its only failure is unattributable; its other two issues are warnings."""
    issues = parse_audit_issues(load("activities.json"))
    assert len(issues) == 3
    assert postable_issues(issues) == ()


def test_audit_record_keeps_a_description_containing_pipes():
    data = {
        "testIdentifier": "T/t()",
        "testRuns": [
            {"activities": [{"title": "SIGHTLINE|Cart|contrast|id=a|label=b|frame=c|has | pipes"}]}
        ],
    }
    (issue,) = parse_audit_issues(data)
    assert issue.description == "has | pipes"
    assert issue.identifier == "a"


def test_non_audit_activities_are_ignored():
    data = {"testRuns": [{"activities": [{"title": "Launch com.example.app"}]}]}
    assert parse_audit_issues(data) == ()


def test_audit_records_are_found_in_nested_activities():
    data = {
        "testRuns": [
            {
                "activities": [
                    {
                        "title": "Outer",
                        "childActivities": [
                            {"title": "SIGHTLINE|S|trait|id=x|label=y|frame=z|Trait failed"}
                        ],
                    }
                ]
            }
        ]
    }
    assert len(parse_audit_issues(data)) == 1


# --- xcresult: attachments ------------------------------------------------------------


def test_attachment_manifest_links_screenshot_to_screen_and_device():
    (a,) = parse_attachments_manifest(load("attachments-manifest.json"))
    assert a.screen == "CheckoutSummary"
    assert a.exported_file_name.endswith(".png")
    assert a.device_name == "Sightline iPhone SE (3rd generation)"
    assert a.test_id == "CheckoutDemoUITests/testCheckoutSummaryScreen()"


# --- schema drift ---------------------------------------------------------------------

_HAS_XCODE = shutil.which("xcrun") is not None and (
    subprocess.run(
        ["xcrun", "xcresulttool", "version"], capture_output=True, check=False
    ).returncode
    == 0
)


@pytest.mark.skipif(not _HAS_XCODE, reason="needs Xcode")
def test_pinned_schema_version_is_still_accepted():
    """The loud failure ADR-0003 §6 asks for: if Apple drops our pinned schema, fail here."""
    proc = subprocess.run(
        ["xcrun", "xcresulttool", "get", "test-results", "summary", "--schema",
         "--schema-version", SCHEMA_VERSION],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert proc.returncode == 0, f"pinned schema {SCHEMA_VERSION} rejected: {proc.stderr}"


# --- simulator: we own the flags -----------------------------------------------------


class FakeRunner:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.calls: list[list[str]] = []
        self._rc, self._out, self._err = returncode, stdout, stderr

    def __call__(self, cmd):
        self.calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, self._rc, self._out, self._err)


def test_content_size_uses_the_verified_ax5_spelling():
    r = FakeRunner()
    Simulator("UDID", run=r).set_content_size(ContentSize.AX5)
    assert r.calls == [
        ["xcrun", "simctl", "ui", "UDID", "content_size",
         "accessibility-extra-extra-extra-large"]
    ]  # fmt: skip


def test_appearance_dark():
    r = FakeRunner()
    Simulator("UDID", run=r).set_appearance(Appearance.DARK)
    assert r.calls[0][-2:] == ["appearance", "dark"]


def test_boot_uses_bootstatus_not_sleep():
    r = FakeRunner()
    Simulator("UDID", run=r).boot()
    assert r.calls == [["xcrun", "simctl", "bootstatus", "UDID", "-b"]]


def test_boot_retries_on_an_erased_device():
    """A simulator that failed to boot once usually fails again; erase between tries."""
    r = FakeRunner(returncode=1, stderr="boot timed out")
    with pytest.raises(SimctlError, match="failed to boot after 3 attempts"):
        Simulator("UDID", run=r).boot()
    verbs = [c[2] for c in r.calls]
    assert verbs == [
        "bootstatus", "shutdown", "erase",
        "bootstatus", "shutdown", "erase",
        "bootstatus",
    ]  # fmt: skip


def test_freeze_status_bar_pins_every_mutable_element():
    r = FakeRunner()
    Simulator("UDID", run=r).freeze_status_bar()
    argv = r.calls[0]
    for flag in (
        "--time", "--dataNetwork", "--wifiMode", "--wifiBars", "--cellularMode",
        "--cellularBars", "--operatorName", "--batteryState", "--batteryLevel",
    ):  # fmt: skip
        assert flag in argv


def test_revoke_privacy_rejects_camera_with_a_useful_message():
    """Found while verifying P6/P7: camera is not a simctl privacy service."""
    r = FakeRunner()
    with pytest.raises(SimctlError, match="Camera and notifications are NOT"):
        Simulator("UDID", run=r).revoke_privacy("camera", "com.example.app")
    assert r.calls == []


def test_revoke_privacy_allows_a_real_service():
    r = FakeRunner()
    Simulator("UDID", run=r).revoke_privacy("photos", "com.example.app")
    assert r.calls[0][2:] == ["privacy", "UDID", "revoke", "photos", "com.example.app"]


def test_prepare_returns_the_artifact_context():
    r = FakeRunner()
    ctx = Simulator("UDID", run=r).prepare(content_size=ContentSize.AX5, appearance=Appearance.DARK)
    assert ctx == {
        "udid": "UDID",
        "content_size": "accessibility-extra-extra-extra-large",
        "appearance": "dark",
        "status_bar": "frozen",
    }
    verbs = [c[2] for c in r.calls]
    assert verbs == ["bootstatus", "status_bar", "ui", "ui"]


def test_failed_simctl_call_raises_with_the_stderr():
    r = FakeRunner(returncode=1, stderr="Invalid device")
    with pytest.raises(SimctlError, match="Invalid device"):
        Simulator("UDID", run=r).set_appearance(Appearance.DARK)


def test_export_attachments_clears_a_stale_manifest(tmp_path, monkeypatch):
    """xcresulttool refuses to overwrite manifest.json, so a re-run must clear it."""
    from sightline.runners.xcode import xcresult as mod

    dest = tmp_path / "out"
    dest.mkdir()
    stale = dest / "manifest.json"
    stale.write_text("[]")
    seen = {}

    def fake_run(cmd, **kw):
        seen["manifest_existed"] = stale.exists()
        stale.write_text("[]")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    mod.XcresultTool(tmp_path / "b.xcresult").export_attachments(dest)
    assert seen["manifest_existed"] is False, "stale manifest should be removed first"


def test_status_bar_time_is_a_bare_time_string():
    """simctl rejects ISO date strings despite its help text saying otherwise.

    Verified against iOS 26.5 on 2026-08-31: every ISO form returned
    "Invalid, non-ISO date/time string". A regression here silently unpins the clock,
    and a ticking clock produces a render diff on every run.
    """
    r = FakeRunner()
    Simulator("UDID", run=r).freeze_status_bar()
    argv = r.calls[0]
    assert argv[argv.index("--time") + 1] == "9:41"
