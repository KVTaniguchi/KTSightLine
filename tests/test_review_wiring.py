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


def test_issue_without_an_identifier_is_unanchorable():
    changed = parse_diff(DIFF).files[0]
    findings, unmapped = build_findings(
        (_issue(""),), changed=changed, source=SOURCE, evidence=[_ref()]
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
