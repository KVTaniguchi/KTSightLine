"""Preparing a workspace to run the accessibility audit.

ADR-0003 §5, in order:

1. **Discover** a UI test target — named in config, or the only one in the project.
2. **Generate** one if there is none.
3. **Degrade** with an actionable message naming the config key that would fix it.

Every path operates on a **scratch clone**. The checkout we were handed is never
modified, so a crash mid-run cannot leave the user's branch dirty and nothing we write
can be accidentally committed. That guarantee is also what lets `project.py` rewrite the
pbxproj as an XML plist without caring that Xcode's formatting is lost.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sightline.runners.xcode.driver_template import (
    DEFAULT_AUDIT_TYPES,
    Surface,
    render_driver,
)
from sightline.runners.xcode.project import (
    PbxProject,
    ProjectError,
    Target,
    write_scheme,
)

DRIVER_FILENAME = "SightlineAudit.swift"
DRIVER_CLASS = "SightlineAudit"
GENERATED_TARGET_NAME = "SightlineUITests"
GENERATED_SCHEME_NAME = "SightlineAudit"

IGNORED_WHEN_COPYING = shutil.ignore_patterns(
    ".git", "DerivedData", "build", ".build", "*.xcresult", "xcuserdata", ".venv"
)


class InjectionUnavailable(RuntimeError):
    """We could not prepare a runnable workspace.

    ADR-0003 §7 requires this to degrade, not explode: the runtime tier reports
    unavailable, the static findings still post, and the merge is never blocked. The
    message must name the config key that would fix it — a bot that says "failed" and
    nothing else is a bot people turn off.
    """


@dataclass(frozen=True)
class PreparedWorkspace:
    root: Path
    project: Path
    scheme: str
    app_target: str
    ui_test_target: str
    driver_path: Path
    surfaces: tuple[Surface, ...]
    strategy: Literal["discovered", "generated"]

    def test_identifier(self, surface: Surface) -> str:
        """The `-only-testing:` identifier for one surface."""
        return f"{self.ui_test_target}/{DRIVER_CLASS}/{surface.method_name}"

    @property
    def test_identifiers(self) -> tuple[str, ...]:
        return tuple(self.test_identifier(s) for s in self.surfaces)


def _clone(source: Path, scratch: Path) -> Path:
    source, scratch = Path(source), Path(scratch)
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, scratch, ignore=IGNORED_WHEN_COPYING, symlinks=True)
    return scratch


def _resolve_ui_test_target(
    project: PbxProject, configured: str | None, app: Target
) -> tuple[Target, str]:
    if configured:
        target = project.target_named(configured)
        if target is None:
            raise InjectionUnavailable(
                f"config names ui_test_target: {configured!r}, but the project has no "
                f"target by that name. Targets: {[t.name for t in project.targets()]}"
            )
        if not target.is_ui_test:
            raise InjectionUnavailable(
                f"{configured!r} is a {target.product_type}, not a UI test bundle"
            )
        return target, "discovered"

    candidates = project.ui_test_targets()
    if len(candidates) == 1:
        return candidates[0], "discovered"
    if len(candidates) > 1:
        raise InjectionUnavailable(
            f"the project has {len(candidates)} UI test targets "
            f"({[t.name for t in candidates]}); set `ui_test_target:` in "
            ".sightline/config.yml to choose one"
        )
    return project.create_ui_test_target(
        GENERATED_TARGET_NAME,
        app,
        bundle_id=f"com.sightline.generated.{GENERATED_TARGET_NAME.lower()}",
    ), "generated"


def _resolve_app_target(project: PbxProject, configured: str | None) -> Target:
    if configured:
        target = project.target_named(configured)
        if target is None or not target.is_app:
            raise InjectionUnavailable(
                f"config names app_target: {configured!r}, which is not an application "
                f"target. Applications: {[t.name for t in project.app_targets()]}"
            )
        return target
    apps = project.app_targets()
    if not apps:
        raise InjectionUnavailable(
            "no application target found. If this project is generated (XcodeGen, "
            "Tuist), generate it before review, or set `app_target:` in "
            ".sightline/config.yml"
        )
    if len(apps) > 1:
        raise InjectionUnavailable(
            f"the project has {len(apps)} application targets "
            f"({[t.name for t in apps]}); set `app_target:` in .sightline/config.yml"
        )
    return apps[0]


def prepare_workspace(
    source_root: Path,
    *,
    scratch_dir: Path,
    project_relpath: str,
    surfaces: list[Surface],
    app_target: str | None = None,
    ui_test_target: str | None = None,
    audit_types: tuple[str, ...] = DEFAULT_AUDIT_TYPES,
) -> PreparedWorkspace:
    """Copy the repo, ensure a UI test target, and inject the driver into it."""
    if not surfaces:
        raise InjectionUnavailable("no surfaces to audit; impact analysis reached no screen")

    root = _clone(source_root, scratch_dir)
    project_path = root / project_relpath
    if not project_path.exists():
        raise InjectionUnavailable(
            f"{project_relpath} not found in the checkout; set `project:` in .sightline/config.yml"
        )

    try:
        project = PbxProject(project_path)
    except ProjectError as exc:
        raise InjectionUnavailable(str(exc)) from exc

    app = _resolve_app_target(project, app_target)
    ui_test, strategy = _resolve_ui_test_target(project, ui_test_target, app)

    group_dir = project_path.parent / (project.group_path(ui_test) or ui_test.name)
    group_dir.mkdir(parents=True, exist_ok=True)
    driver_path = group_dir / DRIVER_FILENAME
    driver_path.write_text(
        render_driver(surfaces, audit_types=audit_types, class_name=DRIVER_CLASS)
    )

    project.add_source_file(ui_test, DRIVER_FILENAME)
    project.save()

    write_scheme(project_path, GENERATED_SCHEME_NAME, app, ui_test)

    return PreparedWorkspace(
        root=root,
        project=project_path,
        scheme=GENERATED_SCHEME_NAME,
        app_target=app.name,
        ui_test_target=ui_test.name,
        driver_path=driver_path,
        surfaces=tuple(surfaces),
        strategy=strategy,
    )
