"""Build wrapper, evidence store, telemetry, gate, rendering, and the forge adapter."""

import hashlib
import io
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image

from sightline.adapters.forge.base import AnchorRejected, PullRequest
from sightline.adapters.forge.github import GitHubAdapter, comment_payload, validate_anchor
from sightline.core.diff.parser import parse_diff
from sightline.core.evidence.models import ArtifactKind
from sightline.core.evidence.redaction import (
    RedactionError,
    RedactionPolicy,
    RegionMask,
    redact_text,
)
from sightline.core.evidence.store import FilesystemEvidenceStore
from sightline.core.findings.models import (
    _GATE_TOKEN,
    Anchor,
    ProposedFinding,
    Side,
    Verdict,
    VerifiedFinding,
)
from sightline.core.findings.render import TARGET_CHARS, render_comment, render_summary
from sightline.core.telemetry.ledger import Addressal, LedgerEntry, SqliteLedger
from sightline.core.telemetry.trajectory import SkillTrace, Suppression, Trajectory
from sightline.core.verify.gate import (
    StructuredOracleVerifier,
    SuppressionReason,
    run_gate,
)
from sightline.runners.xcode.build import (
    Destination,
    XcodeBuild,
    XcodeBuildError,
    derived_data_key,
)

REPO = Path(__file__).resolve().parent.parent
SHOT = REPO / "eval/fixtures/CheckoutDemo/reference-renders/CheckoutSummary-se-light-default.png"


class FakeRunner:
    def __init__(self, returncode=0, stdout="", stderr="", raises=None):
        self.calls = []
        self._rc, self._out, self._err, self._raises = returncode, stdout, stderr, raises

    def __call__(self, cmd, *rest):
        self.calls.append(list(cmd))
        if self._raises:
            raise self._raises
        return subprocess.CompletedProcess(cmd, self._rc, self._out, self._err)


# --- xcodebuild ----------------------------------------------------------------------


def test_destination_prefers_udid_over_name():
    assert Destination(udid="ABC").as_arg() == "id=ABC"
    assert Destination(name="iPhone SE").as_arg() == "platform=iOS Simulator,name=iPhone SE"


def test_destination_without_either_is_an_error():
    with pytest.raises(XcodeBuildError):
        Destination().as_arg()


def test_build_command_shape(tmp_path):
    r = FakeRunner()
    XcodeBuild(tmp_path / "P.xcodeproj", "S", tmp_path / "dd", run=r).build(Destination(udid="U"))
    argv = r.calls[0]
    assert argv[:2] == ["xcodebuild", "build"]
    assert "-derivedDataPath" in argv and "-destination" in argv


def test_test_refuses_to_reuse_a_result_bundle(tmp_path):
    bundle = tmp_path / "r.xcresult"
    bundle.mkdir()
    with pytest.raises(XcodeBuildError, match="already exists"):
        XcodeBuild(tmp_path / "P.xcodeproj", "S", tmp_path / "dd", run=FakeRunner()).test(
            Destination(udid="U"), result_bundle=bundle
        )


def test_timeout_is_reported_as_fail_open(tmp_path):
    r = FakeRunner(raises=subprocess.TimeoutExpired("xcodebuild", 1))
    with pytest.raises(XcodeBuildError, match="fail open"):
        XcodeBuild(tmp_path / "P.xcodeproj", "S", tmp_path / "dd", run=r).build(
            Destination(udid="U")
        )


def test_failed_build_surfaces_compiler_errors(tmp_path):
    log = "A.swift:3:1: error: cannot find 'Foo'\nB.swift:9:2: warning: unused\n"
    r = FakeRunner(returncode=65, stdout=log)
    result = XcodeBuild(tmp_path / "P.xcodeproj", "S", tmp_path / "dd", run=r).build(
        Destination(udid="U")
    )
    assert not result.succeeded
    assert result.failure_lines == ("A.swift:3:1: error: cannot find 'Foo'",)
    assert len(result.warning_lines) == 1


def test_derived_data_key_changes_with_commit_and_project(tmp_path):
    project = tmp_path / "P.xcodeproj"
    project.mkdir()
    (project / "project.pbxproj").write_text("v1")
    a = derived_data_key(commit_sha="aaa", project=project)
    b = derived_data_key(commit_sha="bbb", project=project)
    assert a != b, "a different commit must not reuse a build"
    (project / "project.pbxproj").write_text("v2")
    assert derived_data_key(commit_sha="aaa", project=project) != a


def test_derived_data_key_distinguishes_missing_from_present(tmp_path):
    """A deleted Package.resolved must not collide with one that exists."""
    project = tmp_path / "P.xcodeproj"
    project.mkdir()
    (project / "project.pbxproj").write_text("v1")
    resolved = tmp_path / "Package.resolved"
    with_missing = derived_data_key(commit_sha="a", project=project, extra=[resolved])
    resolved.write_text("{}")
    assert derived_data_key(commit_sha="a", project=project, extra=[resolved]) != with_missing


# --- redaction and the evidence store -------------------------------------------------


def test_text_redaction_scrubs_tokens_and_pans():
    out = redact_text(
        "Authorization: Bearer abcdef1234567890xyz\ncard 4111 1111 1111 1111\na@b.com",
        RedactionPolicy(),
    )
    assert "abcdef1234567890xyz" not in out
    assert "4111" not in out
    assert "a@b.com" not in out


def test_image_masking_actually_changes_the_pixels(tmp_path):
    policy = RedactionPolicy(masks=(RegionMask(0, 0, 100, 100, "top-left"),), scale=1.0)
    store = FilesystemEvidenceStore(tmp_path, run_id="r", policy=policy)
    ref = store.put(SHOT.read_bytes(), kind=ArtifactKind.SCREENSHOT, produced_by="t", context={})
    with Image.open(io.BytesIO(store.get(ref))) as out:
        assert out.convert("RGB").getpixel((10, 10)) == (17, 17, 17)


def test_unmasked_bytes_never_reach_disk(tmp_path):
    """ADR-0002 §1 states this as an absolute. Assert it, don't trust it."""
    raw = SHOT.read_bytes()
    policy = RedactionPolicy(masks=(RegionMask(0, 0, 200, 200),), scale=1.0)
    store = FilesystemEvidenceStore(tmp_path, run_id="r", policy=policy)
    store.put(raw, kind=ArtifactKind.SCREENSHOT, produced_by="t", context={})
    raw_digest = hashlib.sha256(raw).hexdigest()
    on_disk = {p.read_bytes() for p in tmp_path.rglob("*.png")}
    assert all(hashlib.sha256(b).hexdigest() != raw_digest for b in on_disk)


def test_identical_content_stores_once(tmp_path):
    store = FilesystemEvidenceStore(tmp_path, run_id="r")
    a = store.put(b"x" * 64, kind=ArtifactKind.CONSOLE_LOG, produced_by="t", context={})
    b = store.put(b"x" * 64, kind=ArtifactKind.CONSOLE_LOG, produced_by="other", context={})
    assert a.sha256 == b.sha256
    assert len(list(tmp_path.rglob("*.log"))) == 1


def test_store_roundtrip_and_uri(tmp_path):
    store = FilesystemEvidenceStore(tmp_path, run_id="r")
    ref = store.put(b"hello", kind=ArtifactKind.BUILD_LOG, produced_by="t", context={"os": "26.5"})
    assert store.exists(ref) and store.get(ref) == b"hello"
    assert ref.uri.endswith(ref.sha256)
    assert ref.context["os"] == "26.5"


def test_no_partial_files_survive(tmp_path):
    store = FilesystemEvidenceStore(tmp_path, run_id="r")
    store.put(b"data", kind=ArtifactKind.CONSOLE_LOG, produced_by="t", context={})
    assert not list(tmp_path.rglob("*.partial"))


# --- telemetry ------------------------------------------------------------------------


def test_suppression_summary_is_counts_and_reasons_only():
    """D8: never claim text in what the author sees."""
    t = Trajectory(run_id="r", repo="o/r")
    t.suppressions = [
        Suppression(skill_id="s", claim="secret claim", reason="missing_evidence"),
        Suppression(skill_id="s", claim="another", reason="missing_evidence"),
        Suppression(skill_id="s", claim="third", reason="no_baseline"),
    ]
    assert t.suppression_summary() == {"missing_evidence": 2, "no_baseline": 1}
    assert "secret claim" not in t.summary_line()


def test_summary_line_names_a_budget_denial():
    """D6's mitigation: a skipped skill is never silent."""
    t = Trajectory(run_id="r", repo="o/r")
    t.skills = [
        SkillTrace(skill_id="pricey", outcome="over_budget", reason="declares $4.00, $0.50 left")
    ]
    assert "pricey" in t.summary_line() and "$4.00" in t.summary_line()


def test_trajectory_roundtrips(tmp_path):
    t = Trajectory(run_id="r", repo="o/r", pr_number=7, triggers=["ui_surface_changed"])
    path = t.finish().write(tmp_path / "t.json")
    back = Trajectory.read(path)
    assert back.run_id == "r" and back.triggers == ["ui_surface_changed"]
    assert json.loads(path.read_text())["schema_version"] == 1


def _entry(fp: str, **over) -> LedgerEntry:
    kw = {
        "fingerprint": fp, "repo": "o/r", "pr_number": 1, "skill_id": "a11y",
        "file": "A.swift", "line": 10, "claim": "c", "comment_id": "1",
        "posted_at": datetime(2026, 8, 31, tzinfo=UTC), "head_sha": "abc",
    }  # fmt: skip
    kw.update(over)
    return LedgerEntry(**kw)


def test_ledger_record_is_idempotent(tmp_path):
    """Re-posting on a later push must not inflate the metric's denominator."""
    ledger = SqliteLedger(tmp_path / "l.db")
    ledger.record_posted(_entry("fp1"))
    ledger.record_posted(_entry("fp1"))
    assert len(ledger.open_entries("o/r", 1)) == 1
    assert ledger.already_posted("fp1", "o/r", 1)


def test_addressal_rate_excludes_open_comments(tmp_path):
    """A PR nobody has looked at is not evidence the bot was ignored."""
    ledger = SqliteLedger(tmp_path / "l.db")
    for i in range(4):
        ledger.record_posted(_entry(f"fp{i}"))
    assert ledger.addressal_rate() == 0.0  # nothing decided yet
    ledger.update_status("fp0", "o/r", 1, Addressal.ADDRESSED)
    ledger.update_status("fp1", "o/r", 1, Addressal.DISMISSED)
    assert ledger.addressal_rate() == 0.5  # 1 of 2 decided; fp2/fp3 still open


def test_addressal_rate_filters_by_skill(tmp_path):
    ledger = SqliteLedger(tmp_path / "l.db")
    ledger.record_posted(_entry("a", skill_id="a11y"))
    ledger.record_posted(_entry("b", skill_id="other"))
    ledger.update_status("a", "o/r", 1, Addressal.ADDRESSED)
    ledger.update_status("b", "o/r", 1, Addressal.DISMISSED)
    assert ledger.addressal_rate(skill_id="a11y") == 1.0
    assert ledger.addressal_rate(skill_id="other") == 0.0


# --- gate -----------------------------------------------------------------------------


def _png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (200, 200, 200)).save(buf, format="PNG")
    return buf.getvalue()


def _artifact_ref(store, kind=ArtifactKind.XCRESULT):
    content = _png_bytes() if kind is ArtifactKind.SCREENSHOT else b"bundle"
    return store.put(content, kind=kind, produced_by="xcodebuild", context={"device": "SE"})


def _proposed(store, **over) -> ProposedFinding:
    kw = {
        "rule_id": "accessibility-audit",
        "anchor": Anchor(file="CartView.swift", line=53),
        "enclosing_symbol": "CartView.body",
        "severity": "high",
        "claim": "The help button has no accessibility label.",
        "evidence": [_artifact_ref(store)],
        "oracle_key": "sufficientElementDescription:cart.help",
    }  # fmt: skip
    kw.update(over)
    return ProposedFinding(**kw)


def test_oracle_confirms_a_finding_it_independently_reported(tmp_path):
    store = FilesystemEvidenceStore(tmp_path, run_id="r")
    verifier = StructuredOracleVerifier(frozenset({"sufficientElementDescription:cart.help"}))
    result = run_gate([_proposed(store)], verifier, skill_id="accessibility-audit")
    assert len(result.verified) == 1
    assert result.verified[0].verified_by == "structured_oracle"


def test_invented_finding_dies_at_the_gate(tmp_path):
    """A plausible claim the oracle never reported must not survive."""
    store = FilesystemEvidenceStore(tmp_path, run_id="r")
    verifier = StructuredOracleVerifier(frozenset({"hitRegion:cart.removeItem"}))
    result = run_gate([_proposed(store)], verifier, skill_id="a")
    assert result.verified == ()
    assert result.counts == {SuppressionReason.NOT_CONFIRMED: 1}


def test_missing_required_evidence_is_rejected_not_trusted(tmp_path):
    store = FilesystemEvidenceStore(tmp_path, run_id="r")
    finding = _proposed(store, evidence=[_artifact_ref(store, ArtifactKind.SCREENSHOT)])
    verifier = StructuredOracleVerifier(frozenset({"sufficientElementDescription:cart.help"}))
    result = run_gate([finding], verifier, skill_id="a")
    assert result.counts == {SuppressionReason.MISSING_EVIDENCE: 1}


def test_finding_without_an_oracle_key_cannot_pass(tmp_path):
    store = FilesystemEvidenceStore(tmp_path, run_id="r")
    verifier = StructuredOracleVerifier(frozenset({"anything"}))
    result = run_gate([_proposed(store, oracle_key=None)], verifier, skill_id="a")
    assert result.verified == ()


def test_unanchorable_findings_are_dropped_and_counted(tmp_path):
    """OQ-FIXTURE-1's current policy, measured rather than assumed."""
    store = FilesystemEvidenceStore(tmp_path, run_id="r")
    finding = _proposed(store, enclosing_symbol="")
    verifier = StructuredOracleVerifier(frozenset({"sufficientElementDescription:cart.help"}))
    result = run_gate([finding], verifier, skill_id="a")
    assert result.counts == {SuppressionReason.UNANCHORABLE: 1}


def test_no_verifier_means_no_post(tmp_path):
    store = FilesystemEvidenceStore(tmp_path, run_id="r")
    result = run_gate([_proposed(store)], None, skill_id="a")
    assert result.counts == {SuppressionReason.NO_VERIFIER: 1}


def _verified(store, **over) -> VerifiedFinding:
    p = _proposed(store, **over)
    return VerifiedFinding(
        _GATE_TOKEN,
        proposed=p,
        verdict=Verdict(verifier="structured_oracle", confirmed=True, reason="ok"),
        verified_by="structured_oracle",
    )


# --- rendering ------------------------------------------------------------------------


def test_comment_carries_claim_evidence_and_suggestion(tmp_path):
    store = FilesystemEvidenceStore(tmp_path, run_id="r")
    body = render_comment(_verified(store, suggestion='.accessibilityLabel("Help")'))
    assert "CartView.swift:53" in body
    assert "Evidence:" in body
    assert "```suggestion" in body
    assert 'accessibilityLabel("Help")' in body


def test_comment_stays_near_the_measured_useful_length(tmp_path):
    store = FilesystemEvidenceStore(tmp_path, run_id="r")
    body = render_comment(_verified(store, suggestion='.accessibilityLabel("Help")'))
    assert len(body) < TARGET_CHARS


def test_summary_hides_claims_but_shows_counts():
    body = render_summary(
        posted=1, suppressed={"unanchorable": 3}, skipped_notes=["runtime tier: skipped (fork PR)"]
    )
    assert "1 comment posted" in body
    assert "3 unanchorable" in body
    assert "fork PR" in body


# --- forge: positioning ---------------------------------------------------------------

DIFF = (
    "diff --git a/CartView.swift b/CartView.swift\n--- a/CartView.swift\n+++ b/CartView.swift\n"
    "@@ -50,3 +50,5 @@\n ctx\n+added a\n+added b\n ctx\n"
)


def test_payload_never_sends_the_deprecated_position_field(tmp_path):
    store = FilesystemEvidenceStore(tmp_path, run_id="r")
    payload = comment_payload(_verified(store))
    assert "position" not in payload
    assert payload["line"] == 53 and payload["side"] == "RIGHT"


def test_multiline_payload_sends_start_side_too(tmp_path):
    """P8 caught this gap in our own schema."""
    store = FilesystemEvidenceStore(tmp_path, run_id="r")
    anchor = Anchor(file="CartView.swift", line=53, start_line=51, start_side=Side.RIGHT)
    payload = comment_payload(_verified(store, anchor=anchor))
    assert payload["start_line"] == 51
    assert payload["start_side"] == "RIGHT"


def test_anchor_on_a_commentable_line_is_accepted(tmp_path):
    store = FilesystemEvidenceStore(tmp_path, run_id="r")
    validate_anchor(parse_diff(DIFF), _verified(store))  # line 53 is added


def test_anchor_outside_a_hunk_is_refused_before_any_network_call(tmp_path):
    store = FilesystemEvidenceStore(tmp_path, run_id="r")
    finding = _verified(store, anchor=Anchor(file="CartView.swift", line=900))
    with pytest.raises(AnchorRejected, match="not in a diff hunk"):
        validate_anchor(parse_diff(DIFF), finding)


def test_anchor_on_an_unchanged_file_is_refused(tmp_path):
    store = FilesystemEvidenceStore(tmp_path, run_id="r")
    finding = _verified(store, anchor=Anchor(file="Other.swift", line=1))
    with pytest.raises(AnchorRejected, match="not in this diff"):
        validate_anchor(parse_diff(DIFF), finding)


def test_left_side_anchor_must_be_a_removed_line(tmp_path):
    store = FilesystemEvidenceStore(tmp_path, run_id="r")
    finding = _verified(store, anchor=Anchor(file="CartView.swift", line=53, side=Side.LEFT))
    with pytest.raises(AnchorRejected):
        validate_anchor(parse_diff(DIFF), finding)


def test_get_pull_request_detects_a_fork():
    payload = {
        "head": {"sha": "h", "ref": "feature", "repo": {"full_name": "someone/fork"}},
        "base": {"sha": "b", "ref": "main"},
        "labels": [{"name": "sightline:full"}],
        "title": "T",
    }
    adapter = GitHubAdapter(run=FakeRunner(stdout=json.dumps(payload)))
    pr = adapter.get_pull_request("owner/repo", 7)
    assert pr.is_fork and pr.labels == ("sightline:full",)


def test_dry_run_posts_nothing(tmp_path):
    store = FilesystemEvidenceStore(tmp_path, run_id="r")
    runner = FakeRunner(stdout="{}")
    adapter = GitHubAdapter(run=runner)
    pr = PullRequest(repo="o/r", number=1, base_sha="b", head_sha="h")
    assert adapter.post_review(pr, [_verified(store)], summary="s", dry_run=True) == []
    assert runner.calls == []


def test_undecodable_image_is_refused_not_stored(tmp_path):
    """A truncated capture must not crash the run, and must not be stored unmasked."""
    store = FilesystemEvidenceStore(tmp_path, run_id="r")
    with pytest.raises(RedactionError, match="refusing to store it unmasked"):
        store.put(b"not a png", kind=ArtifactKind.SCREENSHOT, produced_by="t", context={})
    assert not list(tmp_path.rglob("*.png"))


# --- positioning gaps found by cross-checking real accepted comments -----------------

LEFT_DIFF = (
    "diff --git a/A.swift b/A.swift\n--- a/A.swift\n+++ b/A.swift\n"
    "@@ -594,4 +594,4 @@\n ctx594\n ctx595\n-removed596\n+added596\n ctx597\n"
)


def test_left_side_comment_is_allowed_on_a_context_line(tmp_path):
    """Found by cross-checking astral-sh/ruff#28200: GitHub accepts a LEFT comment on
    unchanged context, not only on removed lines. Our first validator refused it."""
    store = FilesystemEvidenceStore(tmp_path, run_id="r")
    f = parse_diff(LEFT_DIFF).files[0]
    assert 597 in f.commentable_left_lines
    finding = _verified(store, anchor=Anchor(file="A.swift", line=597, side=Side.LEFT))
    validate_anchor(parse_diff(LEFT_DIFF), finding)


def test_left_side_still_refuses_a_line_outside_the_hunk(tmp_path):
    store = FilesystemEvidenceStore(tmp_path, run_id="r")
    finding = _verified(store, anchor=Anchor(file="A.swift", line=5000, side=Side.LEFT))
    with pytest.raises(AnchorRejected):
        validate_anchor(parse_diff(LEFT_DIFF), finding)


def test_file_level_anchor_needs_no_line(tmp_path):
    """The honest option for an unanchorable audit issue (OQ-FIXTURE-1)."""
    store = FilesystemEvidenceStore(tmp_path, run_id="r")
    anchor = Anchor(file="CartView.swift", line=0, file_level=True)
    finding = _verified(store, anchor=anchor)
    validate_anchor(parse_diff(DIFF), finding)
    payload = comment_payload(finding)
    assert payload["subject_type"] == "file"
    assert "line" not in payload and "side" not in payload
