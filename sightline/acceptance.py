"""`sightline accept`: criteria in, one verdict per criterion out.

ADR-0004. The acceptance tier's orchestrator, the counterpart of ``review._run_runtime_tier``:
lint → inject the interpreter into a scratch clone → prepare the simulator → build and
run → read records, screenshots, crash logs and app output back from the ``.xcresult`` →
derive. No PR, no forge, no model call, so it runs in a worktree before a PR exists.

The report is written on every path. A run that never built still produces a report
saying so, with every criterion ``not_verifiable(build_failed)`` — an agent loop reading
the file must never mistake "didn't run" for "nothing failed".
"""

from __future__ import annotations

import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from sightline.config import RepoConfig
from sightline.core.acceptance.criteria import CriteriaFile, Criterion
from sightline.core.acceptance.derive import (
    CriterionFacts,
    LogWindow,
    attribute_log_lines,
    derive,
)
from sightline.core.acceptance.lint import LintIssue, has_errors, lint_file
from sightline.core.acceptance.report import AcceptanceReport, RunStatus, render_markdown
from sightline.core.acceptance.verdict import CriterionResult, VerdictKind
from sightline.core.evidence.models import ArtifactKind, ArtifactRef
from sightline.core.evidence.redaction import RedactionError
from sightline.core.evidence.store import FilesystemEvidenceStore
from sightline.core.telemetry.trajectory import SkillTrace, Trajectory
from sightline.review import _redaction_policy
from sightline.runners.simulator.device import Appearance, ContentSize, SimctlError, Simulator
from sightline.runners.xcode.acceptance_driver import render_acceptance_driver
from sightline.runners.xcode.build import Destination, XcodeBuild, XcodeBuildError
from sightline.runners.xcode.injection import InjectionUnavailable, prepare_acceptance_workspace
from sightline.runners.xcode.xcresult import (
    AppLogLine,
    CriterionRun,
    XcresultError,
    XcresultTool,
    parse_acceptance_shot,
    parse_app_output,
)

SKILL_ID = "acceptance"


class CriteriaInvalid(Exception):
    """The criteria file failed lint. Nothing ran; there is no report to write."""

    def __init__(self, issues: list[LintIssue]) -> None:
        super().__init__(f"{sum(i.level == 'error' for i in issues)} lint error(s)")
        self.issues = issues


@dataclass
class AcceptOptions:
    criteria: Path
    checkout: Path = Path(".")
    udid: str | None = None
    config_path: Path | None = None
    content_size: str = "large"
    appearance: str = "light"
    out_dir: Path = Path(".sightline/run/accept")
    scratch_dir: Path = Path(".sightline/scratch")
    summary_file: Path | None = None


@dataclass
class AcceptResult:
    report: AcceptanceReport
    report_path: Path
    trajectory_path: Path
    lint: list[LintIssue] = field(default_factory=list)


@dataclass
class _Run:
    """What one build-and-run produced, before derivation."""

    status: RunStatus = "completed"
    detail: str = ""
    runs: dict[str, CriterionRun] = field(default_factory=dict)
    lines: tuple[AppLogLine, ...] = ()
    shots: dict[int, ArtifactRef] = field(default_factory=dict)
    crashes: dict[str, ArtifactRef] = field(default_factory=dict)
    """Crash Log attachment name → stored crash report."""
    bundle_ref: ArtifactRef | None = None
    build_ref: ArtifactRef | None = None
    context: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def _git(checkout: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(checkout), *args], capture_output=True, text=True, check=False
        )
    except OSError:
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _tool_versions() -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        xcode = subprocess.run(
            ["xcodebuild", "-version"], capture_output=True, text=True, check=False
        )
        out["xcode"] = " ".join(xcode.stdout.split())
        out["xcresulttool"] = XcresultTool.tool_version()
    except OSError:
        pass
    return out


def _execute(
    criteria: CriteriaFile,
    config: RepoConfig,
    options: AcceptOptions,
    *,
    run_id: str,
    out: Path,
    store: FilesystemEvidenceStore,
) -> _Run:
    result = _Run()
    if not any(c.executable for c in criteria.criteria):
        return result  # nothing needs a simulator; every verdict is decided statically

    def unavailable(detail: str) -> _Run:
        result.status, result.detail = "runner_unavailable", detail
        return result

    if not config.project:
        return unavailable("set `project:` in .sightline/config.yml")
    udid = options.udid or config.simulator.udid
    if not udid:
        return unavailable("no simulator UDID; pass --udid or set `simulator.udid` in config")

    rendered = render_acceptance_driver(criteria, config)
    try:
        workspace = prepare_acceptance_workspace(
            options.checkout,
            scratch_dir=Path(options.scratch_dir) / run_id,
            project_relpath=config.project,
            rendered=rendered,
            app_target=config.app_target,
            ui_test_target=config.ui_test_target,
        )
    except InjectionUnavailable as exc:
        return unavailable(str(exc))

    try:
        device_context = Simulator(udid).prepare(
            content_size=ContentSize(options.content_size),
            appearance=Appearance(options.appearance),
        )
    except (SimctlError, ValueError) as exc:
        return unavailable(f"simulator: {exc}")

    bundle = out / f"{run_id}.xcresult"
    try:
        build = XcodeBuild(workspace.project, workspace.scheme, workspace.root / "dd").test(
            Destination(udid=udid), result_bundle=bundle, only_testing=workspace.test_identifiers
        )
    except XcodeBuildError as exc:
        return unavailable(str(exc))

    if not bundle.exists():
        result.status = "build_failed"
        result.detail = "; ".join(build.failure_lines[:3]) or "the build failed before any test ran"
        result.build_ref = store.put(
            build.log.encode(),
            kind=ArtifactKind.BUILD_LOG,
            produced_by="xcodebuild.test",
            context={},
        )
        return result

    tool = XcresultTool(bundle)
    summary = tool.summary()
    result.context = {
        **(summary.device.as_context() if summary.device else {}),
        **device_context,
        "language": config.acceptance.language,
        "locale": config.acceptance.locale,
    }
    result.bundle_ref = store.put(
        (bundle / "Info.plist").read_bytes(),
        kind=ArtifactKind.XCRESULT,
        produced_by="xcodebuild.test",
        context=result.context,
    )
    method_to_id = dict(zip(rendered.method_names, rendered.criterion_ids, strict=True))
    for run in tool.acceptance_runs():
        method = run.test_id.rsplit("/", 1)[-1].removesuffix("()")
        if method in method_to_id:
            result.runs[method_to_id[method]] = run

    attachments_dir = out / "attachments"
    for attachment in tool.export_attachments(attachments_dir):
        source = attachments_dir / attachment.exported_file_name
        index = parse_acceptance_shot(attachment.suggested_name)
        try:
            if index is not None and index < len(rendered.criterion_ids):
                result.shots[index] = store.put_file(
                    source,
                    kind=ArtifactKind.SCREENSHOT,
                    produced_by="simulator.capture.screenshot",
                    context={**result.context, "criterion": rendered.criterion_ids[index]},
                )
            elif attachment.suggested_name.startswith("Crash Log "):
                result.crashes[attachment.suggested_name] = store.put_file(
                    source,
                    kind=ArtifactKind.CRASH_REPORT,
                    produced_by="xctest.crash_report",
                    context=result.context,
                )
        except (RedactionError, OSError) as exc:
            result.notes.append(f"attachment {attachment.suggested_name} discarded: {exc}")

    if any(c.expect_log for c in criteria.criteria if c.executable):
        try:
            outputs = tool.export_diagnostics(out / "diagnostics")
            result.lines = tuple(
                line
                for path in outputs
                for line in parse_app_output(path.read_text(errors="replace"))
            )
            if not outputs:
                result.notes.append(
                    "the result bundle carries no app output; log criteria cannot be checked"
                )
        except (XcresultError, OSError) as exc:
            result.notes.append(f"could not read app output: {exc}")
    return result


def _facts(
    criterion: Criterion, index: int | None, run: _Run, store: FilesystemEvidenceStore
) -> CriterionFacts:
    if run.status != "completed":
        evidence = (run.build_ref,) if run.build_ref else ()
        return CriterionFacts(status=run.status, detail=run.detail, evidence=evidence)
    criterion_run = run.runs.get(criterion.id)
    if criterion_run is None:
        return CriterionFacts(run=None)

    end = criterion_run.done_time or criterion_run.teardown_time
    surface = criterion_run.phase("surface")
    log: LogWindow | None = None
    log_ref: ArtifactRef | None = None
    if criterion.expect_log:
        log = attribute_log_lines(
            run.lines,
            launch=criterion_run.launch_time,
            counting_from=surface[-1].time if surface else None,
            end=end,
        )
        events = [line.message for line in log.counted if line.event]
        if events:
            log_ref = store.put(
                "\n".join(events).encode(),
                kind=ArtifactKind.CONSOLE_LOG,
                produced_by="app.log.sightline_ac",
                context={**run.context, "criterion": criterion.id},
            )

    evidence = tuple(
        ref
        for ref in (run.bundle_ref, run.shots.get(index) if index is not None else None, log_ref)
        if ref is not None
    )
    crash = tuple(
        run.crashes[name] for name in criterion_run.crash_attachments if name in run.crashes
    )
    started = criterion_run.launch_time
    return CriterionFacts(
        run=criterion_run,
        log=log,
        evidence=evidence,
        crash_evidence=crash,
        duration_s=round(end - started, 3) if end and started else 0.0,
    )


def run_accept(options: AcceptOptions) -> AcceptResult:
    """Lint, run, derive, write. Raises CriteriaInvalid before anything runs."""
    config = RepoConfig.load(options.checkout, override=options.config_path)
    criteria, issues = lint_file(options.criteria, config)
    if criteria is None or has_errors(issues):
        raise CriteriaInvalid(issues)

    run_id = uuid.uuid4().hex[:12]
    out = Path(options.out_dir) / run_id
    out.mkdir(parents=True, exist_ok=True)
    head_sha = _git(options.checkout, "rev-parse", "HEAD")
    report = AcceptanceReport(
        run_id=run_id,
        spec_id=criteria.spec_id,
        provenance=dict(criteria.provenance),
        head_sha=head_sha,
    )
    if head_sha and _git(options.checkout, "status", "--porcelain", "--", "."):
        report.notes.append("the checkout has uncommitted changes; head_sha does not describe it")
    trajectory = Trajectory(run_id=run_id, repo=str(options.checkout), head_sha=head_sha)
    trajectory.tool_versions = _tool_versions()
    store = FilesystemEvidenceStore(
        out / "evidence", run_id=run_id, policy=_redaction_policy(config)
    )

    started = time.monotonic()
    run = _execute(criteria, config, options, run_id=run_id, out=out, store=store)

    runnable = [c.id for c in criteria.criteria if c.executable]
    results: list[CriterionResult] = []
    for criterion in criteria.criteria:
        index = runnable.index(criterion.id) if criterion.id in runnable else None
        facts = _facts(criterion, index, run, store) if criterion.executable else CriterionFacts()
        results.append(derive(criterion, facts))

    report.status = run.status
    report.context = {
        **run.context,
        "content_size": options.content_size,
        "appearance": options.appearance,
        "language": config.acceptance.language,
        "locale": config.acceptance.locale,
        **{f"tool.{k}": v for k, v in trajectory.tool_versions.items()},
    }
    if run.status != "completed":
        report.notes.append(f"{run.status.replace('_', ' ')}: {run.detail}")
    report.notes.extend(run.notes)
    report.with_results(results).finish()

    judged = sum(r.verdict is not VerdictKind.NOT_VERIFIABLE for r in results)
    trajectory.skills = [
        SkillTrace(
            skill_id=SKILL_ID,
            outcome="fired" if run.status == "completed" else "errored",
            reason=f"{len(results)} criteria from {options.criteria}",
            duration_s=round(time.monotonic() - started, 3),
            proposed=len(results),
            verified=judged,
            artifacts=sorted({ref.sha256 for r in results for ref in r.evidence}),
            error=run.detail or None,
        )
    ]
    trajectory.notes.extend(report.notes)
    trajectory.notes.append(" · ".join(f"{n} {v}" for v, n in report.summary.by_verdict.items()))

    report_path = report.write(out / f"acceptance-{run_id}.json")
    trajectory_path = trajectory.finish().write(out / f"trajectory-{run_id}.json")
    if options.summary_file:
        Path(options.summary_file).parent.mkdir(parents=True, exist_ok=True)
        with open(options.summary_file, "a", encoding="utf-8") as handle:
            handle.write(render_markdown(report))
    return AcceptResult(
        report=report, report_path=report_path, trajectory_path=trajectory_path, lint=issues
    )
