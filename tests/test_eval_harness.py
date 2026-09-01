"""Eval corpus loading and scoring.

The parts that run without Xcode. `run_case` itself needs a simulator and is exercised
by `sightline eval`.
"""

from pathlib import Path

import pytest

from sightline.eval_harness import (
    Case,
    CaseResult,
    Expectation,
    Score,
    _assert_patch_landed,
    discover_cases,
)

REPO = Path(__file__).resolve().parent.parent
CORPUS = REPO / "eval/corpus"


def test_corpus_has_the_three_cases_the_brief_asked_for():
    cases = discover_cases(CORPUS)
    kinds = [c.kind for c in cases]
    assert len(cases) == 3
    assert kinds.count("true_positive") == 2
    assert kinds.count("clean") == 1


def test_every_case_has_a_patch_that_exists_and_is_non_trivial():
    for case in discover_cases(CORPUS):
        assert case.patch.exists(), case.id
        added = [
            line
            for line in case.patch.read_text().splitlines()
            if line.startswith("+") and not line.startswith("+++")
        ]
        assert added, f"{case.id} patch adds nothing"


def test_clean_case_expects_nothing():
    clean = next(c for c in discover_cases(CORPUS) if c.kind == "clean")
    assert clean.expected == ()


def test_expected_findings_reference_a_file_the_patch_touches():
    for case in discover_cases(CORPUS):
        patch_text = case.patch.read_text()
        for expectation in case.expected:
            assert expectation.file in patch_text, f"{case.id}: {expectation.file}"


def test_expected_identifier_appears_in_the_patch():
    """Guards against an expectation drifting away from what the patch adds."""
    for case in discover_cases(CORPUS):
        patch_text = case.patch.read_text()
        for expectation in case.expected:
            assert expectation.identifier in patch_text, f"{case.id}: {expectation.identifier}"


# --- scoring math ---------------------------------------------------------------------


def _result(tp=(), fp=(), fn=()) -> CaseResult:
    case = Case(
        id="x",
        title="t",
        kind="true_positive",
        directory=Path("."),
        patch=Path("p"),
        expected=(),
    )
    return CaseResult(
        case=case,
        true_positives=list(tp),
        false_positives=list(fp),
        false_negatives=list(fn),
    )


def test_perfect_score():
    score = Score([_result(tp=["a"]), _result(tp=["b"])])
    assert score.precision == 1.0 and score.recall == 1.0 and score.passed


def test_false_positive_lowers_precision_only():
    score = Score([_result(tp=["a"], fp=["b"])])
    assert score.precision == 0.5
    assert score.recall == 1.0
    assert not score.passed


def test_missed_finding_lowers_recall_only():
    score = Score([_result(tp=["a"], fn=["b"])])
    assert score.precision == 1.0
    assert score.recall == 0.5
    assert not score.passed


def test_silence_scores_perfect_precision():
    """A reviewer that says nothing is never wrong, which is why recall matters."""
    score = Score([_result()])
    assert score.precision == 1.0 and score.recall == 1.0


def test_a_case_that_errored_never_passes():
    result = _result()
    result.error = "build failed"
    assert not result.passed
    assert not Score([result]).passed


# --- the guard that catches a silently unapplied patch --------------------------------


def test_patch_landed_guard_accepts_an_applied_patch(tmp_path):
    (tmp_path / "A.swift").write_text('    .accessibilityIdentifier("cart.filters")\n')
    patch = tmp_path / "p.patch"
    patch.write_text('+++ b/A.swift\n+    .accessibilityIdentifier("cart.filters")\n')
    _assert_patch_landed(tmp_path, patch)


def test_patch_landed_guard_catches_a_silent_no_op(tmp_path):
    """`git apply` exits 0 having done nothing when the target is inside another repo.

    That turned every case into a false negative that looked like a harness bug.
    """
    (tmp_path / "A.swift").write_text("unchanged\n")
    patch = tmp_path / "p.patch"
    patch.write_text('+++ b/A.swift\n+    .accessibilityIdentifier("cart.filters")\n')
    with pytest.raises(RuntimeError, match="not in the tree"):
        _assert_patch_landed(tmp_path, patch)


def test_expectation_key_shape():
    assert Expectation("hitRegion", "a.b", "F.swift").key == "hitRegion:a.b"
