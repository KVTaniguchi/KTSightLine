"""Content-addressed evidence store.

ADR-0002 §1. Two properties do the work:

* **Content-addressed.** The same screenshot captured by two skills stores once and both
  reference it, so dedupe of evidence falls out for free.
* **Immutable.** Nothing is annotated after the fact. A derived artifact (a render diff)
  is a *new* artifact whose ``context`` names its parents.

The interface exists so this can become S3-backed without touching callers; the
filesystem implementation is what v1 ships (artifacts are uploaded as CI job artifacts
alongside the run).
"""

from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path

from sightline.core.evidence.models import ArtifactKind, ArtifactRef
from sightline.core.evidence.redaction import (
    RedactionPolicy,
    redact_image,
    redact_text,
)

_IMAGE_KINDS = {ArtifactKind.SCREENSHOT, ArtifactKind.RENDER_DIFF}
_TEXT_KINDS = {
    ArtifactKind.CONSOLE_LOG,
    ArtifactKind.BUILD_LOG,
    ArtifactKind.CRASH_REPORT,
    ArtifactKind.SOURCE_SPAN,
    ArtifactKind.STATIC_ANALYSIS_REPORT,
    ArtifactKind.METRIC_SERIES,
}

_EXTENSIONS = {
    ArtifactKind.SCREENSHOT: ".png",
    ArtifactKind.RENDER_DIFF: ".png",
    ArtifactKind.SCREEN_RECORDING: ".mp4",
    ArtifactKind.CONSOLE_LOG: ".log",
    ArtifactKind.BUILD_LOG: ".log",
    ArtifactKind.CRASH_REPORT: ".crash",
    ArtifactKind.METRIC_SERIES: ".json",
    ArtifactKind.STATIC_ANALYSIS_REPORT: ".json",
    ArtifactKind.SOURCE_SPAN: ".txt",
    ArtifactKind.XCRESULT: ".xcresult",
    ArtifactKind.INSTRUMENTS_TRACE: ".trace",
}


class EvidenceStore(ABC):
    @abstractmethod
    def put(
        self,
        content: bytes,
        *,
        kind: ArtifactKind,
        produced_by: str,
        context: dict[str, str],
    ) -> ArtifactRef: ...

    @abstractmethod
    def get(self, ref: ArtifactRef) -> bytes: ...

    @abstractmethod
    def exists(self, ref: ArtifactRef) -> bool: ...


class FilesystemEvidenceStore(EvidenceStore):
    def __init__(
        self,
        root: Path,
        *,
        run_id: str,
        policy: RedactionPolicy | None = None,
        now: datetime | None = None,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.policy = policy or RedactionPolicy()
        self._now = now

    def _timestamp(self) -> datetime:
        return self._now or datetime.now(UTC)

    def _path_for(self, sha256: str, kind: ArtifactKind) -> Path:
        # Two-level fan-out: a busy run can produce thousands of screenshots and a flat
        # directory gets unpleasant fast.
        return self.root / sha256[:2] / f"{sha256}{_EXTENSIONS.get(kind, '.bin')}"

    def put(
        self,
        content: bytes,
        *,
        kind: ArtifactKind,
        produced_by: str,
        context: dict[str, str],
    ) -> ArtifactRef:
        """Redact, then address, then write. That order is the whole point."""
        if kind in _IMAGE_KINDS:
            scale = float(context.get("scale", self.policy.scale))
            content = redact_image(content, self.policy.with_scale(scale))
        elif kind in _TEXT_KINDS:
            content = redact_text(content.decode("utf-8", "replace"), self.policy).encode()

        ref = ArtifactRef.for_content(
            content,
            kind=kind,
            produced_by=produced_by,
            run_id=self.run_id,
            context=dict(context),
            created_at=self._timestamp(),
        )
        destination = self._path_for(ref.sha256, kind)
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            tmp = destination.with_suffix(destination.suffix + ".partial")
            tmp.write_bytes(content)
            tmp.replace(destination)  # atomic: a reader never sees a half-written artifact
        return ref

    def put_file(
        self,
        path: Path,
        *,
        kind: ArtifactKind,
        produced_by: str,
        context: dict[str, str],
    ) -> ArtifactRef:
        return self.put(
            Path(path).read_bytes(), kind=kind, produced_by=produced_by, context=context
        )

    def get(self, ref: ArtifactRef) -> bytes:
        return self._path_for(ref.sha256, ref.kind).read_bytes()

    def exists(self, ref: ArtifactRef) -> bool:
        return self._path_for(ref.sha256, ref.kind).exists()

    def local_path(self, ref: ArtifactRef) -> Path:
        return self._path_for(ref.sha256, ref.kind)

    def export(self, dest: Path) -> Path:
        """Copy the store somewhere a CI job can upload it as a run artifact."""
        dest = Path(dest)
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(self.root, dest)
        return dest
