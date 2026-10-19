"""A forge that is just a local git repository: two refs stand in for a pull request.

For reviewing a branch before a PR exists, and for running against an app whose PRs we
are not authorized to comment on. The engine cannot tell it apart from GitHub, which is
the point of the `ForgeAdapter` boundary — and it cannot post, which is the point of
this adapter.

The diff is `git diff <merge-base> <head>`, the same three-dot shape GitHub serves for a
PR, so anchors validated here are anchors GitHub would accept.
"""

from __future__ import annotations

import io
import subprocess
import tarfile
from collections.abc import Callable, Sequence
from pathlib import Path

from sightline.adapters.forge.base import ForgeAdapter, ForgeError, PostedComment, PullRequest
from sightline.core.diff.models import Diff
from sightline.core.diff.parser import parse_diff
from sightline.core.findings.models import VerifiedFinding

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


def _default_runner(cmd: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), capture_output=True, check=False)


class LocalGitAdapter(ForgeAdapter):
    def __init__(self, repo_path: Path, base: str, head: str, *, run: Runner = _default_runner):
        self.repo_path = Path(repo_path)
        self.run = run
        self.base_ref, self.head_ref = base, head
        self.head_sha = self._rev_parse(head)
        self.base_sha = self._git("merge-base", self._rev_parse(base), self.head_sha).strip()

    def _git_bytes(self, *args: str) -> bytes:
        proc = self.run(["git", "-C", str(self.repo_path), *args])
        if proc.returncode != 0:
            stderr = proc.stderr.decode() if isinstance(proc.stderr, bytes) else proc.stderr
            raise ForgeError(f"git {' '.join(args)} failed: {(stderr or '').strip()}")
        return proc.stdout if isinstance(proc.stdout, bytes) else proc.stdout.encode()

    def _git(self, *args: str) -> str:
        return self._git_bytes(*args).decode("utf-8")

    def _rev_parse(self, ref: str) -> str:
        return self._git("rev-parse", "--verify", f"{ref}^{{commit}}").strip()

    @property
    def name(self) -> str:
        return self.repo_path.resolve().name

    def get_pull_request(self, repo: str, number: int) -> PullRequest:
        return PullRequest(
            repo=repo,
            number=number,
            base_sha=self.base_sha,
            head_sha=self.head_sha,
            base_ref=self.base_ref,
            head_ref=self.head_ref,
            title=f"{self.base_ref}...{self.head_ref} (local)",
        )

    def get_diff(self, repo: str, number: int) -> Diff:
        return parse_diff(
            self._git("diff", "--no-color", "--no-ext-diff", "-M", self.base_sha, self.head_sha)
        )

    def get_file(self, repo: str, path: str, ref: str) -> str | None:
        try:
            return self._git("show", f"{ref}:{path}")
        except ForgeError:
            return None

    def post_review(
        self,
        pr: PullRequest,
        findings: list[VerifiedFinding],
        *,
        summary: str,
        dry_run: bool = False,
    ) -> list[PostedComment]:
        raise ForgeError("a local git review has nowhere to post; run without --post")

    def existing_comment_bodies(self, repo: str, number: int) -> list[str]:
        return []

    def materialize(self, dest: Path) -> Path:
        """Write the head tree to ``dest`` without touching the repo's working tree.

        `git archive`, not a checkout or a worktree: the user's branch, index, and
        `.git` stay exactly as they were, and uncommitted edits cannot leak into a run.
        """
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        archive = self._git_bytes("archive", "--format=tar", self.head_sha)
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(dest, filter="data")
        return dest
