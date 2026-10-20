"""`sightline` — the command line entry point."""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import click

from sightline.acceptance import AcceptOptions, CriteriaInvalid, run_accept
from sightline.adapters.forge.base import ForgeAdapter
from sightline.adapters.forge.github import GitHubAdapter
from sightline.adapters.forge.local_git import LocalGitAdapter
from sightline.config import PermissionAnswer, RepoConfig
from sightline.core.acceptance.lint import lint_file
from sightline.core.findings.render import render_preview
from sightline.core.impact.analyzer import analyze
from sightline.core.telemetry.trajectory import SkillTrace, Trajectory
from sightline.eval_harness import ACCEPTANCE_VERDICTS, run_acceptance_corpus, run_corpus
from sightline.gate import GateInputs
from sightline.gate import decide as decide_gate
from sightline.review import ReviewOptions
from sightline.review import review as run_review
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
@click.option("--pr", "target", default=None, help="owner/repo#123 or a PR URL.")
@click.option(
    "--local",
    "local_repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Review two refs of a local git repo instead of a PR. Never posts.",
)
@click.option("--base", default=None, help="With --local: the base ref.")
@click.option("--head", default=None, help="With --local: the head ref.")
@click.option(
    "--path",
    "checkout",
    type=click.Path(exists=True, path_type=Path),
    default=Path("."),
    help="Local checkout of the PR head. Needed for the runtime tier. "
    "Ignored with --local, which exports the head ref itself.",
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Use this config instead of <checkout>/.sightline/config.yml.",
)
@click.option("--skills-dir", type=click.Path(path_type=Path), multiple=True)
@click.option("--runtime/--no-runtime", default=False, help="Enable the macOS runtime tier.")
@click.option("--udid", default=None, help="Simulator UDID (overrides config).")
@click.option("--content-size", default="large", show_default=True)
@click.option("--appearance", type=click.Choice(["light", "dark"]), default="light")
@click.option("--allow-experimental", is_flag=True)
@click.option("--budget", type=float, default=0.50, show_default=True)
@click.option("--out", type=click.Path(path_type=Path), default=Path(".sightline/run"))
@click.option("--post/--no-post", default=False, help="Post to the PR. Off by default.")
@click.option(
    "--summary-file",
    type=click.Path(path_type=Path),
    default=None,
    help="Write a markdown report, including each comment body verbatim. "
    "Point at $GITHUB_STEP_SUMMARY in CI.",
)
def review(
    target: str | None,
    local_repo: Path | None,
    base: str | None,
    head: str | None,
    checkout: Path,
    config_path: Path | None,
    skills_dir: tuple[Path, ...],
    runtime: bool,
    udid: str | None,
    content_size: str,
    appearance: str,
    allow_experimental: bool,
    budget: float,
    out: Path,
    post: bool,
    summary_file: Path | None,
) -> None:
    """Review a pull request, end to end.

    Posting is opt-in: without --post this is a dry run that writes a trajectory and
    prints what it would say. A reviewer that comments the first time someone tries it
    is a reviewer people uninstall.
    """
    forge: ForgeAdapter
    if local_repo:
        if target or post:
            raise click.UsageError("--local reviews two local refs; it takes no --pr or --post")
        if not (base and head):
            raise click.UsageError("--local needs --base and --head")
        local = LocalGitAdapter(local_repo, base, head)
        repo, number, forge = local.name, 0, local
        checkout = local.materialize(Path(out) / "checkouts" / local.head_sha[:12])
    elif target:
        repo, number = _parse_pr(target)
        forge = GitHubAdapter()
    else:
        raise click.UsageError("pass --pr, or --local with --base and --head")

    options = ReviewOptions(
        checkout=Path(checkout),
        config_path=config_path,
        runtime=runtime,
        allow_experimental=allow_experimental,
        post=post,
        budget_usd=budget,
        content_size=content_size,
        appearance=appearance,
        out_dir=Path(out),
        skills_dirs=list(skills_dir),
        udid=udid,
    )
    result = run_review(forge, repo, number, options)
    t = result.trajectory

    where = f"{repo} {base}...{head}" if local_repo else f"{repo}#{number}"
    click.echo(f"{where}  {t.head_sha[:8]}")
    click.echo(f"triggers: {', '.join(t.triggers) or 'none'}")
    for d in result.decisions:
        click.echo(f"  {'✔' if d.fired else '·'} {d.skill_id}: {d.outcome.value} — {d.reason}")
    for note in result.notes:
        click.echo(f"  {note}")

    for finding in result.verified:
        p = finding.proposed
        click.echo(f"\n  {p.anchor.file}:{p.anchor.line}  [{p.severity}]")
        click.echo(f"    {p.claim}")
    if summary := t.suppression_summary():
        detail = ", ".join(f"{n} {reason}" for reason, n in summary.items())
        click.echo(f"\nsuppressed: {detail}")

    if summary_file:
        Path(summary_file).parent.mkdir(parents=True, exist_ok=True)
        with open(summary_file, "a", encoding="utf-8") as handle:
            handle.write(
                render_preview(
                    result.verified,
                    suppressed=t.suppression_summary(),
                    notes=result.notes,
                    posted=post,
                )
            )

    click.echo(f"\ntrajectory: {result.trajectory_path}")
    if post:
        for url in result.posted_urls:
            click.echo(f"posted: {url}")
        if not result.verified:
            click.echo("nothing to post")
    else:
        # The exact bodies, so a dry run shows what would land, not just a count.
        click.echo("")
        click.echo(
            render_preview(
                result.verified,
                suppressed=t.suppression_summary(),
                notes=result.notes,
                posted=False,
            )
        )
        hint = "" if local_repo else " (use --post)"
        click.echo(f"dry run — {len(result.verified)} finding(s) would post{hint}")


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
@click.option(
    "--deny-permissions",
    is_flag=True,
    help="Answer every system permission prompt with its deny button (needs `deny_alerts:`).",
)
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
    deny_permissions: bool,
) -> None:
    """Run the runtime accessibility audit against a local checkout.

    The full runtime tier in one command: copy the repo to a scratch clone, ensure a UI
    test target, inject the driver, put the simulator in a known state, build, run,
    parse the result bundle, and apply the gate.

    Nothing is posted and the checkout is never modified.
    """
    run_id = uuid.uuid4().hex[:12]
    surfaces = [_parse_surface(s) for s in surface_specs]
    answer: PermissionAnswer = "deny" if deny_permissions else "allow"
    # Config is read for the alert titles alone: with a permission prompt covering the
    # app, `performAccessibilityAudit` throws "Invalid target app" and audits nothing.
    alert_titles = RepoConfig.load(Path(path)).alert_titles(answer=answer)
    if deny_permissions and not alert_titles:
        click.echo(
            "--deny-permissions needs `deny_alerts:` in .sightline/config.yml; "
            "without a deny title the prompt would be left up, not denied.",
            err=True,
        )
        raise SystemExit(2)

    try:
        workspace = prepare_workspace(
            Path(path),
            scratch_dir=Path(scratch) / run_id,
            project_relpath=project_relpath,
            surfaces=surfaces,
            app_target=app_target,
            ui_test_target=ui_test_target,
            dismiss_alerts=alert_titles,
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


@main.command()
@click.option(
    "--criteria",
    "criteria_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Acceptance criteria file (ADR-0004).",
)
@click.option(
    "--path",
    "checkout",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
    help="Checkout or worktree to build. Never modified.",
)
@click.option("--udid", default=None, help="Simulator UDID (overrides config).")
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Use this config instead of <path>/.sightline/config.yml.",
)
@click.option("--content-size", default="large", show_default=True)
@click.option("--appearance", type=click.Choice(["light", "dark"]), default="light")
@click.option("--scratch", type=click.Path(path_type=Path), default=Path(".sightline/scratch"))
@click.option("--out", type=click.Path(path_type=Path), default=Path(".sightline/run/accept"))
@click.option(
    "--summary-file",
    type=click.Path(path_type=Path),
    default=None,
    help="Append a markdown verdict table. Point at $GITHUB_STEP_SUMMARY in CI.",
)
@click.option(
    "--strict",
    is_flag=True,
    help="Exit 1 on any fail, 2 if nothing could run. Never non-zero for not_verifiable alone.",
)
def accept(
    criteria_path: Path,
    checkout: Path,
    udid: str | None,
    config_path: Path | None,
    content_size: str,
    appearance: str,
    scratch: Path,
    out: Path,
    summary_file: Path | None,
    strict: bool,
) -> None:
    """Check acceptance criteria against a running build. One verdict per criterion.

    Builds the checkout in a scratch clone, drives the app on one simulator, and
    reports pass, fail, or not_verifiable for each criterion, each backed by a record
    in the .xcresult. Needs no PR and no forge, and makes no model call.

    Exits 0 by default. A criteria file that fails lint never runs, and exits 2.
    """
    try:
        result = run_accept(
            AcceptOptions(
                criteria=criteria_path,
                checkout=checkout,
                udid=udid,
                config_path=config_path,
                content_size=content_size,
                appearance=appearance,
                out_dir=out,
                scratch_dir=scratch,
                summary_file=summary_file,
            )
        )
    except CriteriaInvalid as exc:
        for issue in exc.issues:
            click.echo(str(issue), err=True)
        click.echo("criteria failed lint; nothing ran", err=True)
        raise SystemExit(2) from exc

    for issue in result.lint:
        click.echo(str(issue), err=True)
    report = result.report
    click.echo(f"{report.spec_id} · {report.status.replace('_', ' ')}")
    for c in report.criteria:
        reason = f" ({c.reason.value})" if c.reason else ""
        click.echo(f"  {c.verdict.value:<15} {c.id}{reason}  [{c.evidence_type.value}]")
        if c.verdict.value == "fail":
            for o in c.observations:
                if o.met is False:
                    click.echo(f"      {o.target}: expected {o.expected}, got {o.actual}")
        elif c.detail:
            click.echo(f"      {c.detail}")
    counts = " · ".join(f"{n} {v}" for v, n in report.summary.by_verdict.items())
    click.echo(f"\n{counts}")
    click.echo(f"report: {result.report_path}")
    click.echo(f"trajectory: {result.trajectory_path}")
    raise SystemExit(report.exit_code(strict=strict))


@main.command()
@click.option(
    "--criteria",
    "criteria_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option(
    "--path",
    "checkout",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
    help="Repo whose .sightline/config.yml names the surfaces and fixtures.",
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
)
def lint(criteria_path: Path, checkout: Path, config_path: Path | None) -> None:
    """Lint an acceptance criteria file. Runs anywhere; needs no Xcode.

    Exits 1 on any error. Every message names the fix.
    """
    config = RepoConfig.load(checkout, override=config_path)
    _, issues = lint_file(criteria_path, config)
    for issue in issues:
        click.echo(str(issue))
    errors = sum(i.level == "error" for i in issues)
    warnings = len(issues) - errors
    click.echo(f"{criteria_path}: {errors} error(s), {warnings} warning(s)")
    if errors:
        raise SystemExit(1)


@main.command("eval")
@click.option("--corpus", type=click.Path(exists=True, path_type=Path), default=Path("eval/corpus"))
@click.option(
    "--fixture",
    type=click.Path(exists=True, path_type=Path),
    default=Path("eval/fixtures/CheckoutDemo"),
)
@click.option("--udid", required=True, help="Simulator UDID.")
@click.option("--workdir", type=click.Path(path_type=Path), default=Path(".sightline/run/eval"))
@click.option("--only", default=None, help="Substring filter on case id.")
@click.option("--content-size", default="large", show_default=True)
@click.option(
    "--target-repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Run cases against this git repo at each case's `base:` commit, "
    "instead of the vendored fixture.",
)
@click.option(
    "--suite",
    type=click.Choice(["review", "acceptance"]),
    default="review",
    show_default=True,
    help="review: findings scored for precision/recall. acceptance: verdicts scored "
    "as a confusion matrix.",
)
@click.option(
    "--acceptance-corpus",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("eval/acceptance"),
)
def eval_command(
    corpus: Path,
    fixture: Path,
    udid: str,
    workdir: Path,
    only: str | None,
    content_size: str,
    target_repo: Path | None,
    suite: str,
    acceptance_corpus: Path,
) -> None:
    """Score an eval corpus.

    The review suite scores precision and recall and exits non-zero on any false
    positive or false negative. The acceptance suite scores verdicts and exits non-zero
    on any false pass, false fail, wrong reason, or case that did not run. Both are
    usable as gates on our own PRs.
    """
    if suite == "acceptance":
        _eval_acceptance(acceptance_corpus, fixture, udid, workdir / "acceptance", only)
        return
    score = run_corpus(
        Path(corpus),
        fixture=Path(fixture),
        udid=udid,
        workdir=Path(workdir),
        only=only,
        target_repo=target_repo,
    )
    for result in score.results:
        mark = "PASS" if result.passed else "FAIL"
        click.echo(f"\n[{mark}] {result.case.id} ({result.case.kind})")
        click.echo(f"       {result.case.title}")
        if result.error:
            click.echo(f"       error: {result.error}")
            continue
        click.echo(
            f"       {result.audit_issue_count} audit issues → {len(result.produced)} finding(s)"
        )
        for key in result.true_positives:
            click.echo(f"       ✔ true positive  {key}")
        for key in result.false_positives:
            click.echo(f"       ✘ FALSE POSITIVE {key}")
        for key in result.false_negatives:
            click.echo(f"       ✘ MISSED         {key}")
        if result.suppressed:
            detail = ", ".join(f"{n} {r}" for r, n in result.suppressed.items())
            click.echo(f"       suppressed: {detail}")

    click.echo(
        f"\nprecision {score.precision:.2f}  recall {score.recall:.2f}  "
        f"(tp {score.tp} · fp {score.fp} · fn {score.fn})"
    )
    if not score.passed:
        raise SystemExit(1)


def _eval_acceptance(
    corpus: Path, fixture: Path, udid: str, workdir: Path, only: str | None
) -> None:
    score = run_acceptance_corpus(corpus, fixture=fixture, udid=udid, workdir=workdir, only=only)
    for result in score.results:
        click.echo(f"\n[{'PASS' if result.passed else 'FAIL'}] {result.case.id}")
        click.echo(f"       {result.case.title}")
        if result.error:
            click.echo(f"       error: {result.error}")
        for cid, (verdict, reason) in result.observed.items():
            want = result.case.expected[cid]
            got = verdict + (f" ({reason})" if reason else "")
            mark = "✔" if (verdict, reason or want.reason) == (want.verdict, want.reason) else "✘"
            click.echo(f"       {mark} {cid:<20} {got}")
        for cid in result.false_passes:
            click.echo(f"       ✘ FALSE PASS {cid}")
        for cid in result.false_fails:
            click.echo(f"       ✘ FALSE FAIL {cid}")
        for line in result.mismatches:
            click.echo(f"       ✘ {line}")

    matrix = score.matrix
    click.echo("\nexpected \\ observed   pass   fail   not_verifiable")
    for expected in ACCEPTANCE_VERDICTS:
        row = "".join(f"{matrix[(expected, o)]:>7}" for o in ACCEPTANCE_VERDICTS)
        click.echo(f"{expected:<20}{row}")
    click.echo(f"\nfalse passes {score.false_passes} · false fails {score.false_fails}")
    if not score.passed:
        raise SystemExit(1)


@main.command()
@click.option("--pr", "target", required=True, help="owner/repo#123 or a PR URL.")
@click.option("--path", "checkout", type=click.Path(exists=True, path_type=Path), default=Path("."))
@click.option("--skills-dir", type=click.Path(path_type=Path), multiple=True)
@click.option("--budget", type=float, default=0.50, show_default=True)
def gate(target: str, checkout: Path, skills_dir: tuple[Path, ...], budget: float) -> None:
    """Decide whether the runtime tier should run. Prints the reason; never fails a PR.

    CI calls this on Linux so the expensive macOS job inherits a cheap job's analysis
    instead of redoing it. Writes `runtime` and `reason` to $GITHUB_OUTPUT when set.

    Exits 0 in every ordinary case, including "do not run". It also exits 0 when the
    decision itself errors, admitting the run: ADR-0003 requires the gate to fail open,
    and a gate that can block a merge by crashing is worse than no gate.
    """
    runtime, reason, stage = True, "gate errored; failing open", "ok"
    try:
        repo, number = _parse_pr(target)
        forge = GitHubAdapter()
        pr = forge.get_pull_request(repo, number)
        diff = forge.get_diff(repo, number)
        sources: dict[str, str] = {}
        for changed in diff.files:
            if changed.path.endswith(".swift") and (
                content := forge.get_file(repo, changed.path, pr.head_sha)
            ):
                sources[changed.path] = content
        impact = analyze(diff, sources=sources)
        skills, _ = load_all([Path(d) for d in skills_dir])
        trace = decide_gate(
            GateInputs(
                pr=pr,
                impact=impact,
                changed_paths=diff.paths,
                skills=skills,
                budget_remaining_usd=budget,
            )
        )
        runtime, reason, stage = trace.runtime_enabled, trace.reason, trace.stage
    except Exception as exc:  # noqa: BLE001 — fail open is the whole point
        reason = f"gate errored ({exc}); failing open"

    click.echo(f"runtime={'true' if runtime else 'false'} stage={stage}")
    click.echo(f"reason: {reason}")
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"runtime={'true' if runtime else 'false'}\n")
            handle.write(f"stage={stage}\n")
            handle.write(f"reason={reason}\n")


if __name__ == "__main__":
    # Must stay last: every command below this line is invisible to
    # `python -m sightline.cli` if main() runs before its decorator does.
    main()
