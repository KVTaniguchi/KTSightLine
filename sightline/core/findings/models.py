"""The Finding contract.

Two types, deliberately. ``ProposedFinding`` is what a skill produces; ``VerifiedFinding``
is what a ForgeAdapter accepts. Only the falsifiability gate can build the second one.
The brief's single ``Finding`` with ``verified_by: str | None`` is the bug this splits:
optionality in the postable type means someone eventually posts ``None``.

See docs/adr/0002-evidence-and-verification.md.
"""

from __future__ import annotations

import hashlib
import re
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sightline.core.evidence.models import ArtifactRef

Severity = Literal["blocking", "high", "medium", "low"]

_GATE_TOKEN = object()
"""Private capability token. ``core.verify.gate`` holds the only reference."""


class Side(StrEnum):
    """Which side of the diff a comment anchors to."""

    LEFT = "LEFT"
    RIGHT = "RIGHT"


class Anchor(BaseModel):
    """Where the comment lands. Positioning data only — never part of the fingerprint."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    file: str
    line: int
    side: Side = Side.RIGHT
    # Multi-line comments require BOTH start_line and start_side (verified against the
    # GitHub REST docs 2026-08-31, P8). `position` is deprecated and is never sent.
    start_line: int | None = None
    start_side: Side | None = None

    @model_validator(mode="after")
    def _multiline_is_complete(self) -> Anchor:
        if self.start_line is not None:
            if self.start_line > self.line:
                raise ValueError("start_line must be <= line")
            if self.start_side is None:
                raise ValueError("start_side is required whenever start_line is set")
        elif self.start_side is not None:
            raise ValueError("start_side is meaningless without start_line")
        return self


_DIGITS = re.compile(r"\d+")
_QUOTED = re.compile(r"[\"'“‘].*?[\"'”’]")
_PATHISH = re.compile(r"\S+\.(swift|xib|storyboard|plist|xcprivacy|m|h)\b")
_WS = re.compile(r"\s+")


def normalize_claim(claim: str) -> str:
    """Strip everything that legitimately varies push-to-push.

    Digits and quoted literals go because "truncates to 'Estimated de…'" and
    "truncates to 'Estimated deliv…'" are the same finding and must hash identically.
    """
    text = claim.lower()
    text = _QUOTED.sub("<q>", text)
    text = _PATHISH.sub("<path>", text)
    text = _DIGITS.sub("<n>", text)
    return _WS.sub(" ", text).strip()


def fingerprint(rule_id: str, path: str, enclosing_symbol: str, claim: str) -> str:
    """Content-derived identity. Deliberately excludes line numbers.

    ``enclosing_symbol`` comes from a structural Swift parse. On parse failure the caller
    passes ``"<file>"`` — a coarser fingerprint over-dedupes, which is the safe direction.
    """
    parts = (rule_id, path, enclosing_symbol, normalize_claim(claim))
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


class ProposedFinding(BaseModel):
    """What a skill emits. Carries evidence, but nothing has checked the claim yet."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rule_id: str
    anchor: Anchor
    enclosing_symbol: str
    severity: Severity
    claim: str = Field(max_length=280, description="One sentence. No hedging.")
    detail: str | None = Field(default=None, max_length=600)
    evidence: Annotated[list[ArtifactRef], Field(min_length=1)]
    suggestion: str | None = None  # verbatim replacement code; rendered as ```suggestion
    owners: list[str] = Field(default_factory=list)

    @property
    def fingerprint(self) -> str:
        return fingerprint(
            self.rule_id, self.anchor.file, self.enclosing_symbol, self.claim
        )


class Verdict(BaseModel):
    """A verifier's answer. Adversarial by construction: ambiguity resolves to reject."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verifier: str
    confirmed: bool
    reason: str
    supporting_evidence: list[ArtifactRef] = Field(default_factory=list)


class VerifiedFinding(BaseModel):
    """The only thing a ForgeAdapter will accept.

    Constructible only via ``core.verify.gate``. The ``_token`` guard is not a security
    boundary — it is a speed bump loud enough that nobody bypasses it by accident, and
    grep-able enough that a reviewer sees it if they do.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    proposed: ProposedFinding
    verdict: Verdict
    verified_by: str

    def __init__(self, /, _token: Any = None, **data: Any) -> None:
        if _token is not _GATE_TOKEN:
            raise TypeError(
                "VerifiedFinding is constructible only by sightline.core.verify.gate. "
                "A finding reaches the forge by passing verification, not by being built."
            )
        super().__init__(**data)

    @property
    def fingerprint(self) -> str:
        return self.proposed.fingerprint
