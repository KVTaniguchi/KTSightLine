"""Artifact model for the evidence store.

An artifact is immutable and content-addressed. Anything "derived" from an artifact
(a render diff, a cropped region) is a *new* artifact whose ``context`` names its
parents. See docs/adr/0002-evidence-and-verification.md.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class ArtifactKind(StrEnum):
    """Closed set. Adding a kind means adding a producer and a verifier that consumes it."""

    SCREENSHOT = "screenshot"
    SCREEN_RECORDING = "screen_recording"
    CONSOLE_LOG = "console_log"
    XCRESULT = "xcresult"
    CRASH_REPORT = "crash_report"
    INSTRUMENTS_TRACE = "instruments_trace"
    METRIC_SERIES = "metric_series"
    BUILD_LOG = "build_log"
    SOURCE_SPAN = "source_span"
    STATIC_ANALYSIS_REPORT = "static_analysis_report"
    RENDER_DIFF = "render_diff"


class ArtifactRef(BaseModel):
    """A reference to one stored artifact.

    ``context`` is mandatory and is what makes a finding reproducible by a human:
    device, os, appearance, content_size, locale, branch, commit. It is rendered into
    the comment footer, so it must be readable after the artifact link expires.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    kind: ArtifactKind
    uri: str  # sightline://evidence/<sha256>; the store resolves to CI artifact URLs
    produced_by: str  # capability id, e.g. "simulator.capture.screenshot"
    run_id: str
    context: dict[str, str]
    bytes: int
    created_at: datetime

    @classmethod
    def for_content(
        cls,
        content: bytes,
        *,
        kind: ArtifactKind,
        produced_by: str,
        run_id: str,
        context: dict[str, str],
        created_at: datetime,
    ) -> ArtifactRef:
        digest = hashlib.sha256(content).hexdigest()
        return cls(
            sha256=digest,
            kind=kind,
            uri=f"sightline://evidence/{digest}",
            produced_by=produced_by,
            run_id=run_id,
            context=context,
            bytes=len(content),
            created_at=created_at,
        )
