"""Impact analysis and dispatch — the deterministic layer.

ADR-0001 §2: the impact layer's precision *is* the product's precision. If
`ui_surface_changed` over-fires, every runtime skill over-fires with it and we pay macOS
runner minutes for nothing. So these tests assert on what does NOT fire as much as on
what does.
"""

from pathlib import Path

import pytest

from sightline.core.diff.parser import parse_diff
from sightline.core.impact.analyzer import analyze
from sightline.core.skills.dispatch import Decision, Outcome, RunPolicy, dispatch
from sightline.core.skills.frontmatter import Trigger
from sightline.core.skills.loader import load_skill

REPO = Path(__file__).resolve().parent.parent
APP = REPO / "eval/fixtures/CheckoutDemo/CheckoutDemo"


def diff_for(path: str, added: list[str], *, start: int = 10, header: str = "") -> str:
    body = "".join(f"+{line}\n" for line in added)
    return (
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
        f"@@ -{start},1 +{start},{len(added) + 1} @@{header}\n ctx\n{body}"
    )


def sources_for(path: str, name: str) -> dict[str, str]:
    return {path: (APP / name).read_text()}


# --- impact: what fires --------------------------------------------------------------


def test_swiftui_view_declaration_marks_a_ui_surface():
    d = parse_diff(diff_for("App/CartView.swift", ["struct CartView: View {"]))
    r = analyze(d)
    assert Trigger.UI_SURFACE_CHANGED in r.triggers
    assert Trigger.VIEW_MODIFIED in r.triggers
    assert "CartView" in r.ui_surfaces


def test_new_file_view_emits_view_added_not_view_modified():
    text = (
        "diff --git a/App/New.swift b/App/New.swift\nnew file mode 100644\n"
        "--- /dev/null\n+++ b/App/New.swift\n@@ -0,0 +1,1 @@\n+struct New: View {}\n"
    )
    r = analyze(parse_diff(text))
    assert Trigger.VIEW_ADDED in r.triggers
    assert Trigger.VIEW_MODIFIED not in r.triggers


def test_body_only_edit_to_an_existing_view_still_marks_a_surface():
    """The common case: the diff touches no declaration, only lines inside `body`."""
    path = "App/CheckoutSummaryView.swift"
    d = parse_diff(diff_for(path, ['    .accessibilityIdentifier("x")']))
    r = analyze(d, sources=sources_for(path, "CheckoutSummaryView.swift"))
    assert Trigger.UI_SURFACE_CHANGED in r.triggers
    assert "CheckoutSummaryView" in r.ui_surfaces


def test_permission_api_fires_with_the_api_named_in_evidence():
    path = "App/ScanCardView.swift"
    d = parse_diff(diff_for(path, ["        AVCaptureDevice.requestAccess(for: .video) { _ in }"]))
    r = analyze(d, sources=sources_for(path, "ScanCardView.swift"))
    assert Trigger.PERMISSION_API_REFERENCED in r.triggers
    assert "AVCaptureDevice" in r.why(Trigger.PERMISSION_API_REFERENCED)[0].reason


@pytest.mark.parametrize(
    "path,expected",
    [
        ("App/Info.plist", Trigger.INFO_PLIST_CHANGED),
        ("App/PrivacyInfo.xcprivacy", Trigger.PRIVACY_MANIFEST_CHANGED),
        ("App/App.entitlements", Trigger.ENTITLEMENTS_CHANGED),
        ("App/en.lproj/Localizable.strings", Trigger.LOCALIZATION_CHANGED),
        ("App/Assets.xcassets/Color.colorset/Contents.json", Trigger.ASSET_CATALOG_CHANGED),
        ("App/Model.xcdatamodeld/Model.xcdatamodel/contents", Trigger.CORE_DATA_MODEL_CHANGED),
        ("Package.resolved", Trigger.PACKAGE_RESOLVED_CHANGED),
        ("App.xcodeproj/project.pbxproj", Trigger.BUILD_SETTINGS_CHANGED),
        ("App/Base.lproj/Main.storyboard", Trigger.UI_SURFACE_CHANGED),
    ],
)
def test_path_shaped_triggers(path, expected):
    assert expected in analyze(parse_diff(diff_for(path, ["whatever"]))).triggers


def test_concurrency_suppression_is_flagged():
    d = parse_diff(diff_for("App/Store.swift", ["final class Store: @unchecked Sendable {"]))
    assert Trigger.CONCURRENCY_ANNOTATION_ADDED in analyze(d).triggers


def test_navigation_change_is_flagged():
    d = parse_diff(diff_for("App/Router.swift", ["        NavigationLink(value: item) { row }"]))
    assert Trigger.NAVIGATION_GRAPH_CHANGED in analyze(d).triggers


# --- impact: what must NOT fire ------------------------------------------------------


def test_non_ui_swift_change_does_not_mark_a_ui_surface():
    path = "App/Model.swift"
    d = parse_diff(diff_for(path, ["    var subtotalCents: Int { 0 }"]))
    r = analyze(d, sources=sources_for(path, "Model.swift"))
    assert Trigger.UI_SURFACE_CHANGED not in r.triggers
    assert r.ui_surfaces == frozenset()


def test_ui_test_source_is_not_a_ui_surface():
    """UITests reference and declare views constantly. Treating them as surfaces boots
    simulators for a tests-only PR, the exact waste the gate exists to prevent."""
    d = parse_diff(
        diff_for(
            "App/CheckoutDemoUITests/Foo.swift",
            ["struct Thing: View {}", "        NavigationLink(value: x) { row }"],
        )
    )
    r = analyze(d)
    assert Trigger.TEST_TARGET_CHANGED in r.triggers
    assert Trigger.UI_SURFACE_CHANGED not in r.triggers
    assert Trigger.NAVIGATION_GRAPH_CHANGED not in r.triggers
    assert r.ui_surfaces == frozenset()


def test_removed_lines_do_not_trigger():
    """A permission API *deleted* by this PR is not a permission API this PR added."""
    text = (
        "diff --git a/App/Scan.swift b/App/Scan.swift\n--- a/App/Scan.swift\n+++ b/App/Scan.swift\n"
        "@@ -10,2 +10,1 @@\n ctx\n-        AVCaptureDevice.requestAccess(for: .video)\n"
    )
    assert Trigger.PERMISSION_API_REFERENCED not in analyze(parse_diff(text)).triggers


def test_deleted_swift_file_emits_no_content_triggers():
    text = (
        "diff --git a/App/Old.swift b/App/Old.swift\ndeleted file mode 100644\n"
        "--- a/App/Old.swift\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-struct Old: View {}\n"
    )
    r = analyze(parse_diff(text))
    assert Trigger.UI_SURFACE_CHANGED not in r.triggers
    assert Trigger.FILE_CHANGED in r.triggers


def test_every_trigger_carries_evidence():
    d = parse_diff(diff_for("App/CartView.swift", ["struct CartView: View {"]))
    r = analyze(d)
    for trigger in r.triggers:
        assert r.why(trigger), f"{trigger} fired with no evidence"


# --- dispatch ------------------------------------------------------------------------


def builtin_skills():
    return [load_skill(p) for p in sorted((REPO / "skills").glob("*.md"))]


def decision(decisions: list[Decision], skill_id: str) -> Decision:
    return next(d for d in decisions if d.skill_id == skill_id)


def test_runtime_skill_fires_on_a_ui_change():
    path = "App/CheckoutSummaryView.swift"
    d = parse_diff(diff_for(path, ["    .lineLimit(1)"]))
    r = analyze(d, sources=sources_for(path, "CheckoutSummaryView.swift"))
    out = dispatch(builtin_skills(), d.paths, r, RunPolicy.full(allow_experimental=True))
    assert decision(out, "accessibility-audit").fired


def test_static_only_policy_blocks_the_runtime_skill():
    path = "App/CheckoutSummaryView.swift"
    d = parse_diff(diff_for(path, ["    .lineLimit(1)"]))
    r = analyze(d, sources=sources_for(path, "CheckoutSummaryView.swift"))
    out = dispatch(builtin_skills(), d.paths, r, RunPolicy.static_only(allow_experimental=True))
    assert decision(out, "accessibility-audit").outcome is Outcome.TIER_DISABLED


def test_glob_miss_is_recorded_not_silent():
    d = parse_diff(diff_for("docs/README.md", ["text"]))
    r = analyze(d)
    out = dispatch(builtin_skills(), d.paths, r, RunPolicy.full(allow_experimental=True))
    assert all(x.outcome is Outcome.NO_GLOB_MATCH for x in out)
    assert all(x.reason for x in out)


def test_trigger_miss_names_what_was_unmet():
    """Swift file that matches globs but is not a UI surface."""
    path = "App/Model.swift"
    d = parse_diff(diff_for(path, ["    var x = 1"]))
    r = analyze(d, sources=sources_for(path, "Model.swift"))
    out = dispatch(builtin_skills(), d.paths, r, RunPolicy.full(allow_experimental=True))
    dec = decision(out, "accessibility-audit")
    assert dec.outcome is Outcome.NO_TRIGGER_MATCH
    assert "ui_surface_changed" in dec.reason


def test_experimental_skills_are_held_back_by_default():
    path = "App/CheckoutSummaryView.swift"
    d = parse_diff(diff_for(path, ["    .lineLimit(1)"]))
    r = analyze(d, sources=sources_for(path, "CheckoutSummaryView.swift"))
    out = dispatch(builtin_skills(), d.paths, r, RunPolicy.full())
    assert decision(out, "accessibility-audit").outcome is Outcome.NOT_MATURE


def test_budget_exhaustion_names_the_numbers(tmp_path):
    """D6: a denial must be loud enough to put in the run summary."""
    expensive = tmp_path / "expensive.md"
    expensive.write_text(
        "---\nid: expensive\ntrigger_schema: 1\ntier: static\n"
        'globs: ["**/*.swift"]\ntriggers: [file_changed]\n'
        "requires_evidence: [source_span]\nverifier: structured_oracle\n"
        "model_tier: standard\ncost_budget_usd: 4.0\nmaturity: stable\n---\nbody\n"
    )
    d = parse_diff(diff_for("App/A.swift", ["var x = 1"]))
    r = analyze(d)
    out = dispatch([load_skill(expensive)], d.paths, r, RunPolicy.full())
    dec = out[0]
    assert dec.outcome is Outcome.OVER_BUDGET
    assert "$4.00" in dec.reason and "$0.50" in dec.reason


def test_simulator_matrix_outside_the_allowlist_is_denied(tmp_path):
    skill = tmp_path / "wide.md"
    skill.write_text(
        "---\nid: wide\ntrigger_schema: 1\ntier: runtime\n"
        'globs: ["**/*.swift"]\ntriggers: [file_changed]\n'
        "requires_evidence: [screenshot]\nverifier: differential_render\n"
        "capabilities: [{capture: [screenshot]}]\n"
        "simulator_matrix: [se-smallest, pro-max, ipad-split]\nmaturity: stable\n---\nbody\n"
    )
    d = parse_diff(diff_for("App/A.swift", ["var x = 1"]))
    out = dispatch([load_skill(skill)], d.paths, analyze(d), RunPolicy.full())
    assert out[0].outcome is Outcome.MATRIX_NOT_ALLOWED
    assert "pro-max" in out[0].reason


def test_free_skills_never_consume_budget():
    """model_tier: none must be admissible even at a zero budget."""
    d = parse_diff(
        diff_for("App/ScanCardView.swift", ["AVCaptureDevice.requestAccess(for: .video)"])
    )
    r = analyze(d)
    policy = RunPolicy.full(pr_budget_usd=0.0, allow_experimental=True)
    out = dispatch(builtin_skills(), d.paths, r, policy)
    assert decision(out, "missing-usage-description").fired


def test_dispatch_returns_one_decision_per_skill_with_a_reason():
    d = parse_diff(diff_for("App/CartView.swift", ["struct CartView: View {"]))
    skills = builtin_skills()
    out = dispatch(skills, d.paths, analyze(d), RunPolicy.full(allow_experimental=True))
    assert len(out) == len(skills)
    assert {x.skill_id for x in out} == {s.frontmatter.id for s in skills}
    assert all(x.reason for x in out)


# --- end to end over the committed fixture diff --------------------------------------


def test_committed_diff_drives_the_whole_deterministic_layer():
    """diff -> symbols -> triggers -> dispatch, on the real fixture PR.

    This is the D-004 change the first vertical slice targets.
    """
    text = (Path(__file__).resolve().parent / "fixtures/add-help-button.diff").read_text()
    d = parse_diff(text)
    path = d.paths[0]
    r = analyze(d, sources={path: (APP / "CartView.swift").read_text()})

    assert Trigger.UI_SURFACE_CHANGED in r.triggers
    assert "CartView" in r.ui_surfaces
    assert any(s.endswith("CartView.body") for s in r.changed_symbols)

    out = dispatch(builtin_skills(), d.paths, r, RunPolicy.full(allow_experimental=True))
    assert decision(out, "accessibility-audit").fired
    # No permission API in this diff, so the static skill must stay quiet.
    assert decision(out, "missing-usage-description").outcome is Outcome.NO_TRIGGER_MATCH
