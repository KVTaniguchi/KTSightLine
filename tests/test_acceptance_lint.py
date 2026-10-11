"""The criteria linter. Every rejection must name the fix (ADR-0004 §2)."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from sightline.config import RepoConfig
from sightline.core.acceptance.lint import has_errors, lint_file, lint_text

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "eval" / "fixtures" / "CheckoutDemo"
EXAMPLE = REPO / "eval" / "acceptance" / "examples" / "CHK-7.yml"


def _config(**extra) -> RepoConfig:
    base = RepoConfig.load(FIXTURE).model_dump()
    base.update(extra)
    return RepoConfig.model_validate(base)


def _lint(criteria_yaml: str, config: RepoConfig | None = None):
    text = "acceptance_schema: 1\nspec_id: T\ncriteria:\n" + dedent(criteria_yaml)
    _, issues = lint_text(text, config or _config())
    return issues


def _errors(issues) -> list[str]:
    return [str(i) for i in issues if i.level == "error"]


def test_example_lints_clean_of_errors():
    _, issues = lint_file(EXAMPLE, _config())
    assert not has_errors(issues), [str(i) for i in issues]


def test_ui_state_without_expectation_or_manual():
    errors = _errors(
        _lint("""
      - id: A1
        given: g
        when: w
        then: t
        surface: Cart
    """)
    )
    assert errors == [
        (
            "error: A1: a ui_state criterion needs at least one `expect` "
            "(e.g. `expect: [{id: <accessibility id>, exists: true}]`), "
            "or `manual: true` with a `reason`"
        )
    ]


def test_log_event_without_expect_log():
    errors = _errors(
        _lint("""
      - id: A1
        given: g
        when: w
        then: t
        evidence: log_event
    """)
    )
    assert len(errors) == 1 and "needs at least one `expect_log`" in errors[0]


def test_unknown_step_names_the_word():
    errors = _errors(
        _lint("""
      - id: A1
        given: g
        when: w
        then: t
        steps:
          - swipe: cart.continue
        expect:
          - id: cart.continue
            exists: true
    """)
    )
    assert errors == ["error: A1: step `swipe` is not in the v1 vocabulary (tap, type, wait_for)"]


def test_unknown_attribute_names_the_word():
    errors = _errors(
        _lint("""
      - id: A1
        given: g
        when: w
        then: t
        expect:
          - id: cart.continue
            color: blue
    """)
    )
    assert errors == [
        (
            "error: A1: expectation attribute `color` is not in the v1 vocabulary "
            "(exists, label, label_contains, value, enabled)"
        )
    ]


def test_unknown_surface_lists_the_configured_ones():
    errors = _errors(
        _lint("""
      - id: A1
        given: g
        when: w
        then: t
        surface: Checkout
        expect:
          - id: x
            exists: true
    """)
    )
    assert len(errors) == 1
    assert "surface `Checkout` is not configured" in errors[0]
    assert "'CheckoutSummary'" in errors[0]


def test_unknown_fixture_names_the_fix():
    errors = _errors(
        _lint("""
      - id: A1
        given: g
        when: w
        then: t
        fixture: empty_cart
        expect:
          - id: x
            exists: true
    """)
    )
    assert errors == [
        (
            "error: A1: fixture `empty_cart` is not configured; add it under `fixtures:` in "
            ".sightline/config.yml or use one of (none configured)"
        )
    ]


def test_colliding_ids():
    errors = _errors(
        _lint("""
      - id: CHK-7.AC1
        given: g
        when: w
        then: t
        manual: true
        reason: r
      - id: CHK.7-AC1
        given: g
        when: w
        then: t
        manual: true
        reason: r
    """)
    )
    assert errors == [
        (
            "error: CHK.7-AC1: collides with `CHK-7.AC1`: both become the test method "
            "`test_AC_CHK_7_AC1` (`.` and `-` both map to `_`); rename one"
        )
    ]


def test_duplicate_ids():
    errors = _errors(
        _lint("""
      - id: A1
        given: g
        when: w
        then: t
        manual: true
        reason: r
      - id: A1
        given: g
        when: w
        then: t
        manual: true
        reason: r
    """)
    )
    assert "error: A1: id appears 2 times; criterion ids must be unique" in errors


def test_typo_key_is_an_error_naming_the_key():
    errors = _errors(
        _lint("""
      - id: A1
        given: g
        when: w
        then: t
        expct:
          - id: x
            exists: true
    """)
    )
    assert len(errors) == 1 and "unknown key `expct`" in errors[0] and "A1" in errors[0]


def test_bad_id_names_the_shape():
    errors = _errors(
        _lint("""
      - id: CHK_7
        given: g
        when: w
        then: t
        manual: true
        reason: r
    """)
    )
    assert len(errors) == 1 and "e.g. `CHK-7.AC1`" in errors[0]


def test_unsupported_evidence_warns_not_errors():
    issues = _lint("""
      - id: A1
        given: g
        when: w
        then: t
        evidence: network_call
    """)
    assert not _errors(issues)
    assert any("not_verifiable(evidence_type_unsupported)" in str(i) for i in issues)


def test_observation_without_exists_warns_about_no_observable():
    issues = _lint("""
      - id: A1
        given: g
        when: w
        then: t
        expect:
          - id: checkout.subtotal
            label: x
    """)
    assert not _errors(issues)
    assert any("not_verifiable(no_observable)" in str(i) for i in issues)


def test_locale_in_a_fixture_is_rejected():
    config = _config(fixtures={"fr": {"launch_arguments": ["-AppleLocale", "fr_FR"]}})
    errors = _errors(
        _lint(
            """
      - id: A1
        given: g
        when: w
        then: t
        manual: true
        reason: r
    """,
            config,
        )
    )
    assert errors == ["error: fixture `fr` sets the locale; remove it, set `acceptance.locale`"]


def test_not_yaml():
    _, issues = lint_text("a: [", _config())
    assert has_errors(issues) and "not valid YAML" in str(issues[0])
