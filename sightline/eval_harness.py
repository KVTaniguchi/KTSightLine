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
) -> CaseResult:
    """Apply the patch to a fresh copy of the fixture and run the runtime tier."""
    result = CaseResult(case=case)
    root = Path(workdir) / case.id
    if root.exists():
        shutil.rmtree(root)
    shutil.copytree(fixture, root)

    try:
        _apply_patch(root, case.patch)
        diff = parse_diff(case.patch.read_text())
        sources = {
            f.path: (root / f.path).read_text()
            for f in diff.files
            if f.path.endswith(".swift") and f.change_type is not ChangeType.DELETED
        }
        impact = analyze(diff, sources=sources)
        config = RepoConfig.load(root)
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
        issues = tool.all_audit_issues()
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
    except (RuntimeError, InjectionUnavailable, OSError) as exc:
        result.error = str(exc)
        return result

    expected_keys = {e.key for e in case.expected}
    produced_keys = set(result.produced)
    result.true_positives = sorted(expected_keys & produced_keys)
    result.false_positives = sorted(produced_keys - expected_keys)
    result.false_negatives = sorted(expected_keys - produced_keys)
    return result


def run_corpus(
    corpus: Path, *, fixture: Path, udid: str, workdir: Path, only: str | None = None
) -> Score:
    cases = [c for c in discover_cases(corpus) if not only or only in c.id]
    return Score([run_case(c, fixture=fixture, udid=udid, workdir=workdir) for c in cases])
