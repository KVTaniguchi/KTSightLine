"""The only module that knows the `.xcresult` JSON shape.

ADR-0003 §6: parse via `xcrun xcresulttool get test-results ...`, never `get object`,
never `--legacy`. The schema *is* published (verification P3), so we pin
`--schema-version` explicitly and a schema change becomes a loud error rather than
silent parse drift. That matters concretely — the macos-26 runner ships Xcode 26.4.1
while a developer machine may be on 26.6, so "it parsed locally" proves nothing.

Split in two on purpose:

* :class:`XcresultTool` shells out and returns raw JSON. Needs Xcode.
* The ``parse_*`` functions are pure and run anywhere, against captured JSON.

The captured fixtures in ``tests/fixtures/xcresult/`` are real output from a real run,
so the pure tests exercise real shapes without needing a Mac.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "0.1.0"
"""Pinned. Bumping this is a deliberate act with a fixture refresh, not a default."""

# The audit record the UI-test driver writes into activity titles.
# SIGHTLINE|<screen>|<auditType>|id=<id>|label=<label>|frame=<frame>|<compactDescription>
_AUDIT_RECORD = re.compile(r"^SIGHTLINE\|")
_SCREENSHOT_NAME = re.compile(r"^SIGHTLINE-SCREENSHOT-(?P<screen>[^_]+)")

# Severity lives in compactDescription, not auditType (measured on the fixture,
# 2026-08-31). Anything not "failed" is a sub-threshold warning and must not be posted,
# or the bot comments on every screen in the app including the correct ones.
_SUPPRESSED_DESCRIPTIONS = ("nearly passed", "partially unsupported")


class XcresultError(RuntimeError):
    pass


@dataclass(frozen=True)
class Device:
    """Becomes the ``context`` of every ArtifactRef produced from this run."""

    device_id: str
    device_name: str
    model_name: str
    os_version: str
    os_build: str
    platform: str
    architecture: str

    def as_context(self) -> dict[str, str]:
        return {
            "device": self.model_name,
            "device_name": self.device_name,
            "os": self.os_version,
            "os_build": self.os_build,
            "platform": self.platform,
            "arch": self.architecture,
        }


@dataclass(frozen=True)
class TestSummary:
    title: str
    result: str
    total: int
    passed: int
    failed: int
    skipped: int
    devices: tuple[Device, ...]
    environment: str = ""

    @property
    def device(self) -> Device | None:
        return self.devices[0] if self.devices else None


@dataclass(frozen=True)
class AuditIssue:
    """One accessibility-audit issue, recovered from an activity title."""

    screen: str
    audit_type: str
    identifier: str
    label: str
    frame: str
    description: str
    test_id: str

    @property
    def is_failure(self) -> bool:
        """False for 'nearly passed' / 'partially unsupported' warnings."""
        low = self.description.lower()
        return not any(s in low for s in _SUPPRESSED_DESCRIPTIONS)

    @property
    def is_attributable(self) -> bool:
        """An issue with no element has no identifier and no frame to anchor to.

        ADR-0002 forbids inventing one. See OQ-FIXTURE-1.
        """
        return bool(self.identifier) and self.frame != "nil"


@dataclass(frozen=True)
class Attachment:
    exported_file_name: str
    suggested_name: str
    test_id: str
    device_id: str
    device_name: str
    timestamp: float
    associated_with_failure: bool = False

    @property
    def screen(self) -> str | None:
        m = _SCREENSHOT_NAME.match(self.suggested_name)
        return m["screen"] if m else None


@dataclass(frozen=True)
class TestNode:
    node_type: str
    name: str
    identifier: str | None
    children: tuple[TestNode, ...] = field(default=())

    def test_cases(self) -> Iterator[TestNode]:
        if self.node_type == "Test Case" and self.identifier:
            yield self
        for child in self.children:
            yield from child.test_cases()


# --- pure parsers ---------------------------------------------------------------------


def parse_summary(data: dict[str, Any]) -> TestSummary:
    devices = tuple(
        Device(
            device_id=d["device"].get("deviceId", ""),
            device_name=d["device"].get("deviceName", ""),
            model_name=d["device"].get("modelName", ""),
            os_version=d["device"].get("osVersion", ""),
            os_build=d["device"].get("osBuildNumber", ""),
            platform=d["device"].get("platform", ""),
            architecture=d["device"].get("architecture", ""),
        )
        for d in data.get("devicesAndConfigurations", [])
        if "device" in d
    )
    return TestSummary(
        title=data.get("title", ""),
        result=data.get("result", ""),
        total=data.get("totalTestCount", 0),
        passed=data.get("passedTests", 0),
        failed=data.get("failedTests", 0),
        skipped=data.get("skippedTests", 0),
        devices=devices,
        environment=data.get("environmentDescription", ""),
    )


def parse_tests(data: dict[str, Any]) -> tuple[TestNode, ...]:
    def build(node: dict[str, Any]) -> TestNode:
        return TestNode(
            node_type=node.get("nodeType", ""),
            name=node.get("name", ""),
            identifier=node.get("nodeIdentifier"),
            children=tuple(build(c) for c in node.get("children", [])),
        )

    return tuple(build(n) for n in data.get("testNodes", []))


def _walk_activities(activities: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    for activity in activities:
        yield activity
        yield from _walk_activities(activity.get("childActivities", []))


def parse_audit_issues(data: dict[str, Any]) -> tuple[AuditIssue, ...]:
    """Recover audit issues from activity titles.

    Activity titles are how the UI-test driver hands structured data across the test
    boundary; XCTest has no other channel that survives into the result bundle.
    """
    test_id = data.get("testIdentifier", "")
    issues: list[AuditIssue] = []
    for run in data.get("testRuns", []):
        for activity in _walk_activities(run.get("activities", [])):
            title = activity.get("title", "")
            if not _AUDIT_RECORD.match(title):
                continue
            parts = title.split("|", 6)
            if len(parts) < 7:
                continue
            _, screen, audit_type, id_field, label_field, frame_field, description = parts
            issues.append(
                AuditIssue(
                    screen=screen,
                    audit_type=audit_type,
                    identifier=id_field.removeprefix("id="),
                    label=label_field.removeprefix("label="),
                    frame=frame_field.removeprefix("frame="),
                    description=description,
                    test_id=test_id,
                )
            )
    return tuple(issues)


def parse_attachments_manifest(data: list[dict[str, Any]]) -> tuple[Attachment, ...]:
    out: list[Attachment] = []
    for entry in data:
        test_id = entry.get("testIdentifier", "")
        for a in entry.get("attachments", []):
            out.append(
                Attachment(
                    exported_file_name=a.get("exportedFileName", ""),
                    suggested_name=a.get("suggestedHumanReadableName", ""),
                    test_id=test_id,
                    device_id=a.get("deviceId", ""),
                    device_name=a.get("deviceName", ""),
                    timestamp=a.get("timestamp", 0.0),
                    associated_with_failure=a.get("isAssociatedWithFailure", False),
                )
            )
    return tuple(out)


def postable_issues(issues: tuple[AuditIssue, ...]) -> tuple[AuditIssue, ...]:
    """Apply the two suppression rules that make the clean screen actually clean."""
    return tuple(i for i in issues if i.is_failure and i.is_attributable)


# --- the shelling-out half ------------------------------------------------------------


class XcresultTool:
    """Thin wrapper over `xcrun xcresulttool`. Vendored, not an MCP dependency (D4)."""

    def __init__(self, bundle: Path, *, schema_version: str = SCHEMA_VERSION) -> None:
        self.bundle = Path(bundle)
        self.schema_version = schema_version

    def _get(self, *args: str) -> Any:
        cmd = [
            "xcrun",
            "xcresulttool",
            "get",
            "test-results",
            *args,
            "--path",
            str(self.bundle),
            "--schema-version",
            self.schema_version,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise XcresultError(
                f"{' '.join(cmd)} failed ({proc.returncode}). "
                f"A schema-version rejection here is the intended loud failure: {proc.stderr.strip()}"
            )
        return json.loads(proc.stdout)

    @staticmethod
    def tool_version() -> str:
        proc = subprocess.run(
            ["xcrun", "xcresulttool", "version"], capture_output=True, text=True, check=False
        )
        return proc.stdout.strip()

    def summary(self) -> TestSummary:
        return parse_summary(self._get("summary"))

    def tests(self) -> tuple[TestNode, ...]:
        return parse_tests(self._get("tests"))

    def audit_issues(self, test_id: str) -> tuple[AuditIssue, ...]:
        return parse_audit_issues(self._get("activities", "--test-id", test_id))

    def all_audit_issues(self) -> tuple[AuditIssue, ...]:
        out: list[AuditIssue] = []
        for root in self.tests():
            for case in root.test_cases():
                out.extend(self.audit_issues(case.identifier or ""))
        return tuple(out)

    def export_attachments(self, dest: Path) -> tuple[Attachment, ...]:
        """Export every attachment and return the manifest.

        This is the `screenshot` ArtifactRef producer for the evidence store.
        """
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        # xcresulttool refuses to write a manifest over an existing one, which makes a
        # re-run fail rather than repeat. Clear it so exporting is idempotent; the
        # attachment files themselves are content-named and safe to overwrite.
        (dest / "manifest.json").unlink(missing_ok=True)
        cmd = [
            "xcrun", "xcresulttool", "export", "attachments",
            "--path", str(self.bundle), "--output-path", str(dest),
        ]  # fmt: skip
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise XcresultError(f"export attachments failed: {proc.stderr.strip()}")
        manifest = dest / "manifest.json"
        if not manifest.exists():
            return ()
        return parse_attachments_manifest(json.loads(manifest.read_text()))
