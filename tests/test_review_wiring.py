"""Repo config and audit-issue → finding mapping.

The scope test below is the one that matters most for whether anyone keeps the bot
installed: commenting on defects the author did not introduce is how a reviewer gets
muted.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from sightline.config import RepoConfig
from sightline.core.diff.parser import parse_diff
from sightline.core.evidence.models import ArtifactKind, ArtifactRef
from sightline.core.findings.from_audit import build_findings
from sightline.runners.xcode.xcresult import AuditIssue

REPO = Path(__file__).resolve().parent.parent

SOURCE = """import SwiftUI

struct CartView: View {
    var body: some View {
        List {
            Button("Old") {}
                .accessibilityIdentifier("cart.old")
            Button {
            } label: {
                Image(systemName: "questionmark.circle")
            }
            .accessibilityIdentifier("cart.new")
        }
    }
}
"""

# Lines 8-12 are added; 1-7 and 13-15 are context.
DIFF = (
    "diff --git a/CartView.swift b/CartView.swift\n"
    "--- a/CartView.swift\n+++ b/CartView.swift\n"
    "@@ -1,10 +1,15 @@\n"
    + "".join(f" {line}\n" for line in SOURCE.splitlines()[:7])
    + "".join(f"+{line}\n" for line in SOURCE.splitlines()[7:12])
    + "".join(f" {line}\n" for line in SOURCE.splitlines()[12:])
)


def _ref() -> ArtifactRef:
    from datetime import UTC, datetime

    return ArtifactRef.for_content(
        b"bundle",
        kind=ArtifactKind.XCRESULT,
        produced_by="t",
        run_id="r",
        context={},
        created_at=datetime(2026, 8, 31, tzinfo=UTC),
    )


def _issue(identifier: str, audit_type: str = "sufficientElementDescription") -> AuditIssue:
    return AuditIssue(
        screen="Cart",
        audit_type=audit_type,
        identifier=identifier,
        label="questionmark.circle",
        frame="(29.0, 24.0, 18.0, 36.0)",
        description="Label not human-readable",
        test_id="T/t()",
    )


# --- scope ---------------------------------------------------------------------------


def test_finding_anchors_to_a_line_this_pr_added():
    changed = parse_diff(DIFF).files[0]
    findings, unmapped = build_findings(
        (_issue("cart.new"),), changed=changed, source=SOURCE, evidence=[_ref()]
    )
    assert len(findings) == 1
    assert findings[0].anchor.line in changed.added_lines
    assert not unmapped


def test_pre_existing_defect_on_a_context_line_is_out_of_scope():
    """`commentable_lines` includes context, but context is not what the PR changed.

    Anchoring there would comment on defects the author did not introduce. GitHub would
    happily accept the comment, which is exactly why this has to be checked here.
    """
    changed = parse_diff(DIFF).files[0]
    old_line = next(n for n, t in enumerate(SOURCE.splitlines(), 1) if "cart.old" in t)
    assert old_line in changed.commentable_lines  # GitHub would accept it
    assert old_line not in changed.added_lines  # but the PR did not touch it

    findings, unmapped = build_findings(
        (_issue("cart.old"),), changed=changed, source=SOURCE, evidence=[_ref()]
    )
    assert findings == []
    assert [u.reason for u in unmapped] == ["not_in_diff"]


def test_issue_with_no_element_at_all_is_unanchorable():
    """element = nil: no identifier, no label, no frame. Nothing to anchor to."""
    changed = parse_diff(DIFF).files[0]
    orphan = AuditIssue(
        screen="Cart",
        audit_type="hitRegion",
        identifier="",
        label="",
        frame="nil",
        description="Hit area is too small",
        test_id="T/t()",
    )
    findings, unmapped = build_findings(
        (orphan,), changed=changed, source=SOURCE, evidence=[_ref()]
    )
    assert findings == []
    assert [u.reason for u in unmapped] == ["unanchorable"]


def test_element_without_an_identifier_anchors_by_its_label_literal():
    """OQ-FIXTURE-2, corrected: an element can have a label and no identifier."""
    source = (
        "struct V: View {\n"
        "    var body: some View {\n"
        '        Text("Estimated delivery")\n'
        "    }\n"
        "}\n"
    )
    diff = (
        "diff --git a/V.swift b/V.swift\n"
        "--- a/V.swift\n+++ b/V.swift\n"
        "@@ -1,2 +1,5 @@\n"
        + "".join(f" {line}\n" for line in source.splitlines()[:2])
        + "".join(f"+{line}\n" for line in source.splitlines()[2:])
    )
    issue = AuditIssue(
        screen="Checkout",
        audit_type="textClipped",
        identifier="",
        label="Estimated delivery",
        frame="(16.0, 142.0, 120.0, 20.5)",
        description="Text clipped",
        test_id="T/t()",
    )
    findings, unmapped = build_findings(
        (issue,), changed=parse_diff(diff).files[0], source=source, evidence=[_ref()]
    )
    assert not unmapped
    assert '"Estimated delivery"' in source.splitlines()[findings[0].anchor.line - 1]
    assert findings[0].oracle_key == "textClipped:label=Estimated delivery"
    assert '"Estimated delivery"' in findings[0].claim


def test_a_label_anchored_fingerprint_survives_the_label_value_changing():
    """Rule 3: fingerprints are content-derived. A label-anchored claim names a runtime
    value, so without `normalize_claim` stripping quoted literals the bot would re-post
    every time the price changed."""
    from sightline.core.findings.models import fingerprint

    claims = [f'`"Price ${n}.00"` is clipped at this text size.' for n in (185, 999)]
    assert len({fingerprint("accessibility-audit", "V.swift", "V.body", c) for c in claims}) == 1


def test_an_audit_type_outside_the_allowlist_does_not_anchor_by_label():
    """Only `textClipped` may anchor by label (LABEL_ANCHORABLE_AUDIT_TYPES).

    `contrast` is excluded on purpose: measured on eval case 003, the audit correctly
    reports `Contrast failed` against the inner label of a stock `.bordered` button.
    Anchoring that by label would comment on a system button style in every repo.
    """
    changed = parse_diff(DIFF).files[0]
    issue = AuditIssue(
        screen="Cart",
        audit_type="contrast",
        identifier="",
        label="questionmark.circle",
        frame="(29.0, 24.0, 18.0, 36.0)",
        description="Contrast failed",
        test_id="T/t()",
    )
    findings, unmapped = build_findings((issue,), changed=changed, source=SOURCE, evidence=[_ref()])
    assert findings == []
    assert [u.reason for u in unmapped] == ["unanchorable"]


def test_interpolated_label_literal_matches_its_runtime_value():
    r"""`.accessibilityLabel("Price \(item.formattedPrice)")` anchors `Price $185.00`.

    The Cart screen's real `textClipped`. Interpolations are wildcards; the literal
    segments around them must match verbatim.
    """
    source = (
        "struct CartRow: View {\n"
        "    var body: some View {\n"
        "        Text(item.formattedPrice)\n"
        '            .accessibilityLabel("Price \\(item.formattedPrice)")\n'
        "    }\n"
        "}\n"
    )
    diff = (
        "diff --git a/CartView.swift b/CartView.swift\n"
        "--- a/CartView.swift\n+++ b/CartView.swift\n"
        "@@ -1,2 +1,6 @@\n"
        + "".join(f" {line}\n" for line in source.splitlines()[:2])
        + "".join(f"+{line}\n" for line in source.splitlines()[2:])
    )
    issue = AuditIssue(
        screen="Cart",
        audit_type="textClipped",
        identifier="",
        label="Price $185.00",
        frame="(252.5, 256.75, 66.5, 20.5)",
        description="Text clipped",
        test_id="T/t()",
    )
    findings, unmapped = build_findings(
        (issue,), changed=parse_diff(diff).files[0], source=source, evidence=[_ref()]
    )
    assert not unmapped
    assert "accessibilityLabel" in source.splitlines()[findings[0].anchor.line - 1]
    assert findings[0].oracle_key == "textClipped:label=Price $185.00"


def test_a_label_matching_two_changed_lines_is_dropped_as_ambiguous():
    """Picking one would be a guess, and a comment on the wrong line is worse."""
    source = (
        "struct V: View {\n"
        "    var body: some View {\n"
        '        Text("Subtotal")\n'
        '        Text("Subtotal")\n'
        "    }\n"
        "}\n"
    )
    diff = (
        "diff --git a/V.swift b/V.swift\n"
        "--- a/V.swift\n+++ b/V.swift\n"
        "@@ -1,2 +1,6 @@\n"
        + "".join(f" {line}\n" for line in source.splitlines()[:2])
        + "".join(f"+{line}\n" for line in source.splitlines()[2:])
    )
    issue = AuditIssue(
        screen="Cart",
        audit_type="textClipped",
        identifier="",
        label="Subtotal",
        frame="(1.0, 2.0, 3.0, 4.0)",
        description="Text clipped",
        test_id="T/t()",
    )
    findings, unmapped = build_findings(
        (issue,), changed=parse_diff(diff).files[0], source=source, evidence=[_ref()]
    )
    assert findings == []
    assert [u.reason for u in unmapped] == ["ambiguous_label"]


def test_a_literal_too_thin_to_identify_anything_does_not_anchor():
    r"""`Text("\(total)")` would otherwise match every observed label."""
    source = 'struct V: View {\n    var body: some View {\n        Text("\\(total)")\n    }\n}\n'
    diff = (
        "diff --git a/V.swift b/V.swift\n"
        "--- a/V.swift\n+++ b/V.swift\n"
        "@@ -1,2 +1,5 @@\n"
        + "".join(f" {line}\n" for line in source.splitlines()[:2])
        + "".join(f"+{line}\n" for line in source.splitlines()[2:])
    )
    issue = AuditIssue(
        screen="Cart",
        audit_type="textClipped",
        identifier="",
        label="$441.00",
        frame="(1.0, 2.0, 3.0, 4.0)",
        description="Text clipped",
        test_id="T/t()",
    )
    findings, unmapped = build_findings(
        (issue,), changed=parse_diff(diff).files[0], source=source, evidence=[_ref()]
    )
    assert findings == []
    assert [u.reason for u in unmapped] == ["unanchorable"]


# --- claims and suggestions -----------------------------------------------------------


def test_missing_label_gets_a_mechanical_suggestion():
    changed = parse_diff(DIFF).files[0]
    (finding,), _ = build_findings(
        (_issue("cart.new"),), changed=changed, source=SOURCE, evidence=[_ref()]
    )
    assert finding.suggestion is not None
    assert ".accessibilityLabel(" in finding.suggestion
    assert "questionmark.circle" in finding.claim


def test_hit_region_reports_measured_size_and_offers_no_suggestion():
    """The fix needs design intent, so we give none. A wrong suggestion costs trust."""
    changed = parse_diff(DIFF).files[0]
    (finding,), _ = build_findings(
        (_issue("cart.new", "hitRegion"),), changed=changed, source=SOURCE, evidence=[_ref()]
    )
    assert "18×36pt" in finding.claim
    assert finding.suggestion is None


def test_oracle_key_is_derived_from_the_audit_record():
    changed = parse_diff(DIFF).files[0]
    (finding,), _ = build_findings(
        (_issue("cart.new", "hitRegion"),), changed=changed, source=SOURCE, evidence=[_ref()]
    )
    assert finding.oracle_key == "hitRegion:cart.new"


def test_enclosing_symbol_is_resolved_for_the_fingerprint():
    changed = parse_diff(DIFF).files[0]
    (finding,), _ = build_findings(
        (_issue("cart.new"),), changed=changed, source=SOURCE, evidence=[_ref()]
    )
    assert finding.enclosing_symbol == "CartView.body"


# --- config ---------------------------------------------------------------------------


def test_missing_config_is_a_valid_state(tmp_path):
    config = RepoConfig.load(tmp_path)
    assert config.project is None and config.surfaces == {}


def test_surfaces_match_changed_view_types(tmp_path):
    (tmp_path / ".sightline").mkdir()
    (tmp_path / ".sightline/config.yml").write_text(
        "project: A.xcodeproj\n"
        "surfaces:\n"
        "  Cart:\n    view: CartView\n    wait_for: cart.continue\n"
        "  Checkout:\n    view: CheckoutView\n    taps: [cart.continue]\n"
    )
    config = RepoConfig.load(tmp_path)
    matched = config.surfaces_for(frozenset({"CartView"}))
    assert [s.name for s in matched] == ["Cart"]
    assert matched[0].wait_for == "cart.continue"


def test_surface_key_is_used_when_no_view_is_declared(tmp_path):
    (tmp_path / ".sightline").mkdir()
    (tmp_path / ".sightline/config.yml").write_text("surfaces:\n  CartView:\n    taps: []\n")
    assert [s.name for s in RepoConfig.load(tmp_path).surfaces_for(frozenset({"CartView"}))] == [
        "CartView"
    ]


def test_no_matching_surface_means_no_runtime_work(tmp_path):
    (tmp_path / ".sightline").mkdir()
    (tmp_path / ".sightline/config.yml").write_text("surfaces:\n  Cart:\n    view: CartView\n")
    assert RepoConfig.load(tmp_path).surfaces_for(frozenset({"SettingsView"})) == []


def test_unknown_config_key_is_rejected(tmp_path):
    (tmp_path / ".sightline").mkdir()
    (tmp_path / ".sightline/config.yml").write_text("projekt: A.xcodeproj\n")
    with pytest.raises(ValidationError):
        RepoConfig.load(tmp_path)


def test_the_repos_own_config_is_valid():
    """This repo ships a config pointed at the vendored fixture; keep it loadable."""
    config = RepoConfig.load(REPO)
    assert config.project == "eval/fixtures/CheckoutDemo/CheckoutDemo.xcodeproj"
    assert "Cart" in config.surfaces
    assert config.simulator.udid is None, "a machine-specific UDID must not be committed"
