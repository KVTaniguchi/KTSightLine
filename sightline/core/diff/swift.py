"""Swift declaration outline.

A brace-tracking scanner, not a parser. It exists to answer one question:
"what declaration encloses line N?" — because ADR-0002 hashes the enclosing symbol into
the fingerprint, and line numbers move on every push.

On anything it cannot resolve it returns ``UNRESOLVED`` (``"<file>"``). ADR-0002 §4 is
explicit that a coarser fingerprint over-dedupes, which is the safe direction: we would
rather post one comment for two nearby issues than post the same comment three times.

Replacing this with a real parse (SwiftSyntax via a helper binary, or an index store)
is tracked as future work; the interface is the part that should survive.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

UNRESOLVED = "<file>"

_TYPE_DECL = re.compile(
    r"^\s*(?:@\w+(?:\([^)]*\))?\s+)*"
    r"(?:public\s+|private\s+|internal\s+|fileprivate\s+|open\s+|final\s+|indirect\s+)*"
    r"(?P<kind>struct|class|enum|actor|protocol|extension)\s+"
    r"(?P<name>[A-Za-z_]\w*)"
)
_MEMBER_DECL = re.compile(
    r"^\s*(?:@\w+(?:\([^)]*\))?\s+)*"
    r"(?:public\s+|private\s+|internal\s+|fileprivate\s+|open\s+|final\s+|static\s+|class\s+|"
    r"override\s+|mutating\s+|nonisolated\s+|convenience\s+|required\s+)*"
    r"(?:(?P<fkind>func|init|deinit|subscript|var|let)\s*)"
    r"(?P<name>[A-Za-z_]\w*)?"
)
_LINE_COMMENT = re.compile(r"//.*$")
_STRING = re.compile(r'"(?:[^"\\]|\\.)*"')


@dataclass(frozen=True)
class _Scope:
    name: str
    depth: int


def _sanitize(line: str) -> str:
    """Blank out strings and line comments so their braces don't move the depth."""
    return _LINE_COMMENT.sub("", _STRING.sub('""', line))


class SwiftOutline:
    """Maps 1-indexed head-side line numbers to a dotted declaration path."""

    def __init__(self, source: str) -> None:
        self._by_line: dict[int, str] = {}
        self._build(source)

    def _build(self, source: str) -> None:
        stack: list[_Scope] = []
        depth = 0
        in_block_comment = False

        for number, raw in enumerate(source.splitlines(), start=1):
            line = raw
            if in_block_comment:
                if "*/" in line:
                    line = line.split("*/", 1)[1]
                    in_block_comment = False
                else:
                    self._by_line[number] = self._path(stack)
                    continue
            if "/*" in line:
                head, _, tail = line.partition("/*")
                if "*/" in tail:
                    line = head + tail.split("*/", 1)[1]
                else:
                    line = head
                    in_block_comment = True

            clean = _sanitize(line)

            # Record the scope this line *belongs to* before its own braces apply, so a
            # declaration line maps to its parent and its body maps to itself.
            name: str | None = None
            if m := _TYPE_DECL.match(clean):
                name = m["name"]
            elif m := _MEMBER_DECL.match(clean):
                name = m["name"] or (m["fkind"] if m["fkind"] in {"init", "deinit"} else None)

            opens = clean.count("{")
            closes = clean.count("}")

            if name is not None and opens > 0:
                # A declaration line is attributed to itself: `var body: some View {`
                # is part of `body`, not of the enclosing type.
                stack.append(_Scope(name, depth))
            self._by_line[number] = self._path(stack)

            depth += opens - closes
            while stack and depth <= stack[-1].depth:
                stack.pop()

    @staticmethod
    def _path(stack: list[_Scope]) -> str:
        return ".".join(s.name for s in stack) if stack else UNRESOLVED

    def enclosing_symbol(self, line: int) -> str:
        """The dotted declaration path containing ``line``, or ``UNRESOLVED``."""
        return self._by_line.get(line, UNRESOLVED)

    @property
    def symbols(self) -> set[str]:
        return {s for s in self._by_line.values() if s != UNRESOLVED}
