"""`sightline` — the command line entry point."""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import click

from sightline.adapters.forge.github import GitHubAdapter
from sightline.core.diff.models import ChangeType
from sightline.core.impact.analyzer import analyze
from sightline.core.skills.dispatch import RunPolicy, dispatch
from sightline.core.skills.frontmatter import Tier
from sightline.core.telemetry.trajectory import GateTrace, SkillTrace, Trajectory
from sightline.runners.simulator.device import Appearance, ContentSize, Simulator
from sightline.runners.xcode.build import Destination, XcodeBuild
from sightline.runners.xcode.driver_template import Surface
from sightline.runners.xcode.injection import InjectionUnavailable, prepare_workspace
from sightline.runners.xcode.xcresult import XcresultTool, postable_issues
from sightline.skills_builtin import load_all


def _parse_pr(target: str) -> tuple[str, int]:
    """Accept `owner/repo#123` or a full PR URL."""
    if "#" in target:
        repo, _, number = target.partition("#")
        return repo, int(number)
    parts = [p for p in target.rstrip("/").split("/") if p]
    if "pull" in parts:
        i = parts.index("pull")
        return f"{parts[i - 2]}/{parts[i - 1]}", int(parts[i + 1])
    raise click.BadParameter(f"could not read a PR from {target!r}")


@click.group()
@click.version_option(package_name="sightline")
def main() -> None:
    """Evidence-grounded PR review for iOS."""


@main.command()
@click.option("--pr", "target", required=True, help="owner/repo#123 or a PR URL.")
@click.option("--skills-dir", type=click.Path(path_type=Path), multiple=True)
@click.option("--runtime/--no-runtime", default=False, help="Enable the macOS runtime tier.")
@click.option("--allow-experimental", is_flag=True)
@click.option("--budget", type=float, default=0.50, show_default=True)
@click.option("--out", type=click.Path(path_type=Path), default=Path(".sightline/run"))
@click.option("--post/--no-post", default=False, help="Post to the PR. Off by default.")
def review(
    target: str,
    skills_dir: tuple[Path, ...],
    runtime: bool,
    allow_experimental: bool,
    budget: float,
    out: Path,
    post: bool,
) -> None:
    """Review a pull request.

    Posting is opt-in: without --post this is a dry run that writes a trajectory and
    prints what it *would* say. That default is deliberate — a reviewer that comments
    the first time someone tries it is a reviewer people uninstall.
    """
    repo, number = _parse_pr(target)
    run_id = uuid.uuid4().hex[:12]
    forge = GitHubAdapter()

    pr = forge.get_pull_request(repo, number)
    diff = forge.get_diff(repo, number)

    # Impact analysis needs head-side contents, not just the diff: a change confined to
    # an existing view's `body` matches no declaration in the added lines, and without
    # the file we cannot tell the enclosing type is a View. Missing that means every
    # runtime skill silently does not fire.
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
        budget_usd=budget,
    )

    # ADR-0003 §7: fork PRs get the static tier and are told so plainly.
    if pr.is_fork and runtime:
        runtime = False
        trajectory.gate = GateTrace(
            runtime_enabled=False,
            reason="fork PR — no build cache or credentials",
            stage="fork",
        )
    else:
        trajectory.gate = GateTrace(
            runtime_enabled=runtime,
            reason="runtime tier enabled" if runtime else "runtime tier not requested",
        )

    tiers = frozenset(Tier) if runtime else frozenset({Tier.STATIC})
    policy = RunPolicy(
        enabled_tiers=tiers, pr_budget_usd=budget, allow_experimental=allow_experimental
    )

    skills, errors = load_all(list(skills_dir))
    trajectory.notes.extend(f"skill failed to load: {e}" for e in errors)

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

    path = trajectory.finish().write(Path(out) / f"trajectory-{run_id}.json")

    click.echo(f"{repo}#{number}  {pr.head_sha[:8]}  ({len(diff.files)} files changed)")
    click.echo(f"triggers: {', '.join(trajectory.triggers) or 'none'}")
    for d in decisions:
        mark = "✔" if d.fired else "·"
        click.echo(f"  {mark} {d.skill_id}: {d.outcome.value} — {d.reason}")
    click.echo(f"trajectory: {path}")
    if not post:
        click.echo("dry run — nothing posted (pass --post to comment)")


@main.command("check-anchors")
@click.option("--pr", "target", required=True)
@click.argument("findings_json", type=click.Path(exists=True, path_type=Path))
def check_anchors(target: str, findings_json: Path) -> None:
    """Validate anchors against a real PR diff without posting anything.

    The read-only half of positioning verification: it proves our line arithmetic agrees
    with what GitHub will accept, which is the part a local test cannot establish.
    """
    repo, number = _parse_pr(target)
    diff = GitHubAdapter().get_diff(repo, number)
    anchors = json.loads(Path(findings_json).read_text())
    failures = 0
    for a in anchors:
        changed = diff.by_path(a["file"])
        if changed is None:
            click.echo(f"✘ {a['file']}: not in this diff")
            failures += 1
            continue
        allowed = changed.commentable_lines
        ok = a["line"] in allowed
        click.echo(
            f"{'✔' if ok else '✘'} {a['file']}:{a['line']} "
            f"({'commentable' if ok else 'NOT in a hunk'})"
        )
        failures += 0 if ok else 1
    click.echo(f"{len(anchors) - failures}/{len(anchors)} anchors valid")
    sys.exit(1 if failures else 0)


@main.command()
@click.option("--pr", "target", required=True)
def diff(target: str) -> None:
    """Print what we think is commentable. Useful when a comment lands oddly."""
    repo, number = _parse_pr(target)
    for f in GitHubAdapter().get_diff(repo, number).files:
        click.echo(f"{f.path} [{f.change_type}]")
        click.echo(f"  added:      {sorted(f.added_lines)}")
        click.echo(f"  context:    {sorted(f.context_lines)}")
        click.echo(f"  commentable(RIGHT): {sorted(f.commentable_lines)}")


if __name__ == "__main__":
    main()


def _parse_surface(spec: str) -> Surface:
    """`Name`, `Name:tap1,tap2`, or `Name:tap1,tap2@waitFor`."""
    name, _, rest = spec.partition(":")
    taps, _, wait = rest.partition("@")
    return Surface(
        name=name,
        taps=tuple(t for t in taps.split(",") if t),
        wait_for=wait or None,
    )


@main.command()
@click.option("--path", type=click.Path(exists=True, path_type=Path), default=Path("."))
@click.option("--project", "project_relpath", required=True, help="e.g. App.xcodeproj")
@click.option("--udid", required=True, help="Simulator UDID.")
@click.option(
    "--surface",
    "surface_specs",
    multiple=True,
    required=True,
    help="Name[:tap1,tap2][@waitForIdentifier]. Repeatable.",
)
@click.option("--app-target", default=None)
@click.option("--ui-test-target", default=None)
@click.option("--content-size", default="large", show_default=True)
@click.option("--appearance", type=click.Choice(["light", "dark"]), default="light")
@click.option("--scratch", type=click.Path(path_type=Path), default=Path(".sightline/scratch"))
@click.option("--out", type=click.Path(path_type=Path), default=Path(".sightline/run"))
def audit(
    path: Path,
    project_relpath: str,
    udid: str,
    surface_specs: tuple[str, ...],
    app_target: str | None,
    ui_test_target: str | None,
    content_size: str,
    appearance: str,
    scratch: Path,
    out: Path,
) -> None:
    """Run the runtime accessibility audit against a local checkout.

    The full runtime tier in one command: copy the repo to a scratch clone, ensure a UI
    test target, inject the driver, put the simulator in a known state, build, run,
    parse the result bundle, and apply the gate.

    Nothing is posted and the checkout is never modified.
    """
    run_id = uuid.uuid4().hex[:12]
    surfaces = [_parse_surface(s) for s in surface_specs]

    try:
        workspace = prepare_workspace(
            Path(path),
            scratch_dir=Path(scratch) / run_id,
            project_relpath=project_relpath,
            surfaces=surfaces,
            app_target=app_target,
            ui_test_target=ui_test_target,
        )
    except InjectionUnavailable as exc:
        # ADR-0003 §7: degrade, never explode. The message names the fix.
        click.echo(f"runtime tier unavailable: {exc}", err=True)
        raise SystemExit(2) from exc

    click.echo(f"workspace: {workspace.root} ({workspace.strategy} {workspace.ui_test_target})")

    simulator = Simulator(udid)
    context = simulator.prepare(
        content_size=ContentSize(content_size), appearance=Appearance(appearance)
    )
    click.echo(f"simulator: {context}")

    bundle = Path(out) / f"{run_id}.xcresult"
    Path(out).mkdir(parents=True, exist_ok=True)
    builder = XcodeBuild(workspace.project, workspace.scheme, workspace.root / "dd")
    result = builder.test(
        Destination(udid=udid),
        result_bundle=bundle,
        only_testing=workspace.test_identifiers,
    )
    if not result.succeeded and not bundle.exists():
        click.echo("build failed before any test ran:", err=True)
        for line in result.failure_lines[:5]:
            click.echo(f"  {line}", err=True)
        raise SystemExit(2)

    tool = XcresultTool(bundle)
    summary = tool.summary()
    issues = tool.all_audit_issues()
    postable = postable_issues(issues)

    click.echo(
        f"{summary.device.model_name if summary.device else '?'} · "
        f"{len(issues)} audit issues · {len(postable)} postable"
    )
    for issue in postable:
        click.echo(f"  {issue.audit_type}:{issue.identifier}  {issue.description}  {issue.frame}")

    suppressed = len(issues) - len(postable)
    trajectory = Trajectory(run_id=run_id, repo=str(path), budget_usd=0.0)
    trajectory.skills = [
        SkillTrace(
            skill_id="accessibility-audit",
            outcome="fired",
            reason=f"{workspace.strategy} target {workspace.ui_test_target}",
            proposed=len(postable),
        )
    ]
    trajectory.notes.append(f"{suppressed} audit issues suppressed as warnings or unanchorable")
    click.echo(f"trajectory: {trajectory.finish().write(Path(out) / f'trajectory-{run_id}.json')}")
