"""Project-file editing and UI-test-target injection (ADR-0003 §5).

The three paths that matter: discover an existing target, generate one when there is
none, and degrade with a message naming the config key that would fix it.

Everything here operates on copies. The assertion that the *source* checkout is never
modified is the most important test in the file — a harness that dirties someone's
working tree mid-review is worse than one that does nothing.
"""

import shutil
from pathlib import Path

import pytest

from sightline.runners.xcode.driver_template import (
    DEFAULT_AUDIT_TYPES,
    Surface,
    render_driver,
)
from sightline.runners.xcode.injection import (
    DRIVER_FILENAME,
    GENERATED_TARGET_NAME,
    InjectionUnavailable,
    prepare_workspace,
)
from sightline.runners.xcode.project import PbxProject, ProjectError, write_scheme

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "eval/fixtures/CheckoutDemo"
PROJECT = "CheckoutDemo.xcodeproj"
SURFACES = [Surface("Cart", wait_for="cart.continue")]


@pytest.fixture
def app_copy(tmp_path) -> Path:
    dest = tmp_path / "src"
    shutil.copytree(FIXTURE, dest)
    return dest


def strip_ui_test_target(project_path: Path) -> None:
    """Turn the fixture into a project that has no UI test target, like most repos."""
    p = PbxProject(project_path)
    ui = p.ui_test_targets()[0]
    root = p.data["rootObject"]
    p.objects[root]["targets"] = [t for t in p.objects[root]["targets"] if t != ui.object_id]
    p.objects[root].get("attributes", {}).get("TargetAttributes", {}).pop(ui.object_id, None)
    del p.objects[ui.object_id]
    p.save()


# --- pbxproj reading and editing -----------------------------------------------------


def test_reads_targets_and_product_types():
    p = PbxProject(FIXTURE / PROJECT)
    assert [t.name for t in p.ui_test_targets()] == ["CheckoutDemoUITests"]
    assert [t.name for t in p.app_targets()] == ["CheckoutDemo"]


def test_missing_project_is_a_clear_error(tmp_path):
    with pytest.raises(ProjectError, match="does not exist"):
        PbxProject(tmp_path / "Nope.xcodeproj")


def test_add_source_file_is_idempotent(app_copy):
    p = PbxProject(app_copy / PROJECT)
    target = p.ui_test_targets()[0]
    first = p.add_source_file(target, "Thing.swift")
    second = p.add_source_file(target, "Thing.swift")
    assert first == second
    refs = [
        oid
        for oid, obj in p.objects.items()
        if obj.get("isa") == "PBXFileReference" and obj.get("path") == "Thing.swift"
    ]
    assert len(refs) == 1


def test_saved_project_is_still_readable(app_copy):
    """We write XML plists; the round trip has to survive."""
    p = PbxProject(app_copy / PROJECT)
    p.add_source_file(p.ui_test_targets()[0], "Thing.swift")
    p.save()
    assert [t.name for t in PbxProject(app_copy / PROJECT).ui_test_targets()] == [
        "CheckoutDemoUITests"
    ]


def test_create_ui_test_target_is_idempotent(app_copy):
    strip_ui_test_target(app_copy / PROJECT)
    p = PbxProject(app_copy / PROJECT)
    app = p.app_targets()[0]
    a = p.create_ui_test_target("Gen", app, bundle_id="com.x.gen")
    b = p.create_ui_test_target("Gen", app, bundle_id="com.x.gen")
    assert a.object_id == b.object_id
    assert len([t for t in p.targets() if t.name == "Gen"]) == 1


def test_generated_target_depends_on_the_app(app_copy):
    strip_ui_test_target(app_copy / PROJECT)
    p = PbxProject(app_copy / PROJECT)
    app = p.app_targets()[0]
    generated = p.create_ui_test_target("Gen", app, bundle_id="com.x.gen")
    obj = p.objects[generated.object_id]
    dependency = p.objects[obj["dependencies"][0]]
    assert dependency["target"] == app.object_id


def test_scheme_has_no_empty_test_plans_element(tmp_path):
    """An empty <TestPlans> silently overrides <Testables>; it cost an afternoon once."""
    project = tmp_path / "P.xcodeproj"
    project.mkdir()
    p = PbxProject(FIXTURE / PROJECT)
    path = write_scheme(project, "S", p.app_targets()[0], p.ui_test_targets()[0])
    text = path.read_text()
    assert "<TestPlans>" not in text
    assert "<Testables>" in text
    assert p.ui_test_targets()[0].object_id in text


# --- driver generation ----------------------------------------------------------------


def test_driver_uses_the_verified_audit_type_spellings():
    driver = render_driver(SURFACES)
    assert ".textClipped" in driver and ".clippedText" not in driver
    assert ".trait,\n" in driver or ".trait," in driver
    assert ".sufficientElementDescription" in driver
    assert "traits" not in driver


def test_driver_emits_one_method_per_surface():
    driver = render_driver([Surface("Cart"), Surface("Checkout", taps=("cart.continue",))])
    assert "func testSurface_Cart()" in driver
    assert "func testSurface_Checkout()" in driver
    assert 'tap(app, "cart.continue")' in driver


def test_driver_scrolls_before_tapping():
    """Controls on-screen at the default size are not at AX5."""
    assert "swipeUp()" in render_driver(SURFACES)


def test_audit_types_are_configurable():
    driver = render_driver(SURFACES, audit_types=("contrast",))
    assert ".contrast" in driver and ".hitRegion" not in driver.split("auditTypes")[1][:200]


# --- injection: discovery -------------------------------------------------------------


def test_discovers_the_only_ui_test_target(app_copy, tmp_path):
    w = prepare_workspace(
        app_copy,
        scratch_dir=tmp_path / "scratch",
        project_relpath=PROJECT,
        surfaces=SURFACES,
    )
    assert w.strategy == "discovered"
    assert w.ui_test_target == "CheckoutDemoUITests"
    assert w.driver_path.exists()
    assert w.test_identifiers == ("CheckoutDemoUITests/SightlineAudit/testSurface_Cart",)


def test_the_source_checkout_is_never_modified(app_copy, tmp_path):
    """The guarantee everything else in this module rests on."""
    before = {
        p.relative_to(app_copy): p.stat().st_mtime_ns for p in app_copy.rglob("*") if p.is_file()
    }
    prepare_workspace(
        app_copy, scratch_dir=tmp_path / "scratch", project_relpath=PROJECT, surfaces=SURFACES
    )
    after = {
        p.relative_to(app_copy): p.stat().st_mtime_ns for p in app_copy.rglob("*") if p.is_file()
    }
    assert before == after
    assert not (app_copy / "CheckoutDemoUITests" / DRIVER_FILENAME).exists()


def test_configured_target_is_honoured(app_copy, tmp_path):
    w = prepare_workspace(
        app_copy,
        scratch_dir=tmp_path / "scratch",
        project_relpath=PROJECT,
        surfaces=SURFACES,
        ui_test_target="CheckoutDemoUITests",
    )
    assert w.strategy == "discovered"


# --- injection: generation ------------------------------------------------------------


def test_generates_a_target_when_the_project_has_none(app_copy, tmp_path):
    strip_ui_test_target(app_copy / PROJECT)
    w = prepare_workspace(
        app_copy, scratch_dir=tmp_path / "scratch", project_relpath=PROJECT, surfaces=SURFACES
    )
    assert w.strategy == "generated"
    assert w.ui_test_target == GENERATED_TARGET_NAME
    assert (w.root / GENERATED_TARGET_NAME / DRIVER_FILENAME).exists()
    assert PbxProject(w.project).target_named(GENERATED_TARGET_NAME) is not None


# --- injection: degradation, with actionable messages ---------------------------------


def test_unknown_configured_target_names_the_real_targets(app_copy, tmp_path):
    with pytest.raises(InjectionUnavailable, match="no target by that name"):
        prepare_workspace(
            app_copy,
            scratch_dir=tmp_path / "scratch",
            project_relpath=PROJECT,
            surfaces=SURFACES,
            ui_test_target="NopeUITests",
        )


def test_configured_target_of_the_wrong_kind_is_rejected(app_copy, tmp_path):
    with pytest.raises(InjectionUnavailable, match="not a UI test bundle"):
        prepare_workspace(
            app_copy,
            scratch_dir=tmp_path / "scratch",
            project_relpath=PROJECT,
            surfaces=SURFACES,
            ui_test_target="CheckoutDemo",
        )


def test_ambiguous_ui_test_targets_name_the_config_key(app_copy, tmp_path):
    p = PbxProject(app_copy / PROJECT)
    p.create_ui_test_target("SecondUITests", p.app_targets()[0], bundle_id="com.x.second")
    p.save()
    with pytest.raises(InjectionUnavailable, match="ui_test_target"):
        prepare_workspace(
            app_copy, scratch_dir=tmp_path / "scratch", project_relpath=PROJECT, surfaces=SURFACES
        )


def test_missing_project_names_the_config_key(app_copy, tmp_path):
    with pytest.raises(InjectionUnavailable, match="`project:`"):
        prepare_workspace(
            app_copy,
            scratch_dir=tmp_path / "scratch",
            project_relpath="Nope.xcodeproj",
            surfaces=SURFACES,
        )


def test_no_surfaces_is_refused_early(app_copy, tmp_path):
    with pytest.raises(InjectionUnavailable, match="no surfaces"):
        prepare_workspace(
            app_copy, scratch_dir=tmp_path / "scratch", project_relpath=PROJECT, surfaces=[]
        )


def test_scratch_dir_is_reused_cleanly(app_copy, tmp_path):
    scratch = tmp_path / "scratch"
    prepare_workspace(app_copy, scratch_dir=scratch, project_relpath=PROJECT, surfaces=SURFACES)
    (scratch / "STALE.txt").write_text("left over")
    prepare_workspace(app_copy, scratch_dir=scratch, project_relpath=PROJECT, surfaces=SURFACES)
    assert not (scratch / "STALE.txt").exists()


def test_default_audit_types_match_the_verified_enum():
    assert set(DEFAULT_AUDIT_TYPES) == {
        "contrast", "elementDetection", "hitRegion", "sufficientElementDescription",
        "dynamicType", "textClipped", "trait",
    }  # fmt: skip


# --- the real proof: does an injected workspace actually build and run? ---------------
#
# Slow (~60s each) and needs Xcode plus a booted simulator, so it is opt-in:
#     SIGHTLINE_INTEGRATION=1 SIGHTLINE_SIM_UDID=<udid> uv run pytest tests/test_injection.py
# Everything above is fast and runs anywhere. This is what proves the generated project
# is not merely well-formed but buildable.

import os
import subprocess

_INTEGRATION = os.environ.get("SIGHTLINE_INTEGRATION") == "1"
_UDID = os.environ.get("SIGHTLINE_SIM_UDID", "")

integration = pytest.mark.skipif(
    not (_INTEGRATION and _UDID and shutil.which("xcodebuild")),
    reason="set SIGHTLINE_INTEGRATION=1 and SIGHTLINE_SIM_UDID to run",
)


def _run_injected(workspace) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "xcodebuild",
            "test",
            "-project",
            str(workspace.project),
            "-scheme",
            workspace.scheme,
            "-destination",
            f"id={_UDID}",
            "-derivedDataPath",
            str(workspace.root / "dd"),
            f"-only-testing:{workspace.test_identifiers[0]}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )


@integration
def test_injected_driver_builds_and_audits_in_a_discovered_target(app_copy, tmp_path):
    w = prepare_workspace(
        app_copy, scratch_dir=tmp_path / "scratch", project_relpath=PROJECT, surfaces=SURFACES
    )
    proc = _run_injected(w)
    assert "** TEST SUCCEEDED **" in proc.stdout, proc.stdout[-3000:]
    assert "SIGHTLINE|Cart|" in proc.stdout


@integration
def test_generated_target_builds_and_audits(app_copy, tmp_path):
    strip_ui_test_target(app_copy / PROJECT)
    w = prepare_workspace(
        app_copy, scratch_dir=tmp_path / "scratch", project_relpath=PROJECT, surfaces=SURFACES
    )
    assert w.strategy == "generated"
    proc = _run_injected(w)
    assert "** TEST SUCCEEDED **" in proc.stdout, proc.stdout[-3000:]
    assert "SIGHTLINE|Cart|" in proc.stdout
