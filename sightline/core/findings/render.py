"""Comment rendering.

The format is measured, not taste. arXiv 2607.21997 (verified 2026-08-31): an inline
code suggestion moves resolution from 64.6% to 75.5% — 10.9 points — and useful comments
averaged 616 characters against 807 for ignored ones.

So: one claim, an evidence line, and a suggestion whenever it is mechanically derivable.
No hedging, no explaining iOS to an iOS engineer.
"""

from __future__ import annotations

from sightline.core.findings.models import VerifiedFinding

TARGET_CHARS = 616
"""Observed mean length of a comment developers acted on. A target, not a hard cap."""

_SEVERITY_MARK = {
    "blocking": "🛑",
    "high": "⚠️",
    "medium": "⚠️",
    "low": "💬",
}


def render_comment(finding: VerifiedFinding, *, evidence_base_url: str | None = None) -> str:
    p = finding.proposed
    mark = _SEVERITY_MARK.get(p.severity, "⚠️")
    lines = [f"{mark} {p.claim} — `{p.anchor.file}:{p.anchor.line}`"]

    if p.detail:
        lines += ["", p.detail]

    evidence = []
    for ref in p.evidence:
        label = _evidence_label(ref.kind, ref.context)
        evidence.append(
            f"[{label}]({evidence_base_url.rstrip('/')}/{ref.sha256})"
            if evidence_base_url
            else label
        )
    lines += ["", "Evidence: " + " · ".join(evidence)]

    if p.suggestion:
        lines += ["", "```suggestion", p.suggestion.rstrip("\n"), "```"]

    return "\n".join(lines)


def _evidence_label(kind: str, context: dict[str, str]) -> str:
    """The context block is what makes a finding reproducible after the link expires."""
    bits = [str(kind)]
    for key in ("device", "os", "appearance", "content_size", "locale"):
        if value := context.get(key):
            bits.append(value)
    return " · ".join(bits)


def render_summary(
    *,
    posted: int,
    suppressed: dict[str, int],
    skipped_notes: list[str],
    trajectory_url: str | None = None,
) -> str:
    """The run summary. Suppressions appear as counts and reasons only (D8)."""
    lines = [f"**Sightline** — {posted} comment{'s' if posted != 1 else ''} posted."]
    if suppressed:
        total = sum(suppressed.values())
        detail = ", ".join(f"{n} {reason}" for reason, n in suppressed.items())
        lines.append("")
        lines.append(
            f"<details><summary>{total} findings suppressed</summary>\n\n{detail}\n</details>"
        )
    for note in skipped_notes:
        lines += ["", note]
    if trajectory_url:
        lines += ["", f"[Trajectory]({trajectory_url})"]
    return "\n".join(lines)
