"""The forge-agnostic interface.

Brief rule 5 from the Uber list: GitHub first, but the review engine never imports a
GitHub type. Note the signature of :meth:`post_review` — it accepts ``VerifiedFinding``
and nothing else, so the ADR-0002 gate is enforced by the type system at the boundary
rather than by a convention someone eventually forgets.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from sightline.core.diff.models import Diff
from sightline.core.findings.models import VerifiedFinding


@dataclass(frozen=True)
class PullRequest:
    repo: str  # "owner/name"
    number: int
    base_sha: str
    head_sha: str
    base_ref: str = ""
    head_ref: str = ""
    is_fork: bool = False
    labels: tuple[str, ...] = field(default_factory=tuple)
    title: str = ""


@dataclass(frozen=True)
class PostedComment:
    fingerprint: str
    comment_id: str
    url: str = ""


class ForgeError(RuntimeError):
    pass


class AnchorRejected(ForgeError):
    """The anchor is not on a line the forge will accept a comment on.

    Raised *before* any network call. Getting positioning wrong puts every comment on
    the wrong line, which ADR-0003 calls instantly disqualifying — so we refuse locally
    rather than letting the forge silently reinterpret us.
    """


class ForgeAdapter(ABC):
    @abstractmethod
    def get_pull_request(self, repo: str, number: int) -> PullRequest: ...

    @abstractmethod
    def get_diff(self, repo: str, number: int) -> Diff: ...

    @abstractmethod
    def post_review(
        self,
        pr: PullRequest,
        findings: list[VerifiedFinding],
        *,
        summary: str,
        dry_run: bool = False,
    ) -> list[PostedComment]: ...

    @abstractmethod
    def existing_comment_bodies(self, repo: str, number: int) -> list[str]: ...

    @abstractmethod
    def get_file(self, repo: str, path: str, ref: str) -> str | None:
        """Head-side contents of one file, or None if it is gone at that ref.

        Impact analysis needs this: the common case is a diff that touches only lines
        inside an existing view's body, which matches no declaration in the added text.
        Without the file we cannot tell that the enclosing type is a View, and every
        runtime skill silently fails to fire.
        """
