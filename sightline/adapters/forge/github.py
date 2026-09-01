"""GitHub implementation of :class:`ForgeAdapter`.

Positioning is the whole risk here. Verified against the REST docs 2026-08-31 (P8):

* ``line`` + ``side`` for a single-line comment. ``side`` is ``LEFT`` for deletions
  (red) and ``RIGHT`` for additions (green) or unchanged context (white).
* ``start_line`` **and** ``start_side`` for a multi-line comment. Both, not just the
  first — that gap was in our own schema until P8 caught it.
* ``position`` is deprecated ("closing down. Use line instead"). It is not an older API
  to fall back to; it is a thing we never send.

We validate the anchor against the parsed diff *before* calling the API, because a
comment silently landing on the wrong line is worse than a refusal.

Auth goes through the ``gh`` CLI, which resolves a keyring login locally and
``GH_TOKEN``/``GITHUB_TOKEN`` in Actions with the same code path.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sightline.adapters.forge.base import (
    AnchorRejected,
    ForgeAdapter,
    ForgeError,
    PostedComment,
    PullRequest,
)
from sightline.core.diff.models import Diff
from sightline.core.diff.parser import parse_diff
from sightline.core.findings.models import Side, VerifiedFinding
from sightline.core.findings.render import render_comment

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


def _default_runner(cmd: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), capture_output=True, text=True, check=False)


def validate_anchor(diff: Diff, finding: VerifiedFinding) -> None:
    """Refuse an anchor the forge would misplace. Raises :class:`AnchorRejected`."""
    anchor = finding.proposed.anchor
    changed = diff.by_path(anchor.file)
    if changed is None:
        raise AnchorRejected(
            f"{anchor.file} is not in this diff; GitHub only accepts comments on changed files"
        )
    if anchor.file_level:
        return  # subject_type: file is valid without a line
    allowed = (
        changed.commentable_lines if anchor.side is Side.RIGHT else changed.commentable_left_lines
    )
    if anchor.line not in allowed:
        raise AnchorRejected(
            f"{anchor.file}:{anchor.line} ({anchor.side}) is not in a diff hunk. "
            f"Commentable {anchor.side} lines: {sorted(allowed)[:12]}"
            + ("…" if len(allowed) > 12 else "")
        )
    if anchor.start_line is not None and anchor.start_line not in allowed:
        raise AnchorRejected(
            f"start_line {anchor.start_line} is not in a diff hunk for {anchor.file}"
        )


def comment_payload(finding: VerifiedFinding, *, evidence_base_url: str | None = None) -> dict:
    """Build the review-comment object. `position` is deliberately never present."""
    anchor = finding.proposed.anchor
    payload: dict[str, Any] = {
        "path": anchor.file,
        "body": render_comment(finding, evidence_base_url=evidence_base_url),
    }
    if anchor.file_level:
        payload["subject_type"] = "file"
        return payload
    payload["line"] = anchor.line
    payload["side"] = anchor.side.value
    if anchor.start_line is not None:
        payload["start_line"] = anchor.start_line
        payload["start_side"] = (anchor.start_side or anchor.side).value
    return payload


@dataclass
class GitHubAdapter(ForgeAdapter):
    run: Runner = _default_runner
    evidence_base_url: str | None = None
    extra_headers: tuple[str, ...] = field(default_factory=tuple)

    # --- plumbing ---

    def _gh(self, *args: str, raw: bool = False) -> Any:
        proc = self.run(["gh", *args])
        if proc.returncode != 0:
            raise ForgeError(f"gh {' '.join(args)} failed: {(proc.stderr or '').strip()}")
        out = proc.stdout or ""
        return out if raw else json.loads(out or "null")

    def _api(self, path: str, *, method: str = "GET") -> Any:
        args = ["api", "-X", method, path]
        for header in self.extra_headers:
            args += ["-H", header]
        return self._gh(*args)

    def _post_with_input(self, path: str, body: dict) -> Any:
        proc = subprocess.run(
            ["gh", "api", "-X", "POST", path, "--input", "-"],
            input=json.dumps(body),
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise ForgeError(f"gh api POST {path} failed: {(proc.stderr or '').strip()}")
        return json.loads(proc.stdout or "null")

    # --- reads ---

    def get_pull_request(self, repo: str, number: int) -> PullRequest:
        data = self._api(f"repos/{repo}/pulls/{number}")
        head, base = data["head"], data["base"]
        return PullRequest(
            repo=repo,
            number=number,
            base_sha=base["sha"],
            head_sha=head["sha"],
            base_ref=base.get("ref", ""),
            head_ref=head.get("ref", ""),
            is_fork=head.get("repo", {}).get("full_name") != repo,
            labels=tuple(label["name"] for label in data.get("labels", [])),
            title=data.get("title", ""),
        )

    def get_diff(self, repo: str, number: int) -> Diff:
        text = self._gh(
            "api", f"repos/{repo}/pulls/{number}", "-H", "Accept: application/vnd.github.diff",
            raw=True,
        )  # fmt: skip
        return parse_diff(text)

    def existing_comment_bodies(self, repo: str, number: int) -> list[str]:
        data = self._api(f"repos/{repo}/pulls/{number}/comments?per_page=100") or []
        return [c.get("body", "") for c in data]

    # --- writes ---

    def post_review(
        self,
        pr: PullRequest,
        findings: list[VerifiedFinding],
        *,
        summary: str,
        dry_run: bool = False,
    ) -> list[PostedComment]:
        """Post one review carrying every comment.

        A single review rather than N standalone comments: it produces one notification
        instead of N, and the whole batch lands or none of it does.

        ``event`` is always ``COMMENT``. ADR-0003 §7: nothing we post ever blocks a
        merge, so we never REQUEST_CHANGES.
        """
        diff = self.get_diff(pr.repo, pr.number) if not dry_run else None
        comments = []
        for finding in findings:
            if diff is not None:
                validate_anchor(diff, finding)
            comments.append(comment_payload(finding, evidence_base_url=self.evidence_base_url))

        body = {
            "commit_id": pr.head_sha,
            "body": summary,
            "event": "COMMENT",
            "comments": comments,
        }
        if dry_run:
            return []

        result = self._post_with_input(f"repos/{pr.repo}/pulls/{pr.number}/reviews", body)
        review_id = str(result.get("id", ""))
        posted = self._api(f"repos/{pr.repo}/pulls/{pr.number}/comments?per_page=100") or []
        by_line = {(c["path"], c.get("line")): c for c in posted}
        out = []
        for finding in findings:
            anchor = finding.proposed.anchor
            match = by_line.get((anchor.file, anchor.line))
            out.append(
                PostedComment(
                    fingerprint=finding.fingerprint,
                    comment_id=str(match["id"]) if match else review_id,
                    url=match.get("html_url", "") if match else "",
                )
            )
        return out
