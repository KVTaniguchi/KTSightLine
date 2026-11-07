"""The acceptance report: a versioned, public JSON contract.

ADR-0004 §7. Other tools ingest this file keyed on criterion ID, so its shape is a
contract: adding a field is a minor change, renaming or removing one bumps
``report_schema``. Written on every run, including the ones that never built.

``not_verifiable`` criteria are listed by ID and reason, not just counted — the
deliberate difference from D8. They are the author's own criteria that nothing checked,
and a count would let "3 not verifiable" read as a clean run.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

from sightline.core.acceptance.verdict import (
    NOTHING_RAN,
    UNREACHABLE,
    CriterionResult,
    NotVerifiableReason,
    VerdictKind,
    rehydrate,
)

REPORT_SCHEMA = 1

RunStatus = Literal["completed", "build_failed", "runner_unavailable"]


class NotVerifiableEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    reason: NotVerifiableReason
    detail: str = ""


class ReportSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    total: int
    by_verdict: dict[str, int]
    by_reason: dict[str, int]
    crashed: list[str]
    unreachable: list[str]
    not_verifiable: list[NotVerifiableEntry]


class AcceptanceReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_schema: Literal[1] = 1
    run_id: str
    spec_id: str
    provenance: dict[str, str] = Field(default_factory=dict)
    head_sha: str = ""
    status: RunStatus = "completed"
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    context: dict[str, str] = Field(default_factory=dict)
    """Device, OS, language, locale, appearance, content size, tool versions."""
    notes: list[str] = Field(default_factory=list)
    criteria: list[CriterionResult] = Field(default_factory=list)
    """Crashed criteria first (ADR-0004 §3 rule 5), then in criteria-file order."""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def summary(self) -> ReportSummary:
        by_verdict = {v.value: 0 for v in VerdictKind}
        by_reason: dict[str, int] = {}
        nv: list[NotVerifiableEntry] = []
        for c in self.criteria:
            by_verdict[c.verdict.value] += 1
            if c.reason is not None:
                by_reason[c.reason.value] = by_reason.get(c.reason.value, 0) + 1
                nv.append(NotVerifiableEntry(id=c.id, reason=c.reason, detail=c.detail))
        return ReportSummary(
            total=len(self.criteria),
            by_verdict=by_verdict,
            by_reason=dict(sorted(by_reason.items())),
            crashed=[c.id for c in self.criteria if c.reason is NotVerifiableReason.APP_CRASHED],
            unreachable=[c.id for c in self.criteria if c.reason in UNREACHABLE],
            not_verifiable=nv,
        )

    def with_results(self, results: list[CriterionResult]) -> AcceptanceReport:
        crashed = [r for r in results if r.reason is NotVerifiableReason.APP_CRASHED]
        rest = [r for r in results if r.reason is not NotVerifiableReason.APP_CRASHED]
        self.criteria = crashed + rest
        return self

    def finish(self) -> AcceptanceReport:
        self.finished_at = datetime.now(UTC)
        return self

    def exit_code(self, *, strict: bool) -> int:
        """ADR-0004 §8. 0 by default. Under ``--strict``: 2 when nothing could run,
        1 on any ``fail``, and never non-zero for ``not_verifiable`` alone."""
        if not strict:
            return 0
        nothing_ran = self.status != "completed" or (
            bool(self.criteria) and all(c.reason in NOTHING_RAN for c in self.criteria)
        )
        if nothing_ran:
            return 2
        if any(c.verdict is VerdictKind.FAIL for c in self.criteria):
            return 1
        return 0

    def write(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2))
        return path

    @classmethod
    def read(cls, path: Path) -> AcceptanceReport:
        data = json.loads(Path(path).read_text())
        data.pop("summary", None)  # derived; recomputed on read
        criteria = [rehydrate(c) for c in data.pop("criteria", [])]
        report = cls.model_validate(data)
        report.criteria = criteria
        return report


_MARK = {
    VerdictKind.PASS: "✅ pass",
    VerdictKind.FAIL: "❌ fail",
    VerdictKind.NOT_VERIFIABLE: "⚪ not verifiable",
}


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _observed(result: CriterionResult) -> str:
    parts = []
    for o in result.observations:
        if o.met is False:
            parts.append(f"`{o.target}` expected {o.expected}, got {o.actual}")
    return "; ".join(parts)


def render_markdown(report: AcceptanceReport) -> str:
    """The `--summary-file` table. Every verdict names its evidence type, so a log-backed
    pass is never read as an observed-UI pass (ADR-0004 §5)."""
    s = report.summary
    counts = " · ".join(f"{n} {v.replace('_', ' ')}" for v, n in s.by_verdict.items())
    lines = [f"### Acceptance: {report.spec_id}", "", f"{counts} ({s.total} criteria)", ""]
    if report.status != "completed":
        lines += [f"**Run did not complete: {report.status.replace('_', ' ')}.**", ""]
    if s.crashed:
        lines += [f"**App crashed during:** {', '.join(f'`{i}`' for i in s.crashed)}", ""]
    if s.unreachable:
        lines += [f"**Unreachable:** {', '.join(f'`{i}`' for i in s.unreachable)}", ""]
    lines += ["| Criterion | Verdict | Evidence | Detail |", "|---|---|---|---|"]
    for c in report.criteria:
        verdict = _MARK[c.verdict] + (f" ({c.reason.value})" if c.reason else "")
        detail = c.detail if c.verdict is not VerdictKind.FAIL else _observed(c)
        lines.append(
            f"| `{c.id}` {_cell(c.then)} | {verdict} | {c.evidence_type.value} | {_cell(detail)} |"
        )
    for note in report.notes:
        lines.append(f"\n_{_cell(note)}_")
    return "\n".join(lines) + "\n"
