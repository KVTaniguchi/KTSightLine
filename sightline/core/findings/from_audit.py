r"""Turning an `.xcresult` audit issue into an anchored, proposable finding.

Deterministic: no model is involved in choosing the line. Two anchoring strategies, in
order, and both are searches of the head-side source rather than inferences:

1. **By identifier.** The identifier the audit reports is searched for literally.
2. **By label literal.** For an element that exists but carries no
   ``accessibilityIdentifier``, the runtime label is matched against the Swift string
   literals on the changed lines, with ``\(...)`` interpolations treated as wildcards.
   ``.accessibilityLabel("Price \(item.formattedPrice)")`` anchors the observed label
   ``Price $185.00``; the literal segments must match verbatim.

A line is accepted only if it falls inside the diff — because we review the change, not
the app. An issue on an element the PR did not touch is dropped as out of scope, not
posted. Strategy 2 additionally requires the match to be *unique*: two changed lines
whose literals both fit the label is an ambiguity, and ADR-0002 resolves ambiguity by
dropping the finding rather than picking one.
"""

from __future__ import annotations

import re
from collections.abc import Set as AbstractSet
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


_STRING_LITERAL = re.compile(r'"((?:[^"\\]|\\.)*)"')
_INTERPOLATION = re.compile(r"\\\((?:[^()]|\([^()]*\))*\)")
_MIN_LITERAL_CHARS = 4
r"""Below this, a literal carries too little text to identify one element.

``Text("\(total)")`` yields no literal text at all and would otherwise match every
observed label; ``"$"`` would match most of them. The floor is what keeps strategy 2
from anchoring on coincidence.
"""


def _unescape(literal: str) -> str:
    """Swift escapes that change the characters a label is compared against."""
    return (
        literal.replace('\\"', '"').replace("\\n", "\n").replace("\\t", "\t").replace("\\\\", "\\")
    )


def _label_pattern(literal: str) -> re.Pattern[str] | None:
    """A matcher for the runtime label a Swift string literal produces.

    Interpolations become wildcards because their value is only known at runtime;
    every literal segment must appear verbatim and in order. Returns ``None`` when the
    literal is too thin to identify anything.
    """
    segments = [_unescape(seg) for seg in _INTERPOLATION.split(literal)]
    if len("".join(segments)) < _MIN_LITERAL_CHARS:
        return None
    return re.compile("^" + ".*".join(re.escape(seg) for seg in segments) + "$", re.DOTALL)


def _lines_matching_label(label: str, lines: list[str], in_scope: AbstractSet[int]) -> list[int]:
    """Every changed line carrying a string literal consistent with ``label``."""
    out: list[int] = []
    for number in sorted(in_scope):
        if number > len(lines):
            continue
        for match in _STRING_LITERAL.finditer(lines[number - 1]):
            pattern = _label_pattern(match.group(1))
            if pattern is not None and pattern.match(label):
                out.append(number)
                break
    return out


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
    # Scope is `added_lines`, not `commentable_lines`. Those answer different questions:
    # commentable_lines is where GitHub will *accept* a comment (added plus surrounding
    # context), while added_lines is what this PR actually changed. Anchoring to context
    # would comment on pre-existing defects the author did not introduce — the fastest
    # way to get a reviewer muted, and contrary to "we review the change, not the app".
    in_scope = changed.added_lines
    findings: list[ProposedFinding] = []
    unmapped: list[Unmapped] = []

    for issue in issues:
        if issue.identifier:
            candidates = [
                number
                for number, text in enumerate(lines, start=1)
                if issue.identifier in text and number in in_scope
            ]
            if not candidates:
                # The element exists, but this PR did not touch it. Out of scope.
                unmapped.append(Unmapped(issue, "not_in_diff"))
                continue
        elif issue.anchors_by_label:
            candidates = _lines_matching_label(issue.label, lines, in_scope)
            if len(candidates) > 1:
                # Two changed lines could have produced this label. Picking one would be
                # a guess, and a comment on the wrong line is worse than no comment.
                unmapped.append(Unmapped(issue, "ambiguous_label"))
                continue
            if not candidates:
                # Nothing on a changed line produces this label: either the element is
                # pre-existing, or its label is built somewhere we cannot see.
                unmapped.append(Unmapped(issue, "unanchorable"))
                continue
        else:
            # No element at all — no identifier, no label, no frame. Nothing to anchor to.
            unmapped.append(Unmapped(issue, "unanchorable"))
            continue

        line_number = candidates[0]
        source_line = lines[line_number - 1]
        width, height = _frame_size(issue.frame)
        template = _CLAIM.get(issue.audit_type, "`{identifier}`: {description}")
        # A label-anchored issue has no identifier to name, so the claim names the label
        # it was observed under. `normalize_claim` strips quoted literals, so the
        # fingerprint of "Price $185.00" survives the price changing.
        display = issue.identifier or f'"{issue.label}"'
        claim = template.format(
            identifier=display,
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
                oracle_key=issue.oracle_key,
            )
        )

    return findings, unmapped
