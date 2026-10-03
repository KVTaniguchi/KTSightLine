"""What it took to run against a real app (Nearby Meows) instead of the vendored fixture.

Each test pins one thing the fixture never exercised and the real app broke:
synchronized folders, launch-argument navigation, a permission alert over the screen, a
thrown audit that looked like a clean one, and reviewing local refs with no PR.
"""

import plistlib
import subprocess
from pathlib import Path

import pytest

from sightline.adapters.forge.base import ForgeError
from sightline.adapters.forge.local_git import LocalGitAdapter
from sightline.config import RepoConfig
from sightline.eval_harness import discover_cases
from sightline.runners.xcode.driver_template import Surface, render_driver
from sightline.runners.xcode.injection import DRIVER_FILENAME, prepare_workspace
from sightline.runners.xcode.project import PbxProject
from sightline.runners.xcode.xcresult import parse_audited_screens, parse_summary

REPO = Path(__file__).resolve().parent.parent
NEARBY_MEOWS = REPO / "eval/nearby-meows"

APP, UI, APP_GROUP, UI_GROUP = "A" * 24, "B" * 24, "C" * 24, "D" * 24


def write_synchronized_project(root: Path) -> Path:
    """An Xcode 16+ project whose targets compile whole folders, like Nearby Meows."""
    project = root / "App.xcodeproj"
    project.mkdir(parents=True)
    objects = {
        "R" * 24: {"isa": "PBXProject", "mainGroup": "M" * 24, "targets": [APP, UI]},
        "M" * 24: {"isa": "PBXGroup", "children": [APP_GROUP, UI_GROUP], "sourceTree": "<group>"},
        APP_GROUP: {"isa": "PBXFileSystemSynchronizedRootGroup", "path": "App"},
        UI_GROUP: {"isa": "PBXFileSystemSynchronizedRootGroup", "path": "AppUITests"},
        APP: {
            "isa": "PBXNativeTarget",
            "name": "App",
            "productType": "com.apple.product-type.application",
            "fileSystemSynchronizedGroups": [APP_GROUP],
            "buildPhases": [],
        },
        UI: {
            "isa": "PBXNativeTarget",
            "name": "AppUITests",
            "productType": "com.apple.product-type.bundle.ui-testing",
            "fileSystemSynchronizedGroups": [UI_GROUP],
            "buildPhases": [],
        },
    }
    data = {"objectVersion": "77", "rootObject": "R" * 24, "objects": objects}
    (project / "project.pbxproj").write_bytes(plistlib.dumps(data))
    (root / "AppUITests").mkdir()
    return project


# --- synchronized folders -------------------------------------------------------------


def test_synchronized_folder_is_found_for_the_target(tmp_path):
    p = PbxProject(write_synchronized_project(tmp_path))
    assert p.synchronized_folder(p.target_named("AppUITests")) == "AppUITests"


def test_driver_goes_into_the_synchronized_folder_without_a_file_reference(tmp_path):
    """The fixture-era path added a PBXFileReference too, which resolved against the
    main group and failed the build: 'Build input file cannot be found'."""
    write_synchronized_project(tmp_path / "src")
    ws = prepare_workspace(
        tmp_path / "src",
        scratch_dir=tmp_path / "scratch",
        project_relpath="App.xcodeproj",
        surfaces=[Surface("Dex")],
    )
    assert ws.driver_path == tmp_path / "scratch/AppUITests" / DRIVER_FILENAME
    assert ws.driver_path.exists()
    refs = [
        o for o in PbxProject(ws.project).objects.values() if o.get("isa") == "PBXFileReference"
    ]
    assert refs == []


# --- driver ---------------------------------------------------------------------------


def test_launch_arguments_are_set_before_launch_and_escaped():
    src = render_driver([Surface("Dex", launch_arguments=("-initialTab", 'a"b'))])
    set_args = src.index('app.launchArguments = ["-initialTab", "a\\"b"]')
    assert set_args < src.index("app.launch()")


def test_no_launch_arguments_renders_no_assignment():
    assert "launchArguments" not in render_driver([Surface("Dex")])


def test_alert_dismissal_uses_only_configured_titles():
    src = render_driver([Surface("Dex")], dismiss_alerts=("Allow While Using App",))
    assert 'alertButtons: [String] = ["Allow While Using App"]' in src
    assert "com.apple.springboard" in src
    # An unknown alert fails loudly rather than tapping a guessed button.
    assert "SIGHTLINE-UNDISMISSED-ALERT" in src
    assert src.index("dismissSystemAlerts()\n", src.index("app.launch()")) > 0


def test_driver_marks_a_completed_audit_after_the_audit_call():
    src = render_driver([Surface("Dex")])
    assert src.index("performAccessibilityAudit") < src.index('"SIGHTLINE-AUDITED|')


# --- xcresult: a thrown audit is not a clean screen -----------------------------------


def test_audited_screens_come_from_the_completion_marker():
    data = {
        "testRuns": [
            {
                "activities": [
                    {"title": "Launch"},
                    {"title": "Outer", "childActivities": [{"title": "SIGHTLINE-AUDITED|Dex"}]},
                ]
            }
        ]
    }
    assert parse_audited_screens(data) == frozenset({"Dex"})


def test_a_thrown_audit_leaves_no_completion_marker():
    data = {"testRuns": [{"activities": [{"title": "Launch"}]}]}
    assert parse_audited_screens(data) == frozenset()


def test_summary_carries_failure_text():
    summary = parse_summary(
        {
            "testFailures": [
                {"testName": "testSurface_Dex()", "failureText": "Invalid target app 20040"}
            ]
        }
    )
    assert summary.failures == ("testSurface_Dex(): Invalid target app 20040",)


# --- config ---------------------------------------------------------------------------


def test_config_override_is_used_instead_of_the_repo_file(tmp_path):
    config = RepoConfig.load(tmp_path, override=NEARBY_MEOWS / "config.yml")
    (dex,) = config.surfaces_for(frozenset({"DexView"}))
    assert dex.launch_arguments == ("-hasCompletedOnboarding", "YES", "-initialTab", "Dex")
    assert config.dismiss_alerts == ["Allow While Using App"]


def test_missing_override_is_an_error_not_an_empty_config(tmp_path):
    with pytest.raises(FileNotFoundError):
        RepoConfig.load(tmp_path, override=tmp_path / "nope.yml")


# --- local git forge ------------------------------------------------------------------


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def repo(tmp_path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    (root / "View.swift").write_text("struct V {\n    let a = 1\n}\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "base")
    git(root, "checkout", "-qb", "feature")
    (root / "View.swift").write_text("struct V {\n    let a = 1\n    let b = 2\n}\n")
    git(root, "commit", "-qam", "head")
    git(root, "checkout", "-q", "main")
    return root


def test_local_diff_is_the_three_dot_diff(repo):
    forge = LocalGitAdapter(repo, "main", "feature")
    (changed,) = forge.get_diff("r", 0).files
    assert changed.path == "View.swift"
    assert changed.added_lines == {3}
    pr = forge.get_pull_request("r", 0)
    assert pr.base_sha == git(repo, "rev-parse", "main").strip()


def test_get_file_reads_the_ref_not_the_working_tree(repo):
    forge = LocalGitAdapter(repo, "main", "feature")
    assert "let b = 2" in forge.get_file("r", "View.swift", forge.head_sha)
    assert forge.get_file("r", "Missing.swift", forge.head_sha) is None


def test_materialize_exports_head_and_leaves_the_repo_alone(repo, tmp_path):
    forge = LocalGitAdapter(repo, "main", "feature")
    out = forge.materialize(tmp_path / "out")
    assert "let b = 2" in (out / "View.swift").read_text()
    assert git(repo, "branch", "--show-current").strip() == "main"
    assert "let b" not in (repo / "View.swift").read_text()
    assert git(repo, "status", "--porcelain") == ""


def test_local_forge_cannot_post(repo):
    forge = LocalGitAdapter(repo, "main", "feature")
    with pytest.raises(ForgeError):
        forge.post_review(forge.get_pull_request("r", 0), [], summary="")


def test_unknown_ref_is_a_forge_error(repo):
    with pytest.raises(ForgeError):
        LocalGitAdapter(repo, "main", "nope")


# --- the Nearby Meows eval cases ------------------------------------------------------


def test_nearby_meows_cases_are_one_defect_and_one_clean():
    cases = discover_cases(NEARBY_MEOWS)
    assert [c.kind for c in cases] == ["true_positive", "clean"]
    assert {c.base for c in cases} == {"61a9a740980714ac4d3e0428e9abb9377ede0626"}
    assert all(c.config == c.directory / "../config.yml" for c in cases)
    assert [e.key for e in cases[0].expected] == ["sufficientElementDescription:dex.help"]
    assert cases[1].expected == ()


def test_nearby_meows_cases_differ_only_by_the_label():
    seeded, clean = (c.patch.read_text().splitlines() for c in discover_cases(NEARBY_MEOWS))
    added = lambda lines: [ln for ln in lines if ln.startswith("+") and "+++" not in ln]
    assert set(added(clean)) - set(added(seeded)) == {
        '+                    .accessibilityLabel("How the dex works")'
    }
