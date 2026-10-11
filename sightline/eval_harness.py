"""The eval corpus runner.

Three PRs against the vendored fixture — two with known defects, one clean — scored for
precision and recall. Three is enough to keep us honest; the corpus grows from here.

The clean case is the important one. It touches a UI surface, so the runtime tier fires
and pays a full build and simulator run, and must still produce nothing. A no-op diff
would not test that.

Each case is a committed patch plus its expected findings, so scoring is repeatable and
does not depend on a live PR. `withmartian/code-review-benchmark`'s online mode — what
developers actually implemented — remains the north star; this is the offline half.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from sightline.adapters.forge.base import ForgeError
from sightline.adapters.forge.local_git import LocalGitAdapter
from sightline.config import RepoConfig
from sightline.core.diff.models import ChangeType
from sightline.core.diff.parser import parse_diff
from sightline.core.evidence.models import ArtifactKind
from sightline.core.evidence.store import FilesystemEvidenceStore
from sightline.core.findings.from_audit import build_findings
from sightline.core.impact.analyzer import analyze
from sightline.core.verify.gate import StructuredOracleVerifier, run_gate
from sightline.runners.simulator.device import Appearance, ContentSize, Simulator
from sightline.runners.xcode.build import Destination, XcodeBuild
from sightline.runners.xcode.injection import InjectionUnavailable, prepare_workspace
from sightline.runners.xcode.xcresult import XcresultTool, postable_issues


@dataclass(frozen=True)
class Expectation:
    audit_type: str
    identifier: str
    file: str

    @property
    def key(self) -> str:
        return f"{self.audit_type}:{self.identifier}"


@dataclass(frozen=True)
class Case:
    id: str
    title: str
    kind: str
    directory: Path
    patch: Path
    expected: tuple[Expectation, ...]
    notes: str = ""
    base: str | None = None
    """Commit of an external target repo the patch applies to (`--target-repo`).
    Unset for cases against the vendored fixture."""
    config: Path | None = None
    """Sightline config for the target, when it is not committed in the target repo."""

    @classmethod
    def load(cls, directory: Path) -> Case:
        data = yaml.safe_load((Path(directory) / "case.yml").read_text())
        return cls(
            id=data["id"],
            title=data["title"],
            kind=data.get("kind", "true_positive"),
            directory=Path(directory),
            patch=Path(directory) / data.get("patch", "change.patch"),
            expected=tuple(Expectation(**e) for e in data.get("expected") or []),
            notes=data.get("notes", ""),
            base=data.get("base"),
            config=Path(directory) / data["config"] if data.get("config") else None,
        )


@dataclass
class CaseResult:
    case: Case
    produced: list[str] = field(default_factory=list)
    true_positives: list[str] = field(default_factory=list)
    false_positives: list[str] = field(default_factory=list)
    false_negatives: list[str] = field(default_factory=list)
    audit_issue_count: int = 0
    suppressed: dict[str, int] = field(default_factory=dict)
    error: str | None = None

    @property
    def passed(self) -> bool:
        return not self.error and not self.false_positives and not self.false_negatives


@dataclass
class Score:
    results: list[CaseResult]

    @property
    def tp(self) -> int:
        return sum(len(r.true_positives) for r in self.results)

    @property
    def fp(self) -> int:
        return sum(len(r.false_positives) for r in self.results)

    @property
    def fn(self) -> int:
        return sum(len(r.false_negatives) for r in self.results)

    @property
    def precision(self) -> float:
        """Undefined with nothing posted. Reported as 1.0: a reviewer that says nothing
        is never wrong, which is exactly why recall has to be read alongside it."""
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else 1.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else 1.0

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)


def discover_cases(corpus: Path) -> list[Case]:
    return [Case.load(d) for d in sorted(Path(corpus).iterdir()) if (d / "case.yml").exists()]


def _apply_patch(root: Path, patch: Path) -> None:
    """Apply with `patch`, not `git apply`.

    `git apply` resolves paths against the *enclosing* repository rather than the
    working directory, and when the scratch copy lives inside one it exits 0 having
    done nothing at all. A silently unapplied patch turns every case into a
    false negative that looks like a harness bug, so the result is verified below
    rather than trusted.
    """
    proc = subprocess.run(
        ["patch", "-p1", "--batch", "--forward", "-d", str(root), "-i", str(patch.resolve())],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"could not apply {patch.name}: {(proc.stderr or proc.stdout).strip()}")
    _assert_patch_landed(root, patch)


def _assert_patch_landed(root: Path, patch: Path) -> None:
    """Confirm at least one added line is actually present in the tree.

    Cheap, and it converts an entire class of silent corpus failure into a loud one.
    """
    added = [
        line[1:].strip()
        for line in patch.read_text().splitlines()
        if line.startswith("+") and not line.startswith("+++") and len(line.strip()) > 12
    ]
    if not added:
        return
    needle = added[len(added) // 2]
    for candidate in Path(root).rglob("*.swift"):
        if needle in candidate.read_text():
            return
    raise RuntimeError(
        f"{patch.name} reported success but its content is not in the tree (looked for {needle!r})"
    )


def run_case(
    case: Case,
    *,
    fixture: Path,
    udid: str,
    workdir: Path,
    content_size: str = "large",
    target_repo: Path | None = None,
) -> CaseResult:
    """Apply the patch to a fresh copy of the fixture and run the runtime tier.

    With ``target_repo``, the tree is that repo exported at the case's ``base`` commit
    instead — a real app rather than the vendored fixture, still reproducible offline.
    """
    result = CaseResult(case=case)
    root = Path(workdir) / case.id
    if root.exists():
        shutil.rmtree(root)

    try:
        if target_repo:
            if not case.base:
                raise RuntimeError("--target-repo needs `base:` in case.yml")
            LocalGitAdapter(target_repo, case.base, case.base).materialize(root)
        else:
            shutil.copytree(fixture, root)
        _apply_patch(root, case.patch)
        diff = parse_diff(case.patch.read_text())
        sources = {
            f.path: (root / f.path).read_text()
            for f in diff.files
            if f.path.endswith(".swift") and f.change_type is not ChangeType.DELETED
        }
        impact = analyze(diff, sources=sources)
        config = RepoConfig.load(root, override=case.config)
        surfaces = config.surfaces_for(impact.ui_surfaces)
        if not surfaces:
            raise RuntimeError(
                f"no configured surface matches {sorted(impact.ui_surfaces) or 'nothing'}"
            )

        workspace = prepare_workspace(
            root,
            scratch_dir=Path(workdir) / f"{case.id}-scratch",
            project_relpath=config.project or "",
            surfaces=surfaces,
            app_target=config.app_target,
            ui_test_target=config.ui_test_target,
            dismiss_alerts=tuple(config.dismiss_alerts),
        )
        Simulator(udid).prepare(content_size=ContentSize(content_size), appearance=Appearance.LIGHT)
        bundle = Path(workdir) / f"{case.id}.xcresult"
        if bundle.exists():
            shutil.rmtree(bundle)
        build = XcodeBuild(workspace.project, workspace.scheme, workspace.root / "dd").test(
            Destination(udid=udid),
            result_bundle=bundle,
            only_testing=workspace.test_identifiers,
        )
        if not bundle.exists():
            raise RuntimeError(f"build failed: {build.failure_lines[:2]}")

        tool = XcresultTool(bundle)
        issues, audited = tool.audit_results()
        if missing := sorted({s.name for s in surfaces} - audited):
            # Otherwise a thrown audit scores as a perfectly quiet clean case.
            raise RuntimeError(
                f"audit did not complete on {missing}: {list(tool.summary().failures)}"
            )
        postable = postable_issues(issues)
        result.audit_issue_count = len(issues)

        store = FilesystemEvidenceStore(Path(workdir) / "evidence", run_id=case.id)
        evidence = [
            store.put(
                (bundle / "Info.plist").read_bytes(),
                kind=ArtifactKind.XCRESULT,
                produced_by="xcodebuild.test",
                context={"case": case.id},
            )
        ]

        proposed = []
        for changed in diff.files:
            if changed.path in sources:
                found, _ = build_findings(
                    postable, changed=changed, source=sources[changed.path], evidence=evidence
                )
                proposed.extend(found)

        oracle = frozenset(f"{i.audit_type}:{i.identifier}" for i in postable)
        gated = run_gate(proposed, StructuredOracleVerifier(oracle), skill_id="accessibility-audit")
        result.suppressed = gated.counts
        result.produced = [f.proposed.oracle_key or "" for f in gated.verified]
    except (RuntimeError, InjectionUnavailable, OSError, ForgeError) as exc:
        result.error = str(exc)
        return result

    expected_keys = {e.key for e in case.expected}
    produced_keys = set(result.produced)
    result.true_positives = sorted(expected_keys & produced_keys)
    result.false_positives = sorted(produced_keys - expected_keys)
    result.false_negatives = sorted(expected_keys - produced_keys)
    return result


def run_corpus(
    corpus: Path,
    *,
    fixture: Path,
    udid: str,
    workdir: Path,
    only: str | None = None,
    target_repo: Path | None = None,
) -> Score:
    cases = [c for c in discover_cases(corpus) if not only or only in c.id]
    return Score(
        [
            run_case(c, fixture=fixture, udid=udid, workdir=workdir, target_repo=target_repo)
            for c in cases
        ]
    )


# --- the acceptance corpus (ADR-0004) ------------------------------------------------------
#
# Separate from the review corpus so the review score keeps its meaning. A case is a
# criteria file, an optional patch to the fixture, and the verdict expected for every
# criterion. Scored as a verdict confusion matrix: a false pass or a false fail is the
# disqualifying error; a not_verifiable with the wrong reason fails the case without
# being either.

ACCEPTANCE_VERDICTS = ("pass", "fail", "not_verifiable")


@dataclass(frozen=True)
class AcceptanceExpectation:
    verdict: str
    reason: str | None = None
    actual: dict[str, object] | None = None
    """Observed values the report must carry, e.g. ``{label: "Subtotal $377.00"}``."""


@dataclass(frozen=True)
class AcceptanceCase:
    id: str
    title: str
    directory: Path
    criteria: Path
    expected: dict[str, AcceptanceExpectation]
    patch: Path | None = None
    config: Path | None = None
    notes: str = ""

    @classmethod
    def load(cls, directory: Path) -> AcceptanceCase:
        directory = Path(directory)
        data = yaml.safe_load((directory / "case.yml").read_text())
        expected = {
            cid: AcceptanceExpectation(
                verdict=e["verdict"], reason=e.get("reason"), actual=e.get("actual")
            )
            for cid, e in (data.get("expected") or {}).items()
        }
        for cid, e in expected.items():
            if e.verdict not in ACCEPTANCE_VERDICTS:
                raise ValueError(f"{directory.name}: {cid} expects unknown verdict {e.verdict!r}")
        return cls(
            id=data["id"],
            title=data["title"],
            directory=directory,
            criteria=(directory / data["criteria"]).resolve(),
            expected=expected,
            patch=directory / data["patch"] if data.get("patch") else None,
            config=directory / data["config"] if data.get("config") else None,
            notes=data.get("notes", ""),
        )


@dataclass
class AcceptanceCaseResult:
    case: AcceptanceCase
    observed: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    """criterion id → (verdict, reason)."""
    false_passes: list[str] = field(default_factory=list)
    false_fails: list[str] = field(default_factory=list)
    mismatches: list[str] = field(default_factory=list)
    """Wrong reason, wrong recorded actual, or a criterion the case does not score."""
    report_path: Path | None = None
    error: str | None = None

    @property
    def passed(self) -> bool:
        return not (self.error or self.false_passes or self.false_fails or self.mismatches)


@dataclass
class AcceptanceScore:
    results: list[AcceptanceCaseResult]

    @property
    def matrix(self) -> dict[tuple[str, str], int]:
        """(expected, observed) → count, over every scored criterion."""
        out = {(e, o): 0 for e in ACCEPTANCE_VERDICTS for o in ACCEPTANCE_VERDICTS}
        for r in self.results:
            for cid, (verdict, _) in r.observed.items():
                if cid in r.case.expected:
                    out[(r.case.expected[cid].verdict, verdict)] += 1
        return out

    @property
    def false_passes(self) -> int:
        return sum(len(r.false_passes) for r in self.results)

    @property
    def false_fails(self) -> int:
        return sum(len(r.false_fails) for r in self.results)

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)


def discover_acceptance_cases(corpus: Path) -> list[AcceptanceCase]:
    return [
        AcceptanceCase.load(d)
        for d in sorted(Path(corpus).iterdir())
        if d.is_dir() and (d / "case.yml").exists()
    ]


def _actual_recorded(want: dict[str, object], observations: list) -> bool:
    return any(
        o.actual is not None and all(o.actual.get(k) == v for k, v in want.items())
        for o in observations
    )


def score_acceptance(case: AcceptanceCase, report) -> AcceptanceCaseResult:
    """Compare a report against the case's expectations. Pure; no Xcode."""
    result = AcceptanceCaseResult(case=case)
    by_id = {c.id: c for c in report.criteria}
    for cid in sorted(set(by_id) - set(case.expected)):
        result.mismatches.append(f"{cid}: in the report but not scored by the case")
    for cid, want in case.expected.items():
        got = by_id.get(cid)
        if got is None:
            result.mismatches.append(f"{cid}: expected {want.verdict}, missing from the report")
            continue
        verdict, reason = got.verdict.value, got.reason.value if got.reason else None
        result.observed[cid] = (verdict, reason)
        if verdict == "pass" and want.verdict != "pass":
            result.false_passes.append(cid)
        elif verdict == "fail" and want.verdict != "fail":
            result.false_fails.append(cid)
        elif verdict != want.verdict:
            result.mismatches.append(f"{cid}: expected {want.verdict}, got {verdict} ({reason})")
        elif want.reason and reason != want.reason:
            result.mismatches.append(f"{cid}: expected reason {want.reason}, got {reason}")
        if (
            want.actual
            and verdict == want.verdict
            and not _actual_recorded(want.actual, got.observations)
        ):
            result.mismatches.append(f"{cid}: the report does not record actual {want.actual}")
    return result


def run_acceptance_case(
    case: AcceptanceCase, *, fixture: Path, udid: str, workdir: Path
) -> AcceptanceCaseResult:
    from sightline.acceptance import AcceptOptions, CriteriaInvalid, run_accept

    root = Path(workdir) / case.id
    if root.exists():
        shutil.rmtree(root)
    try:
        shutil.copytree(fixture, root)
        if case.patch:
            _apply_patch(root, case.patch)
        accepted = run_accept(
            AcceptOptions(
                criteria=case.criteria,
                checkout=root,
                udid=udid,
                config_path=case.config,
                out_dir=Path(workdir) / "reports",
                scratch_dir=Path(workdir) / f"{case.id}-scratch",
            )
        )
    except CriteriaInvalid as exc:
        return AcceptanceCaseResult(
            case=case, error="criteria failed lint: " + "; ".join(map(str, exc.issues))
        )
    except (RuntimeError, OSError) as exc:
        return AcceptanceCaseResult(case=case, error=str(exc))
    result = score_acceptance(case, accepted.report)
    result.report_path = accepted.report_path
    if accepted.report.status != "completed":
        result.error = f"run {accepted.report.status}: {'; '.join(accepted.report.notes)}"
    return result


def run_acceptance_corpus(
    corpus: Path, *, fixture: Path, udid: str, workdir: Path, only: str | None = None
) -> AcceptanceScore:
    cases = [c for c in discover_acceptance_cases(corpus) if not only or only in c.id]
    return AcceptanceScore(
        [run_acceptance_case(c, fixture=fixture, udid=udid, workdir=workdir) for c in cases]
    )
