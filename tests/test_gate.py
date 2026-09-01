"""The runtime-tier gate (ADR-0003 §2).

Order matters here, so most of these tests are about precedence: which stage wins when
two would fire. A human override beats the automatic decision in both directions; the
fork check beats the human override, because credentials genuinely are not there.
"""

from pathlib import Path

from sightline.adapters.forge.base import PullRequest
from sightline.core.diff.parser import parse_diff
from sightline.core.impact.analyzer import analyze
from sightline.core.skills.loader import load_skill
from sightline.gate import FORCE_LABEL, SKIP_LABEL, GateInputs, decide

REPO = Path(__file__).resolve().parent.parent
APP = REPO / "eval/fixtures/CheckoutDemo/CheckoutDemo"

UI_DIFF = (
    "diff --git a/App/CartView.swift b/App/CartView.swift\n"
    "--- a/App/CartView.swift\n+++ b/App/CartView.swift\n"
    "@@ -10,1 +10,2 @@\n ctx\n+struct CartView: View {\n"
)
DOCS_DIFF = (
    "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n"
    "@@ -1,1 +1,2 @@\n ctx\n+text\n"
)


def skills():
    return [load_skill(p) for p in sorted((REPO / "skills").glob("*.md"))]


def pr(**over) -> PullRequest:
    kw = {"repo": "o/r", "number": 1, "base_sha": "b", "head_sha": "h"}
    kw.update(over)
    return PullRequest(**kw)


def inputs(diff_text=UI_DIFF, *, pull=None, skill_list=None, budget=1.0) -> GateInputs:
    diff = parse_diff(diff_text)
    return GateInputs(
        pr=pull or pr(),
        impact=analyze(diff),
        changed_paths=diff.paths,
        skills=skills() if skill_list is None else skill_list,
        budget_remaining_usd=budget,
    )


def test_ui_change_admits_the_runtime_tier():
    trace = decide(inputs())
    assert trace.runtime_enabled and trace.stage == "ok"


def test_docs_only_change_is_refused_at_the_glob_stage():
    """The cheapest stage, and the one that eliminates most PRs in a real repo."""
    trace = decide(inputs(DOCS_DIFF))
    assert not trace.runtime_enabled and trace.stage == "path_globs"


def test_swift_change_with_no_ui_trigger_is_refused_at_the_trigger_stage():
    diff_text = (
        "diff --git a/App/Model.swift b/App/Model.swift\n"
        "--- a/App/Model.swift\n+++ b/App/Model.swift\n"
        "@@ -1,1 +1,2 @@\n ctx\n+    let total = 1\n"
    )
    trace = decide(inputs(diff_text))
    assert not trace.runtime_enabled and trace.stage == "triggers"
    assert "no runtime trigger fired" in trace.reason


def test_skip_label_wins_over_a_matching_diff():
    trace = decide(inputs(pull=pr(labels=(SKIP_LABEL,))))
    assert not trace.runtime_enabled and trace.stage == "label"


def test_force_label_wins_over_a_non_matching_diff():
    """Someone who has looked at the PR knows more than the gate does."""
    trace = decide(inputs(DOCS_DIFF, pull=pr(labels=(FORCE_LABEL,))))
    assert trace.runtime_enabled and trace.stage == "label"


def test_skip_label_beats_force_label():
    trace = decide(inputs(pull=pr(labels=(FORCE_LABEL, SKIP_LABEL))))
    assert not trace.runtime_enabled


def test_fork_is_refused_even_when_forced():
    """Not a preference — the cache and credentials genuinely are not there (D7)."""
    trace = decide(inputs(pull=pr(is_fork=True, labels=(FORCE_LABEL,))))
    assert not trace.runtime_enabled and trace.stage == "fork"
    assert "fork" in trace.reason


def test_exhausted_budget_refuses_and_names_the_numbers():
    trace = decide(inputs(budget=0.0))
    assert not trace.runtime_enabled and trace.stage == "budget"
    assert "$0.00" in trace.reason


def test_no_runtime_skills_means_nothing_to_run():
    static_only = [s for s in skills() if s.frontmatter.tier.value == "static"]
    trace = decide(inputs(skill_list=static_only))
    assert not trace.runtime_enabled
    assert "no runtime-tier skills" in trace.reason


def test_every_decision_carries_a_reason():
    for case in [
        inputs(),
        inputs(DOCS_DIFF),
        inputs(pull=pr(is_fork=True)),
        inputs(budget=0.0),
    ]:
        assert decide(case).reason
