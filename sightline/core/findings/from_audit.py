"""Turning an `.xcresult` audit issue into an anchored, proposable finding.

Deterministic: no model is involved in choosing the line. The identifier the audit
reports is searched for in the head-side source, and the line is accepted only if it
falls inside the diff — because we review the change, not the app. An issue on an
element the PR did not touch is dropped as out of scope, not posted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sightline.core.diff.models import ChangedFile
from sightline.core.diff.swift import SwiftOutline
from sightline.core.evidence.models import ArtifactRef
from sightline.core.findings.models import Anchor, ProposedFinding
from sightline.runners.xcode.xcresult import AuditIssue

RULE_ID = "accessibility-audit"

_CLAIM = {
    "sufficientElementDescription": (
        "`{identifier}` has no accessibility label — VoiceOver reads {label!r}."
    ),
    "hitRegion": "`{identifier}` tap area is {w}×{h}pt — under the 44×44 minimum.",
    "contrast": "`{identifier}` fails the minimum contrast ratio.",
    "textClipped": "`{identifier}` is clipped at this text size.",
    "dynamicType": "`{identifier}` does not support Dynamic Type.",
    "elementDetection": "`{identifier}` is not exposed as a distinct element.",
    "trait": "`{identifier}` has traits that misdescribe it to assistive technology.",
}

_SEVERITY = {
    "sufficientElementDescription": "high",
    "hitRegion": "high",
    "contrast": "high",
    "textClipped": "medium",
    "dynamicType": "medium",
    "elementDetection": "medium",
    "trait": "medium",
}


@dataclass(frozen=True)
class Unmapped:
    """An audit issue we could not turn into a finding, and why."""

    issue: AuditIssue
    reason: str


def _frame_size(frame: str) -> tuple[str, str]:
    numbers = re.findall(r"-?\d+\.?\d*", frame)
    if len(numbers) >= 4:
        return _trim(numbers[2]), _trim(numbers[3])
    return "?", "?"


def _trim(value: str) -> str:
    return value.rstrip("0").rstrip(".") if "." in value else value


def _indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _suggestion(issue: AuditIssue, source_line: str) -> str | None:
    """Only where the fix is mechanically derivable from the audit type.

    A wrong suggestion costs more trust than a missing one, so anything needing design
    intent — what the label should say, how big the control should be — gets none.
    """
    if issue.audit_type == "sufficientElementDescription":
        indent = _indent_of(source_line)
        return f'{source_line.rstrip()}\n{indent}.accessibilityLabel("<describe this control>")'
    return None


def build_findings(
    issues: tuple[AuditIssue, ...],
    *,
    changed: ChangedFile,
    source: str,
    evidence: list[ArtifactRef],
) -> tuple[list[ProposedFinding], list[Unmapped]]:
    """Anchor each issue to a changed line of ``changed.path``."""
    lines = source.splitlines()
    outline = SwiftOutline(source)
    commentable = changed.commentable_lines
    findings: list[ProposedFinding] = []
    unmapped: list[Unmapped] = []

    for issue in issues:
        if not issue.identifier:
            unmapped.append(Unmapped(issue, "unanchorable"))
            continue

        candidates = [
            number
            for number, text in enumerate(lines, start=1)
            if issue.identifier in text and number in commentable
        ]
        if not candidates:
            # The element exists, but this PR did not touch it. Out of scope.
            unmapped.append(Unmapped(issue, "not_in_diff"))
            continue

        line_number = candidates[0]
        source_line = lines[line_number - 1]
        width, height = _frame_size(issue.frame)
        template = _CLAIM.get(issue.audit_type, "`{identifier}`: {description}")
        claim = template.format(
            identifier=issue.identifier,
            label=issue.label,
            description=issue.description,
            w=width,
            h=height,
        )

        findings.append(
            ProposedFinding(
                rule_id=RULE_ID,
                anchor=Anchor(file=changed.path, line=line_number),
                enclosing_symbol=outline.enclosing_symbol(line_number),
                severity=_SEVERITY.get(issue.audit_type, "medium"),
                claim=claim,
                evidence=evidence,
                suggestion=_suggestion(issue, source_line),
                oracle_key=f"{issue.audit_type}:{issue.identifier}",
            )
        )

    return findings, unmapped
