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

import base64
import json
import re
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "0.1.0"
"""Pinned. Bumping this is a deliberate act with a fixture refresh, not a default."""

# The audit record the UI-test driver writes into activity titles.
# SIGHTLINE|<screen>|<auditType>|id=<id>|label=<label>|frame=<frame>|<compactDescription>
_AUDIT_RECORD = re.compile(r"^SIGHTLINE\|")
# Written by the driver after performAccessibilityAudit returns: SIGHTLINE-AUDITED|<screen>
_AUDITED_RECORD = re.compile(r"^SIGHTLINE-AUDITED\|(?P<screen>.+)$")
_SCREENSHOT_NAME = re.compile(r"^SIGHTLINE-SCREENSHOT-(?P<screen>[^_]+)")

# The acceptance driver's records (ADR-0004 §4): SIGHTLINE-ACC|<base64url(JSON)>.
_ACCEPTANCE_RECORD = re.compile(r"^SIGHTLINE-ACC\|(?P<body>[A-Za-z0-9_-]*)$")
_ACCEPTANCE_SHOT = re.compile(r"^SIGHTLINE-ACC-SHOT-(?P<index>\d+)")
# XCTest's own activities, measured on Xcode 27.1 (verification-2026-10-11 V3, V4).
_LAUNCH_ACTIVITY = re.compile(r"^Launch (?P<bundle>\S+)$")
_CRASH_ACTIVITY = re.compile(r"^(?P<bundle>\S+) crashed$")
_CRASH_LOG_ATTACHMENT = re.compile(r"^Crash Log .*\.ips$")
# One unified-log line mirrored into the app's stdout/stderr capture:
# 2026-10-11 13:13:27.764809-0400 CheckoutDemo[18307:951164] [acceptance] SIGHTLINE-AC|...
_APP_LOG_LINE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+[+-]\d{4}) "
    r"(?P<process>[^\[\n]+)\[(?P<pid>\d+):(?P<tid>[0-9a-fA-Fx]+)\] (?P<message>.*)$"
)
_AC_EVENT = re.compile(r"SIGHTLINE-AC\|(?P<event>[a-z][a-z0-9_]*)(?:\||$)")
APP_OUTPUT_GLOB = "StandardOutputAndStandardError-*.txt"

LABEL_ANCHORABLE_AUDIT_TYPES = frozenset({"textClipped"})
"""Audit types allowed to anchor by label when the element has no identifier.

An allowlist, not a general capability, and the reasoning is precision. `textClipped`
is the type OQ-FIXTURE-2 was about: it reports the clipped text element itself, which
in SwiftUI is routinely identifier-less, so without this it is unanchorable in
practice.

`contrast` is deliberately absent. Measured on eval case 003 (2026-10-20): the audit
reports `Contrast failed` against the inner label of a stock
`.buttonStyle(.bordered)` button — tinted text on a near-white fill, about 3.5:1
against a 4.5:1 threshold. The finding is *correct*, but it is a property of a system
button style rather than of the diff, so anchoring it by label would comment on
`.bordered` buttons across every adopting repo. Same reasoning as case 001's note
about stock SF Symbols. Admitting `contrast` needs a way to tell author-chosen colours
from inherited ones first; see the open question.
"""

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
    failures: tuple[str, ...] = ()

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
    def has_element(self) -> bool:
        """Whether the audit attached an element at all.

        The driver writes ``frame=nil`` exactly when ``issue.element`` was nil, so this
        is the one signal that separates "the audit could not say what it was about"
        from "it said, but the view carries no identifier".
        """
        return self.frame != "nil"

    @property
    def is_attributable(self) -> bool:
        """Whether anything in this record can anchor a comment to a line.

        Two distinct cases, collapsed into one before 2026-10-20 (see OQ-FIXTURE-2):

        * ``identifier`` set — anchor by searching the head source for it.
        * no identifier, but an element with a ``label`` — anchor by matching the
          label against the string literals on the changed lines. The Cart screen's
          ``textClipped`` is this case: ``id=`` empty, ``label=Price $185.00``,
          ``frame=(252.5, 256.75, 66.5, 20.5)``.

        An issue with no element at all stays unanchorable; ADR-0002 forbids inventing
        a line for it.
        """
        return bool(self.identifier) or self.anchors_by_label

    @property
    def anchors_by_label(self) -> bool:
        """Whether this issue must be anchored by matching its label to the source."""
        return (
            not self.identifier
            and self.has_element
            and bool(self.label)
            and self.audit_type in LABEL_ANCHORABLE_AUDIT_TYPES
        )

    @property
    def oracle_key(self) -> str:
        """The key ``structured_oracle`` looks this issue up by.

        Lives here rather than at each call site because the oracle set and the
        finding's key must be built by the same code or the verifier rejects every
        label-anchored finding for a mismatch it invented itself.
        """
        if self.identifier:
            return f"{self.audit_type}:{self.identifier}"
        return f"{self.audit_type}:label={self.label}"


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
        failures=tuple(
            f"{f.get('testName', '?')}: {f.get('failureText', '')}"
            for f in data.get("testFailures", [])
        ),
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


def parse_audited_screens(data: dict[str, Any]) -> frozenset[str]:
    """Screens whose audit ran to completion, from the driver's SIGHTLINE-AUDITED marker."""
    return frozenset(
        m["screen"]
        for run in data.get("testRuns", [])
        for activity in _walk_activities(run.get("activities", []))
        if (m := _AUDITED_RECORD.match(activity.get("title", "")))
    )


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


# --- acceptance records (ADR-0004) -----------------------------------------------------


@dataclass(frozen=True)
class AcceptanceRecord:
    """One record the acceptance driver wrote, with the activity's own timestamp."""

    index: int
    criterion_id: str
    phase: str
    fields: dict[str, Any]
    time: float


@dataclass(frozen=True)
class CriterionRun:
    """Everything one acceptance test method left in the bundle."""

    test_id: str
    records: tuple[AcceptanceRecord, ...]
    launch_time: float | None = None
    """``startTime`` of XCTest's ``Launch <bundle id>`` activity. Log attribution's start."""
    teardown_time: float | None = None
    crashed: bool = False
    crash_detail: str = ""
    crash_attachments: tuple[str, ...] = ()
    """Attachment names of the crash logs (``Crash Log <date>.ips``), to find in the export."""

    def phase(self, name: str) -> tuple[AcceptanceRecord, ...]:
        return tuple(r for r in self.records if r.phase == name)

    @property
    def done_time(self) -> float | None:
        done = self.phase("done")
        return done[-1].time if done else None


def decode_acceptance_title(title: str) -> dict[str, Any] | None:
    """The JSON body of a ``SIGHTLINE-ACC|`` activity title, or None for any other title.

    A malformed body is None too: a record we cannot read is a record we do not have,
    and derivation turns that into ``no_record`` rather than guessing.
    """
    m = _ACCEPTANCE_RECORD.match(title)
    if not m:
        return None
    body = m["body"]
    try:
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def parse_acceptance_activities(data: dict[str, Any]) -> CriterionRun:
    """Recover one criterion's run from `get test-results activities` JSON."""
    test_id = data.get("testIdentifier", "")
    records: list[AcceptanceRecord] = []
    launch_time: float | None = None
    teardown_time: float | None = None
    crashed, crash_detail, crash_logs = False, "", []
    for run in data.get("testRuns", []):
        for activity in _walk_activities(run.get("activities", [])):
            title = activity.get("title", "")
            start = float(activity.get("startTime", 0.0))
            if (body := decode_acceptance_title(title)) is not None:
                try:
                    records.append(
                        AcceptanceRecord(
                            index=int(body["i"]),
                            criterion_id=str(body["id"]),
                            phase=str(body["phase"]),
                            fields=body,
                            time=start,
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    continue
            elif _LAUNCH_ACTIVITY.match(title) and launch_time is None:
                launch_time = start
            elif title == "Tear Down" and teardown_time is None:
                teardown_time = start
            elif _CRASH_ACTIVITY.match(title):
                crashed, crash_detail = True, title
            for attachment in activity.get("attachments", []):
                name = attachment.get("name", "")
                if _CRASH_LOG_ATTACHMENT.match(name) and name not in crash_logs:
                    crashed = True
                    crash_logs.append(name)
    return CriterionRun(
        test_id=test_id,
        records=tuple(records),
        launch_time=launch_time,
        teardown_time=teardown_time,
        crashed=crashed,
        crash_detail=crash_detail,
        crash_attachments=tuple(crash_logs),
    )


@dataclass(frozen=True)
class AppLogLine:
    """One unified-log line from the app's captured output (verification V1, V3)."""

    time: float
    pid: int
    process: str
    message: str

    @property
    def event(self) -> str | None:
        m = _AC_EVENT.search(self.message)
        return m["event"] if m else None


def parse_app_output(text: str) -> tuple[AppLogLine, ...]:
    """Every timestamped log line in a `StandardOutputAndStandardError-<bundle>.txt`.

    One file holds every launch in the test run (V3), so lines carry their own PID and
    time. Lines without both — ``print()`` output, continuation lines — cannot be
    attributed to a launch and are not evidence.
    """
    out: list[AppLogLine] = []
    for line in text.splitlines():
        m = _APP_LOG_LINE.match(line)
        if not m:
            continue
        try:
            when = datetime.strptime(m["ts"], "%Y-%m-%d %H:%M:%S.%f%z").timestamp()
        except ValueError:
            continue
        out.append(
            AppLogLine(
                time=when, pid=int(m["pid"]), process=m["process"].strip(), message=m["message"]
            )
        )
    return tuple(out)


def parse_acceptance_shot(suggested_name: str) -> int | None:
    m = _ACCEPTANCE_SHOT.match(suggested_name)
    return int(m["index"]) if m else None


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
        return self.audit_results()[0]

    def audit_results(self) -> tuple[tuple[AuditIssue, ...], frozenset[str]]:
        """Every audit issue, plus the screens whose audit actually completed.

        Read together: an empty issue list means "clean" only for a screen in the
        second set. For any other screen it means the audit never ran.
        """
        issues: list[AuditIssue] = []
        audited: set[str] = set()
        for root in self.tests():
            for case in root.test_cases():
                data = self._get("activities", "--test-id", case.identifier or "")
                issues.extend(parse_audit_issues(data))
                audited |= parse_audited_screens(data)
        return tuple(issues), frozenset(audited)

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

    def acceptance_runs(self) -> tuple[CriterionRun, ...]:
        """One CriterionRun per test case in the bundle."""
        runs: list[CriterionRun] = []
        for root in self.tests():
            for case in root.test_cases():
                data = self._get("activities", "--test-id", case.identifier or "")
                runs.append(parse_acceptance_activities(data))
        return tuple(runs)

    def export_diagnostics(self, dest: Path) -> tuple[Path, ...]:
        """Export diagnostics and return the app stdout/stderr captures in them.

        ``StandardOutputAndStandardError.txt`` (no suffix) is the test runner's own
        output and is not returned; the app's is ``...-<bundle id>.txt``.
        """
        dest = Path(dest)
        if dest.exists():
            shutil.rmtree(dest)
        cmd = [
            "xcrun", "xcresulttool", "export", "diagnostics",
            "--path", str(self.bundle), "--output-path", str(dest),
        ]  # fmt: skip
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise XcresultError(f"export diagnostics failed: {proc.stderr.strip()}")
        return tuple(sorted(dest.rglob(APP_OUTPUT_GLOB)))
