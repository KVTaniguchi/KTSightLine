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

    # Artifact kinds first, then the shared capture context once. Listing the device
    # per artifact doubled the line for no information, and length costs adoption.
    kinds = []
    for ref in p.evidence:
        kind = str(ref.kind)
        label = (
            f"[{kind}]({evidence_base_url.rstrip('/')}/{ref.sha256})"
            if evidence_base_url
            else kind
        )
        if label not in kinds:
            kinds.append(label)
    context_bits = _context_bits(p.evidence[0].context) if p.evidence else []
    lines += ["", " · ".join(["Evidence: " + ", ".join(kinds), *context_bits])]

    if p.suggestion:
        lines += ["", "```suggestion", p.suggestion.rstrip("\n"), "```"]

    return "\n".join(lines)


def _context_bits(context: dict[str, str]) -> list[str]:
    """The context is what makes a finding reproducible after the artifact link expires.

    Kept on the comment even though the links eventually 404, because the claim must
    stand on its own once they do.
    """
    bits = []
    if device := context.get("device"):
        bits.append(device)
    if os_version := context.get("os"):
        bits.append(f"iOS {os_version}")
    for key in ("appearance", "content_size", "locale"):
        if value := context.get(key):
            bits.append(value)
    return bits


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
