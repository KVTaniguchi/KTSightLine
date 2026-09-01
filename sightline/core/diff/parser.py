"""Unified diff parser.

Deliberately hand-rolled and narrow: we need exact head/base line numbers for comment
positioning and nothing else. `reviewdog` is the prior art for mapping tool output onto
hunks; this is the input side of that same problem.
"""

from __future__ import annotations

import re

from sightline.core.diff.models import (
    ChangedFile,
    ChangeType,
    Diff,
    DiffLine,
    Hunk,
    LineKind,
)

_HUNK = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@(?P<heading>.*)$"
)
_DIFF_GIT = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+)$")


def _strip_prefix(path: str) -> str:
    """Drop the a/ or b/ prefix git puts on diff paths."""
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


class _FileAccumulator:
    def __init__(self, path: str, old_path: str | None) -> None:
        self.path = path
        self.old_path = old_path
        self.change_type = ChangeType.MODIFIED
        self.hunks: list[Hunk] = []
        self._hunk: dict | None = None
        self._lines: list[DiffLine] = []
        self._old = 0
        self._new = 0

    def start_hunk(self, m: re.Match[str]) -> None:
        self.close_hunk()
        self._hunk = {
            "old_start": int(m["old_start"]),
            "old_count": int(m["old_count"] or 1),
            "new_start": int(m["new_start"]),
            "new_count": int(m["new_count"] or 1),
            "heading": m["heading"].strip(),
        }
        self._old = self._hunk["old_start"]
        self._new = self._hunk["new_start"]
        self._lines = []

    def add_line(self, raw: str) -> None:
        if self._hunk is None:
            return
        marker, text = (raw[0], raw[1:]) if raw else (" ", "")
        if marker == "+":
            self._lines.append(DiffLine(kind=LineKind.ADDED, text=text, new_line=self._new))
            self._new += 1
        elif marker == "-":
            self._lines.append(DiffLine(kind=LineKind.REMOVED, text=text, old_line=self._old))
            self._old += 1
        else:  # context (" ") — an empty line in a diff is a bare "" and is context
            self._lines.append(
                DiffLine(kind=LineKind.CONTEXT, text=text, old_line=self._old, new_line=self._new)
            )
            self._old += 1
            self._new += 1

    def close_hunk(self) -> None:
        if self._hunk is not None:
            self.hunks.append(Hunk(**self._hunk, lines=self._lines))
            self._hunk = None
            self._lines = []

    def build(self) -> ChangedFile:
        self.close_hunk()
        return ChangedFile(
            path=self.path,
            old_path=self.old_path,
            change_type=self.change_type,
            hunks=self.hunks,
        )


def parse_diff(text: str) -> Diff:
    """Parse a unified diff (``git diff`` / GitHub ``.diff``) into a Diff."""
    files: list[ChangedFile] = []
    current: _FileAccumulator | None = None

    for raw in text.splitlines():
        if m := _DIFF_GIT.match(raw):
            if current is not None:
                files.append(current.build())
            current = _FileAccumulator(_strip_prefix(m["b"]), None)
            continue

        if current is None:
            continue

        if raw.startswith("new file mode"):
            current.change_type = ChangeType.ADDED
        elif raw.startswith("deleted file mode"):
            current.change_type = ChangeType.DELETED
        elif raw.startswith("rename from "):
            current.old_path = raw[len("rename from ") :].strip()
            current.change_type = ChangeType.RENAMED
        elif raw.startswith("rename to "):
            current.path = raw[len("rename to ") :].strip()
        elif raw.startswith("--- "):
            src = raw[4:].strip()
            if src != "/dev/null" and current.old_path is None:
                current.old_path = _strip_prefix(src)
        elif raw.startswith("+++ "):
            dst = raw[4:].strip()
            if dst == "/dev/null":
                current.change_type = ChangeType.DELETED
            elif current.change_type is not ChangeType.RENAMED:
                current.path = _strip_prefix(dst)
        elif m := _HUNK.match(raw):
            current.start_hunk(m)
        elif raw.startswith("\\"):
            continue  # "\ No newline at end of file"
        elif raw.startswith(("index ", "similarity index", "old mode", "new mode", "Binary ")):
            continue
        else:
            current.add_line(raw)

    if current is not None:
        files.append(current.build())
    return Diff(files=files)
