"""Acceptance driver rendering (ADR-0004 §4). Linux-testable except the Swift checks.

The trap: criteria text is often model-written and travels into generated Swift. A `"`,
`\\`, newline, or `\\(` in a label must not break the build or get evaluated as
interpolation. The driver carries all of it in one base64 payload; these tests prove the
payload decodes back exactly and nothing else from the criteria reaches the source.
"""

from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from sightline.config import RepoConfig
from sightline.core.acceptance.criteria import CriteriaFile
from sightline.runners.xcode.acceptance_driver import (
    DRIVER_CLASS,
    build_payload,
    decode_record,
    render_acceptance_driver,
)

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "eval" / "fixtures" / "CheckoutDemo"
EXAMPLE = REPO / "eval" / "acceptance" / "examples" / "CHK-7.yml"

HOSTILE = [
    'quote " inside',
    "backslash \\ inside",
    "line\nbreak",
    "interpolation \\(evil()) inside",
    'close the literal "); fatalError("',
    "pipe | and SIGHTLINE-ACC| inside",
    "emoji 🐱 and RTL \u200f עברית",
    "*/ /* comment markers",
    '#"raw"#',
]


def _config(**extra) -> RepoConfig:
    base = RepoConfig.load(FIXTURE).model_dump()
    base.update(extra)
    return RepoConfig.model_validate(base)


def _hostile_criteria() -> CriteriaFile:
    criteria = []
    for n, text in enumerate(HOSTILE):
        criteria.append(
            {
                "id": f"H-{n}",
                "given": text,
                "when": text,
                "then": text,
                "surface": "CheckoutSummary",
                "fixture": "hostile",
                "steps": [{"type": {"into": "search.field", "text": text}}],
                "expect": [{"id": "checkout.subtotal", "label": text, "label_contains": text}],
            }
        )
    return CriteriaFile.model_validate(
        {"acceptance_schema": 1, "spec_id": "HOSTILE", "criteria": criteria}
    )


def _hostile_config() -> RepoConfig:
    return _config(
        fixtures={
            "hostile": {
                "launch_arguments": ["-Flag", *HOSTILE],
                "launch_environment": {f"K{n}": v for n, v in enumerate(HOSTILE)},
            }
        }
    )


def _embedded_payload(source: str) -> dict:
    (literal,) = re.findall(r'private static let encoded = "([A-Za-z0-9+/=]*)"', source)
    return json.loads(base64.b64decode(literal).decode("utf-8"))


def test_hostile_strings_round_trip_through_the_payload():
    criteria, config = _hostile_criteria(), _hostile_config()
    rendered = render_acceptance_driver(criteria, config)
    payload = _embedded_payload(rendered.source)
    assert payload == json.loads(json.dumps(build_payload(criteria.criteria, config)))
    for spec, text in zip(payload["criteria"], HOSTILE, strict=True):
        assert spec["steps"][0]["text"] == text
        assert spec["expect"][0]["label"] == text
        assert spec["expect"][0]["labelContains"] == text
        assert text in spec["launchArguments"]
        assert text in spec["launchEnvironment"].values()


def test_no_criterion_text_reaches_swift_source():
    rendered = render_acceptance_driver(_hostile_criteria(), _hostile_config())
    for text in HOSTILE:
        assert text not in rendered.source
    assert "evil()" not in rendered.source
    assert rendered.source.isascii(), "only the fixed template and base64 may appear"


def test_one_method_per_executable_criterion():
    rendered = render_acceptance_driver(CriteriaFile.load(EXAMPLE), _config())
    # AC4 is manual: decided without a simulator, so it gets no method.
    assert rendered.criterion_ids == ("CHK-7.AC1", "CHK-7.AC2", "CHK-7.AC3")
    assert rendered.method_names == ("test_AC_CHK_7_AC1", "test_AC_CHK_7_AC2", "test_AC_CHK_7_AC3")
    for i, name in enumerate(rendered.method_names):
        assert f"    func {name}() {{ run({i}) }}" in rendered.source
    assert f"final class {DRIVER_CLASS}: XCTestCase" in rendered.source


def test_launch_order_is_locale_then_surface_then_fixture():
    config = _config(
        acceptance={"language": "en", "locale": "en_US"},
        fixtures={
            "empty": {"launch_arguments": ["-cart", "empty"], "launch_environment": {"A": "1"}}
        },
    )
    config.surfaces["CheckoutSummary"].launch_arguments.append("-surfaceArg")
    criteria = CriteriaFile.model_validate(
        {
            "acceptance_schema": 1,
            "spec_id": "S",
            "criteria": [
                {
                    "id": "A1",
                    "given": "g",
                    "when": "w",
                    "then": "t",
                    "surface": "CheckoutSummary",
                    "fixture": "empty",
                    "expect": [{"id": "x", "exists": True}],
                }
            ],
        }
    )
    (spec,) = build_payload(criteria.criteria, config)["criteria"]
    assert spec["launchArguments"] == [
        "-AppleLanguages", "(en)", "-AppleLocale", "en_US", "-surfaceArg", "-cart", "empty",
    ]  # fmt: skip
    assert spec["launchEnvironment"] == {"A": "1"}
    assert spec["navigation"] == ["cart.continue"]
    assert spec["waitFor"] == "checkout.payment"


def test_record_decoding_matches_swift_encoding():
    """The Swift `record` helper emits unpadded base64url JSON."""
    body = {"v": 1, "i": 0, "phase": "expect", "actual": {"label": 'a|b\n🐱 \\(x) "q"'}}
    raw = json.dumps(body).encode()
    swift_style = base64.b64encode(raw).decode().replace("+", "-").replace("/", "_").rstrip("=")
    assert decode_record(swift_style) == body


def test_template_uses_only_verified_api():
    """V5: these exist in Xcode 27.1. Guard against an invented one creeping in."""
    source = render_acceptance_driver(CriteriaFile.load(EXAMPLE), _config()).source
    for api in ("waitForNonExistence(timeout:", "waitForExistence(timeout:", ".launchEnvironment"):
        assert api in source
    assert "sleep(" not in source and "Thread.sleep" not in source, "bounded waits only"
    assert "firstMatch.tap()" not in source, "never act on firstMatch"
    assert "XCTAssert" not in source and "XCTFail" not in source, "the driver never asserts"


# --- Swift-level checks, where Xcode exists ---------------------------------------------

_XCRUN = shutil.which("xcrun")


def _platform_path() -> str | None:
    if not _XCRUN:
        return None
    proc = subprocess.run(
        ["xcrun", "--sdk", "iphonesimulator", "--show-sdk-platform-path"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip() or None


@pytest.mark.skipif(_XCRUN is None, reason="needs Xcode")
def test_hostile_driver_typechecks(tmp_path):
    """The real proof: the generated file type-checks against the iOS simulator SDK."""
    platform = _platform_path()
    if not platform:
        pytest.skip("no iphonesimulator platform")
    developer = Path(platform) / "Developer"
    frameworks = developer / "Library" / "Frameworks"
    driver = tmp_path / "SightlineAcceptance.swift"
    driver.write_text(render_acceptance_driver(_hostile_criteria(), _hostile_config()).source)
    proc = subprocess.run(
        [
            "xcrun", "--sdk", "iphonesimulator", "swiftc", "-typecheck",
            "-target", "arm64-apple-ios17.0-simulator",
            "-F", str(frameworks),
            "-I", str(developer / "usr" / "lib"),
            str(driver),
        ],
        capture_output=True,
        text=True,
        check=False,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr
