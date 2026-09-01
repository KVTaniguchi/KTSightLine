"""End-to-end review: a PR URL in, posted comments out.

Joins the deterministic layer (diff → impact → dispatch) to the runtime tier
(inject → build → drive → capture → parse) and the gate. Every stage records into the
trajectory, including the ones that decide *not* to do something.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sightline.adapters.forge.base import AnchorRejected, ForgeAdapter, PullRequest
from sightline.adapters.forge.github import validate_anchor
from sightline.config import RepoConfig
from sightline.core.diff.models import ChangeType, Diff
from sightline.core.evidence.models import ArtifactKind
from sightline.core.evidence.redaction import RedactionError, RedactionPolicy, RegionMask
from sightline.core.evidence.store import FilesystemEvidenceStore
from sightline.core.findings.from_audit import build_findings
from sightline.core.findings.models import VerifiedFinding
from sightline.core.findings.render import render_summary
from sightline.core.impact.analyzer import ImpactReport, analyze
from sightline.core.skills.dispatch import Decision, Outcome, RunPolicy, dispatch
from sightline.core.skills.frontmatter import Tier
from sightline.core.telemetry.ledger import Addressal, LedgerEntry, SqliteLedger
from sightline.core.telemetry.trajectory import (
    GateTrace,
    SkillTrace,
    Suppression,
    Trajectory,
)
from sightline.core.verify.gate import StructuredOracleVerifier, run_gate
from sightline.runners.simulator.device import Appearance, ContentSize, Simulator
from sightline.runners.xcode.build import Destination, XcodeBuild
from sightline.runners.xcode.injection import InjectionUnavailable, prepare_workspace
from sightline.runners.xcode.xcresult import XcresultTool, postable_issues
from sightline.skills_builtin import load_all

RUNTIME_SKILL_ID = "accessibility-audit"


@dataclass
class ReviewOptions:
    checkout: Path = Path(".")
    runtime: bool = False
    allow_experimental: bool = False
    post: bool = False
    budget_usd: float = 0.50
    content_size: str = "large"
    appearance: str = "light"
    out_dir: Path = Path(".sightline/run")
    scratch_dir: Path = Path(".sightline/scratch")
    skills_dirs: list[Path] = field(default_factory=list)
    udid: str | None = None


@dataclass
class ReviewResult:
    trajectory: Trajectory
    decisions: list[Decision]
    verified: list[VerifiedFinding]
    posted_urls: list[str]
    notes: list[str]
    trajectory_path: Path | None = None


def _redaction_policy(config: RepoConfig) -> RedactionPolicy:
    masks = tuple(
        RegionMask(
            x=float(m.get("x", 0)),
            y=float(m.get("y", 0)),
            width=float(m.get("width", 0)),
            height=float(m.get("height", 0)),
            label=str(m.get("label", "")),
        )
        for m in config.redaction.masks
    )
    return RedactionPolicy(
        masks=masks,
        scale=config.redaction.scale,
        mask_status_bar=config.redaction.mask_status_bar,
    )


def _run_runtime_tier(
    *,
    options: ReviewOptions,
    config: RepoConfig,
    impact: ImpactReport,
    diff: Diff,
    sources: dict[str, str],
    store: FilesystemEvidenceStore,
    run_id: str,
    trajectory: Trajectory,
) -> tuple[list[VerifiedFinding], list[Suppression], list[str]]:
    """Build, drive, capture, and gate. Returns findings, suppressions, and notes."""
    notes: list[str] = []
    surfaces = config.surfaces_for(impact.ui_surfaces)
    if not surfaces:
        notes.append(
            "runtime tier: no configured surface matches the changed views "
            f"({sorted(impact.ui_surfaces) or 'none'}); add them under `surfaces:` "
            "in .sightline/config.yml"
        )
        return [], [], notes

    if not config.project:
        notes.append("runtime tier: unavailable — set `project:` in .sightline/config.yml")
        return [], [], notes

    udid = options.udid or config.simulator.udid
    if not udid:
        notes.append("runtime tier: unavailable — no simulator UDID configured")
        return [], [], notes

    try:
        workspace = prepare_workspace(
            options.checkout,
            scratch_dir=Path(options.scratch_dir) / run_id,
            project_relpath=config.project,
            surfaces=surfaces,
            app_target=config.app_target,
            ui_test_target=config.ui_test_target,
        )
    except InjectionUnavailable as exc:
        # ADR-0003 §7: degrade with an actionable message; never block the merge.
        notes.append(f"runtime tier: unavailable — {exc}")
        return [], [], notes

    simulator = Simulator(udid)
    device_context = simulator.prepare(
        content_size=ContentSize(options.content_size),
        appearance=Appearance(options.appearance),
    )

    bundle = Path(options.out_dir) / f"{run_id}.xcresult"
    Path(options.out_dir).mkdir(parents=True, exist_ok=True)
    builder = XcodeBuild(workspace.project, workspace.scheme, workspace.root / "dd")
    build = builder.test(
        Destination(udid=udid), result_bundle=bundle, only_testing=workspace.test_identifiers
    )
    if not bundle.exists():
        notes.append("runtime tier: the branch failed to build before any test ran")
        trajectory.notes.extend(build.failure_lines[:5])
        return [], [], notes

    tool = XcresultTool(bundle)
    summary = tool.summary()
    issues = tool.all_audit_issues()
    postable = postable_issues(issues)
    context = {
        **(summary.device.as_context() if summary.device else {}),
        **device_context,
        "commit": trajectory.head_sha[:8],
    }

    attachments = tool.export_attachments(Path(options.out_dir) / f"{run_id}-attachments")
    shot_refs = []
    for attachment in attachments:
        if not attachment.screen:
            continue
        try:
            shot_refs.append(
                store.put_file(
                    Path(options.out_dir) / f"{run_id}-attachments" / attachment.exported_file_name,
                    kind=ArtifactKind.SCREENSHOT,
                    produced_by="simulator.capture.screenshot",
                    context={**context, "screen": attachment.screen},
                )
            )
        except RedactionError as exc:
            notes.append(f"screenshot for {attachment.screen} discarded: {exc}")
    bundle_ref = store.put(
        (bundle / "Info.plist").read_bytes(),
        kind=ArtifactKind.XCRESULT,
        produced_by="xcodebuild.test",
        context=context,
    )
    evidence = [*shot_refs[:1], bundle_ref] if shot_refs else [bundle_ref]

    proposed, unmapped = [], []
    for changed in diff.files:
        if not changed.path.endswith(".swift") or changed.change_type is ChangeType.DELETED:
            continue
        if changed.path not in sources:
            continue
        found, skipped = build_findings(
            postable, changed=changed, source=sources[changed.path], evidence=evidence
        )
        proposed.extend(found)
        unmapped.extend(skipped)

    oracle = frozenset(f"{i.audit_type}:{i.identifier}" for i in postable)
    gated = run_gate(proposed, StructuredOracleVerifier(oracle), skill_id=RUNTIME_SKILL_ID)

    suppressions = list(gated.suppressed)
    suppressions += [
        Suppression(skill_id=RUNTIME_SKILL_ID, claim=u.issue.description, reason=u.reason)
        for u in unmapped
    ]
    # Warnings the audit reported but that never became findings.
    filtered = len(issues) - len(postable)
    if filtered:
        suppressions += [
            Suppression(skill_id=RUNTIME_SKILL_ID, claim="", reason="below_threshold")
            for _ in range(filtered)
        ]

    kept: list[VerifiedFinding] = []
    for finding in gated.verified:
        try:
            validate_anchor(diff, finding)
        except AnchorRejected as exc:
            suppressions.append(
                Suppression(
                    skill_id=RUNTIME_SKILL_ID,
                    fingerprint=finding.fingerprint,
                    claim=finding.proposed.claim,
                    reason="anchor_rejected",
                    verifier=finding.verified_by,
                )
            )
            trajectory.notes.append(str(exc))
            continue
        kept.append(finding)

    notes.append(
        f"runtime tier: {workspace.strategy} target {workspace.ui_test_target}, "
        f"{len(surfaces)} surface(s), {len(issues)} audit issues"
    )
    return kept, suppressions, notes


def review(forge: ForgeAdapter, repo: str, number: int, options: ReviewOptions) -> ReviewResult:
    run_id = uuid.uuid4().hex[:12]
    config = RepoConfig.load(options.checkout)

    pr: PullRequest = forge.get_pull_request(repo, number)
    diff = forge.get_diff(repo, number)

    # Impact analysis needs head-side contents, not just the diff: a change confined to
    # an existing view's `body` matches no declaration in the added lines.
    sources: dict[str, str] = {}
    for changed in diff.files:
        is_live_swift = (
            changed.path.endswith(".swift") and changed.change_type is not ChangeType.DELETED
        )
        if is_live_swift and (content := forge.get_file(repo, changed.path, pr.head_sha)):
            sources[changed.path] = content

    impact = analyze(diff, sources=sources)
    trajectory = Trajectory(
        run_id=run_id,
        repo=repo,
        pr_number=number,
        base_sha=pr.base_sha,
        head_sha=pr.head_sha,
        triggers=sorted(t.value for t in impact.triggers),
        trigger_evidence=[
            {"trigger": e.trigger.value, "path": e.path, "line": e.line, "reason": e.reason}
            for e in impact.evidence
        ],
        budget_usd=options.budget_usd or config.budget_usd,
    )

    runtime = options.runtime
    if pr.is_fork and runtime:
        runtime = False
        trajectory.gate = GateTrace(
            runtime_enabled=False, reason="fork PR — no build cache or credentials", stage="fork"
        )
    else:
        trajectory.gate = GateTrace(
            runtime_enabled=runtime,
            reason="runtime tier enabled" if runtime else "runtime tier not requested",
        )

    skills, errors = load_all([Path(d) for d in options.skills_dirs])
    trajectory.notes.extend(f"skill failed to load: {e}" for e in errors)

    policy = RunPolicy(
        enabled_tiers=frozenset(Tier) if runtime else frozenset({Tier.STATIC}),
        pr_budget_usd=trajectory.budget_usd,
        allow_experimental=options.allow_experimental,
    )
    decisions = dispatch(skills, diff.paths, impact, policy)
    trajectory.skills = [
        SkillTrace(
            skill_id=d.skill_id,
            outcome=d.outcome.value,
            reason=d.reason,
            matched_paths=list(d.matched_paths),
            matched_triggers=list(d.matched_triggers),
            reserved_usd=d.reserved_usd,
        )
        for d in decisions
    ]

    store = FilesystemEvidenceStore(
        Path(options.out_dir) / "evidence", run_id=run_id, policy=_redaction_policy(config)
    )

    verified: list[VerifiedFinding] = []
    notes: list[str] = []
    runtime_fired = any(d.fired and d.skill_id == RUNTIME_SKILL_ID for d in decisions)
    if runtime and runtime_fired:
        verified, suppressions, notes = _run_runtime_tier(
            options=options,
            config=config,
            impact=impact,
            diff=diff,
            sources=sources,
            store=store,
            run_id=run_id,
            trajectory=trajectory,
        )
        trajectory.suppressions.extend(suppressions)
        for trace in trajectory.skills:
            if trace.skill_id == RUNTIME_SKILL_ID:
                trace.proposed = len(verified) + len(suppressions)
                trace.verified = len(verified)
    elif runtime and not runtime_fired:
        blocked = next((d for d in decisions if d.skill_id == RUNTIME_SKILL_ID), None)
        if blocked and blocked.outcome is not Outcome.FIRED:
            notes.append(f"runtime tier: `{blocked.skill_id}` did not run — {blocked.reason}")

    posted_urls: list[str] = []
    if options.post and verified:
        summary = render_summary(
            posted=len(verified),
            suppressed=trajectory.suppression_summary(),
            skipped_notes=notes,
        )
        comments = forge.post_review(pr, verified, summary=summary)
        posted_urls = [c.url for c in comments]
        ledger = SqliteLedger(Path(options.out_dir) / "addressal.sqlite")
        for finding, comment in zip(verified, comments, strict=False):
            ledger.record_posted(
                LedgerEntry(
                    fingerprint=finding.fingerprint,
                    repo=repo,
                    pr_number=number,
                    skill_id=finding.proposed.rule_id,
                    file=finding.proposed.anchor.file,
                    line=finding.proposed.anchor.line,
                    claim=finding.proposed.claim,
                    comment_id=comment.comment_id,
                    posted_at=datetime.now(UTC),
                    head_sha=pr.head_sha,
                    status=Addressal.OPEN,
                )
            )
        for trace in trajectory.skills:
            if trace.skill_id == RUNTIME_SKILL_ID:
                trace.posted = len(verified)

    trajectory.notes.extend(notes)
    path = trajectory.finish().write(Path(options.out_dir) / f"trajectory-{run_id}.json")
    return ReviewResult(
        trajectory=trajectory,
        decisions=decisions,
        verified=verified,
        posted_urls=posted_urls,
        notes=notes,
        trajectory_path=path,
    )
