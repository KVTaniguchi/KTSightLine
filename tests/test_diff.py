"""Diff parsing and Swift symbol resolution.

Line arithmetic here decides where every comment lands. ADR-0003 calls getting it wrong
instantly disqualifying, so these tests are about arithmetic, not plumbing.
"""

from pathlib import Path

from sightline.core.diff.models import ChangeType, LineKind
from sightline.core.diff.parser import parse_diff
from sightline.core.diff.swift import UNRESOLVED, SwiftOutline

REPO = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"

SIMPLE = """diff --git a/A.swift b/A.swift
index 111..222 100644
--- a/A.swift
+++ b/A.swift
@@ -10,4 +10,5 @@ struct A {
 keep one
-gone
+new one
+new two
 keep two
"""


def test_line_numbering_is_independent_per_side():
    f = parse_diff(SIMPLE).files[0]
    kinds = [(l.kind, l.old_line, l.new_line) for l in f.hunks[0].lines]
    assert kinds == [
        (LineKind.CONTEXT, 10, 10),
        (LineKind.REMOVED, 11, None),
        (LineKind.ADDED, None, 11),
        (LineKind.ADDED, None, 12),
        (LineKind.CONTEXT, 12, 13),
    ]


def test_added_removed_and_commentable_lines():
    f = parse_diff(SIMPLE).files[0]
    assert f.added_lines == {11, 12}
    assert f.removed_lines == {11}
    assert f.context_lines == {10, 13}
    # Context inside a hunk is commentable on the RIGHT side; removed lines are not.
    assert f.commentable_lines == {10, 11, 12, 13}


def test_hunk_header_defaults_count_to_one():
    f = parse_diff(
        "diff --git a/A.swift b/A.swift\n--- a/A.swift\n+++ b/A.swift\n@@ -5 +5 @@\n-old\n+new\n"
    ).files[0]
    assert (f.hunks[0].old_count, f.hunks[0].new_count) == (1, 1)
    assert f.added_lines == {5}


def test_multiple_hunks_keep_separate_offsets():
    text = (
        "diff --git a/A.swift b/A.swift\n--- a/A.swift\n+++ b/A.swift\n"
        "@@ -1,2 +1,3 @@\n ctx\n+added early\n ctx2\n"
        "@@ -50,2 +51,3 @@\n ctx\n+added late\n ctx2\n"
    )
    f = parse_diff(text).files[0]
    assert f.added_lines == {2, 52}


def test_new_file_is_classified_and_has_no_removed_lines():
    text = (
        "diff --git a/New.swift b/New.swift\nnew file mode 100644\n"
        "--- /dev/null\n+++ b/New.swift\n@@ -0,0 +1,2 @@\n+import SwiftUI\n+struct New: View {}\n"
    )
    f = parse_diff(text).files[0]
    assert f.change_type is ChangeType.ADDED
    assert f.added_lines == {1, 2}
    assert f.removed_lines == set()


def test_deleted_file_is_classified():
    text = (
        "diff --git a/Old.swift b/Old.swift\ndeleted file mode 100644\n"
        "--- a/Old.swift\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-import SwiftUI\n-struct Old {}\n"
    )
    f = parse_diff(text).files[0]
    assert f.change_type is ChangeType.DELETED
    assert f.added_lines == set()


def test_rename_keeps_both_paths():
    text = (
        "diff --git a/Old.swift b/New.swift\nsimilarity index 98%\n"
        "rename from Old.swift\nrename to New.swift\n"
    )
    f = parse_diff(text).files[0]
    assert f.change_type is ChangeType.RENAMED
    assert (f.old_path, f.path) == ("Old.swift", "New.swift")


def test_no_newline_marker_is_not_a_diff_line():
    text = (
        "diff --git a/A.swift b/A.swift\n--- a/A.swift\n+++ b/A.swift\n"
        "@@ -1,1 +1,1 @@\n-old\n+new\n\\ No newline at end of file\n"
    )
    f = parse_diff(text).files[0]
    assert len(f.hunks[0].lines) == 2


def test_added_text_yields_head_line_numbers():
    f = parse_diff(SIMPLE).files[0]
    assert list(f.added_text()) == [(11, "new one"), (12, "new two")]


def test_multi_file_diff_splits_correctly():
    text = SIMPLE + SIMPLE.replace("A.swift", "B.swift")
    d = parse_diff(text)
    assert d.paths == ["A.swift", "B.swift"]
    assert d.by_path("B.swift") is not None
    assert d.by_path("nope.swift") is None


# --- Swift outline -------------------------------------------------------------------


def _fixture_source(name: str) -> str:
    return (REPO / "eval/fixtures/CheckoutDemo/CheckoutDemo" / name).read_text()


def test_outline_resolves_body_of_a_real_fixture_view():
    src = _fixture_source("CheckoutSummaryView.swift")
    outline = SwiftOutline(src)
    target = next(
        n for n, l in enumerate(src.splitlines(), 1) if "checkout.estimatedDeliveryLabel" in l
    )
    assert outline.enclosing_symbol(target) == "CheckoutSummaryView.body"


def test_outline_resolves_a_private_method():
    src = _fixture_source("ScanCardView.swift")
    outline = SwiftOutline(src)
    target = next(n for n, l in enumerate(src.splitlines(), 1) if "AVCaptureDevice" in l)
    assert outline.enclosing_symbol(target) == "ScanCardView.requestCameraAccess"


def test_outline_returns_unresolved_above_any_declaration():
    outline = SwiftOutline(
        "import SwiftUI\n\nstruct A: View {\n  var body: some View { EmptyView() }\n}\n"
    )
    assert outline.enclosing_symbol(1) == UNRESOLVED


def test_outline_ignores_braces_inside_strings_and_comments():
    src = 'struct A {\n  let s = "{{{"  // }}}\n  var x = 1\n}\n'
    outline = SwiftOutline(src)
    assert outline.enclosing_symbol(3) == "A"


def test_outline_handles_nested_types():
    src = "enum Outer {\n  struct Inner {\n    var value = 1\n  }\n}\n"
    assert SwiftOutline(src).enclosing_symbol(3) == "Outer.Inner"


def test_committed_diff_fixture_parses():
    d = parse_diff((FIXTURES / "add-help-button.diff").read_text())
    f = d.files[0]
    assert f.path.endswith("CartView.swift")
    assert len(f.added_lines) == 8
    assert min(f.added_lines) == 53
