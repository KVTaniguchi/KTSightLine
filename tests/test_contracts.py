"""The invariants the whole design rests on. If one of these breaks, the product is wrong.

Each test names the ADR rule it defends.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from sightline.core.evidence.models import ArtifactKind, ArtifactRef
from sightline.core.findings.models import (
    Anchor,
    ProposedFinding,
    Side,
    VerifiedFinding,
    fingerprint,
    normalize_claim,
)
from sightline.core.skills.frontmatter import ModelTier, Tier
from sightline.core.skills.loader import SkillLoadError, load_skill

REPO = Path(__file__).resolve().parent.parent


def _artifact(**over) -> ArtifactRef:
    kw = {
        "content": b"fake-png-bytes",
        "kind": ArtifactKind.SCREENSHOT,
        "produced_by": "simulator.capture.screenshot",
        "run_id": "run-1",
        "context": {"device": "iPhone SE (3rd generation)", "content_size": "AX5"},
        "created_at": datetime(2026, 8, 31, tzinfo=UTC),
    }
    kw.update(over)
    return ArtifactRef.for_content(**kw)


def _proposed(**over) -> ProposedFinding:
    kw = {
        "rule_id": "accessibility-audit",
        "anchor": Anchor(file="Sources/CheckoutSummaryView.swift", line=142),
        "enclosing_symbol": "CheckoutSummaryView.body",
        "severity": "high",
        "claim": '"Estimated delivery" truncates to "Estimated de…" at AX5 on iPhone SE.',
        "evidence": [_artifact()],
    }
    kw.update(over)
    return ProposedFinding(**kw)


# --- ADR-0002 §2: only the gate can produce a postable finding -------------------------


def test_verified_finding_cannot_be_constructed_directly():
    p = _proposed()
    with pytest.raises(TypeError, match="constructible only by"):
        VerifiedFinding(proposed=p, verdict=None, verified_by="structured_oracle")


# --- ADR-0002 §2: evidence is required by the type, not by convention ------------------


def test_finding_without_evidence_is_rejected():
    with pytest.raises(ValidationError):
        _proposed(evidence=[])


# --- ADR-0002 §4: fingerprints are content-derived, never position-derived -------------


def test_fingerprint_is_stable_across_line_moves():
    """The single most important property. A bot that re-posts on every push is dead."""
    a = _proposed(anchor=Anchor(file="Sources/CheckoutSummaryView.swift", line=142))
    b = _proposed(anchor=Anchor(file="Sources/CheckoutSummaryView.swift", line=207))
    assert a.fingerprint == b.fingerprint


def test_fingerprint_ignores_truncated_literals_and_digits():
    """A one-pixel font change must not re-post the comment."""
    a = _proposed(claim='"Estimated delivery" truncates to "Estimated de…" at AX5.')
    b = _proposed(claim='"Estimated delivery" truncates to "Estimated deliv…" at AX5.')
    assert a.fingerprint == b.fingerprint


def test_fingerprint_separates_distinct_claims():
    a = _proposed()
    b = _proposed(claim="Contrast ratio is below the minimum for this label.")
    assert a.fingerprint != b.fingerprint


def test_fingerprint_separates_same_claim_in_different_symbols():
    a = _proposed(enclosing_symbol="CheckoutSummaryView.body")
    b = _proposed(enclosing_symbol="CheckoutSummaryView.footer")
    assert a.fingerprint != b.fingerprint


def test_normalize_claim_strips_paths_digits_and_quotes():
    out = normalize_claim('Label in "Foo.swift" clips at 142 points')
    assert "142" not in out
    assert "foo.swift" not in out


def test_fingerprint_is_deterministic():
    args = ("rule", "A.swift", "Sym.body", "a claim")
    assert fingerprint(*args) == fingerprint(*args)


# --- P8: GitHub multi-line comments need start_line AND start_side ---------------------


def test_multiline_anchor_requires_start_side():
    with pytest.raises(ValidationError, match="start_side is required"):
        Anchor(file="A.swift", line=20, start_line=10)


def test_start_side_without_start_line_is_rejected():
    with pytest.raises(ValidationError, match="meaningless without"):
        Anchor(file="A.swift", line=20, start_side=Side.RIGHT)


def test_valid_multiline_anchor():
    a = Anchor(file="A.swift", line=20, start_line=10, start_side=Side.RIGHT)
    assert a.start_line == 10 and a.side is Side.RIGHT


def test_start_line_after_line_is_rejected():
    with pytest.raises(ValidationError):
        Anchor(file="A.swift", line=10, start_line=20, start_side=Side.RIGHT)


# --- ADR-0002 §1: artifacts are content-addressed -------------------------------------


def test_same_content_gets_same_address():
    assert _artifact().sha256 == _artifact(run_id="run-2").sha256


def test_artifact_uri_matches_digest():
    a = _artifact()
    assert a.uri == f"sightline://evidence/{a.sha256}"


# --- ADR-0001: frontmatter is strict, and the shipped skills actually load -------------


def test_builtin_skills_load():
    paths = sorted((REPO / "skills").glob("*.md"))
    assert paths, "no built-in skills found"
    for p in paths:
        skill = load_skill(p)
        assert skill.frontmatter.id == p.stem
        assert skill.body, f"{p} has no body"


def test_accessibility_audit_declares_verified_audit_types():
    """P5: the spellings that were wrong before 2026-08-31 verification."""
    skill = load_skill(REPO / "skills" / "accessibility-audit.md")
    run_audit = next(c["run_audit"] for c in skill.frontmatter.capabilities if "run_audit" in c)
    types = set(run_audit["types"])
    assert "textClipped" in types and "clippedText" not in types
    assert "trait" in types and "traits" not in types
    assert "sufficientElementDescription" in types


def test_unknown_frontmatter_key_fails_the_skill(tmp_path):
    """A typo'd key that silently means 'never fires' is the failure mode we refuse."""
    p = tmp_path / "typo.md"
    p.write_text(
        "---\n"
        "id: typo\ntrigger_schema: 1\ntier: static\n"
        'globs: ["**/*.swift"]\ntriggers: [file_changed]\n'
        "requires_evidence: [source_span]\nverifier: structured_oracle\n"
        "trigger: [file_changed]\n"  # <- the typo
        "---\nbody\n"
    )
    with pytest.raises(SkillLoadError):
        load_skill(p)


def test_runtime_skill_without_capabilities_is_rejected(tmp_path):
    p = tmp_path / "bad.md"
    p.write_text(
        "---\n"
        "id: bad\ntrigger_schema: 1\ntier: runtime\n"
        'globs: ["**/*.swift"]\ntriggers: [ui_surface_changed]\n'
        "requires_evidence: [screenshot]\nverifier: differential_render\n"
        "---\nbody\n"
    )
    with pytest.raises(SkillLoadError, match="capabilities"):
        load_skill(p)


def test_model_tier_none_cannot_have_a_budget(tmp_path):
    p = tmp_path / "bad2.md"
    p.write_text(
        "---\n"
        "id: bad2\ntrigger_schema: 1\ntier: static\n"
        'globs: ["**/*.swift"]\ntriggers: [file_changed]\n'
        "requires_evidence: [source_span]\nverifier: structured_oracle\n"
        "model_tier: none\ncost_budget_usd: 0.10\n"
        "---\nbody\n"
    )
    with pytest.raises(SkillLoadError, match="cannot have a cost budget"):
        load_skill(p)


def test_static_skill_cannot_declare_a_simulator_matrix(tmp_path):
    p = tmp_path / "bad3.md"
    p.write_text(
        "---\n"
        "id: bad3\ntrigger_schema: 1\ntier: static\n"
        'globs: ["**/*.swift"]\ntriggers: [file_changed]\n'
        "requires_evidence: [source_span]\nverifier: structured_oracle\n"
        "simulator_matrix: [se-smallest]\n"
        "---\nbody\n"
    )
    with pytest.raises(SkillLoadError, match="simulator_matrix"):
        load_skill(p)


# --- D6: every skill's declared budget must fit inside the per-PR cap -----------------


PR_BUDGET_USD = 0.50


def test_builtin_skill_budgets_fit_the_per_pr_cap():
    for p in sorted((REPO / "skills").glob("*.md")):
        fm = load_skill(p).frontmatter
        assert fm.cost_budget_usd <= PR_BUDGET_USD, (
            f"{fm.id} declares ${fm.cost_budget_usd} against a ${PR_BUDGET_USD} PR cap; "
            "it could never be admitted"
        )


def test_static_tier_skills_are_free():
    """Tier 0 runs on every PR, ungated. It must not spend."""
    for p in sorted((REPO / "skills").glob("*.md")):
        fm = load_skill(p).frontmatter
        if fm.tier is Tier.STATIC and fm.model_tier is not ModelTier.NONE:
            pytest.fail(f"{fm.id} is static tier but spends: model_tier={fm.model_tier}")
