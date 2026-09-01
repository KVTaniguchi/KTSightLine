"""Parsed unified-diff model.

This is the deterministic layer's foundation. It never calls a model, and it owns the
answer to "can a comment land here?" — getting that wrong puts every comment on the
wrong line, which ADR-0003 calls instantly disqualifying.
"""

from __future__ import annotations

from collections.abc import Iterator
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ChangeType(StrEnum):
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"


class LineKind(StrEnum):
    CONTEXT = "context"
    ADDED = "added"
    REMOVED = "removed"


class DiffLine(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: LineKind
    text: str
    old_line: int | None = None  # 1-indexed line in the base file
    new_line: int | None = None  # 1-indexed line in the head file


class Hunk(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    old_start: int
    old_count: int
    new_start: int
    new_count: int
    heading: str = ""
    lines: list[DiffLine] = Field(default_factory=list)


class ChangedFile(BaseModel):
    """One file's worth of diff.

    ``path`` is always the head-side path. For a deletion, head-side does not exist and
    ``path`` is the base path — callers must check ``change_type`` before assuming the
    file is on disk.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    old_path: str | None = None
    change_type: ChangeType = ChangeType.MODIFIED
    hunks: list[Hunk] = Field(default_factory=list)

    @property
    def added_lines(self) -> set[int]:
        """Head-side line numbers of added lines. Commentable on side=RIGHT."""
        return {
            line.new_line
            for hunk in self.hunks
            for line in hunk.lines
            if line.kind is LineKind.ADDED and line.new_line is not None
        }

    @property
    def removed_lines(self) -> set[int]:
        """Base-side line numbers of removed lines. Commentable on side=LEFT."""
        return {
            line.old_line
            for hunk in self.hunks
            for line in hunk.lines
            if line.kind is LineKind.REMOVED and line.old_line is not None
        }

    @property
    def context_lines(self) -> set[int]:
        """Head-side line numbers of unchanged context inside a hunk.

        GitHub accepts a comment on these with side=RIGHT — they appear in the diff as
        white rows. Keeping them separate from added_lines matters: a finding on
        context is real (the diff made it wrong) but is weaker evidence than one on an
        added line, and skills may want to say so.
        """
        return {
            line.new_line
            for hunk in self.hunks
            for line in hunk.lines
            if line.kind is LineKind.CONTEXT and line.new_line is not None
        }

    @property
    def commentable_lines(self) -> set[int]:
        """Every head-side line a RIGHT-side comment may anchor to."""
        return self.added_lines | self.context_lines

    def added_text(self) -> Iterator[tuple[int, str]]:
        """(head line number, text) for added lines only.

        Impact analysis reads *added* text, never whole files: a `try!` that was already
        there is not this PR's problem.
        """
        for hunk in self.hunks:
            for line in hunk.lines:
                if line.kind is LineKind.ADDED and line.new_line is not None:
                    yield line.new_line, line.text


class Diff(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    files: list[ChangedFile] = Field(default_factory=list)

    def by_path(self, path: str) -> ChangedFile | None:
        return next((f for f in self.files if f.path == path), None)

    @property
    def paths(self) -> list[str]:
        return [f.path for f in self.files]
