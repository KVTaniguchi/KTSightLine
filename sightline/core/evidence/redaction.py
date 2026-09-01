"""Redaction, applied before an artifact is ever written.

ADR-0002 §1: "An unmasked artifact never touches disk in the store." Screenshots of a
real e-commerce app in CI contain seeded PII, order numbers, and tokens; console logs
contain bearer tokens. Redaction is a step in the write path, not a cleanup pass — a
cleanup pass leaves the unmasked bytes on disk in between.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

from PIL import Image, ImageDraw


class RedactionError(RuntimeError):
    """The artifact could not be decoded, so it cannot be safely redacted.

    Raised rather than swallowed: an artifact we cannot mask must not be stored, and an
    artifact we cannot store means the finding it backs has no evidence and gets
    suppressed by the gate. A truncated capture from a flaky simulator is a normal
    event, not a crash.
    """


MASK_FILL = (17, 17, 17)
"""Opaque, deterministic, and not black — a solid box that a render diff will see
identically on both branches instead of blending with dark-mode backgrounds."""


@dataclass(frozen=True)
class RegionMask:
    """A rectangle to obliterate, in **device points** with the origin top-left."""

    x: float
    y: float
    width: float
    height: float
    label: str = ""

    def as_pixels(self, scale: float) -> tuple[int, int, int, int]:
        left = int(self.x * scale)
        top = int(self.y * scale)
        return left, top, int(left + self.width * scale), int(top + self.height * scale)


# Token shapes worth scrubbing from logs. Deliberately conservative: over-redacting a
# log costs a little debuggability, under-redacting one publishes a credential.
_TEXT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._\-]{12,}"), r"\1<redacted>"),
    (re.compile(r"(?i)\b(authorization\s*[:=]\s*)\S+"), r"\1<redacted>"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"), "<redacted>"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"), "<redacted>"),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b"), "<redacted-email>"),
    (re.compile(r"\b(?:\d[ -]*?){13,19}\b"), "<redacted-pan>"),
)


@dataclass(frozen=True)
class RedactionPolicy:
    """What to obliterate. Comes from repo config; empty is a legitimate choice."""

    masks: tuple[RegionMask, ...] = field(default_factory=tuple)
    scale: float = 1.0
    mask_status_bar: bool = False
    status_bar_height_pt: float = 47.0
    scrub_text: bool = True

    def with_scale(self, scale: float) -> RedactionPolicy:
        return RedactionPolicy(
            masks=self.masks,
            scale=scale,
            mask_status_bar=self.mask_status_bar,
            status_bar_height_pt=self.status_bar_height_pt,
            scrub_text=self.scrub_text,
        )

    @property
    def is_noop(self) -> bool:
        return not self.masks and not self.mask_status_bar


def redact_image(data: bytes, policy: RedactionPolicy) -> bytes:
    """Return PNG bytes with every configured region filled.

    Always re-encodes, even when there is nothing to mask, so that a stored artifact's
    bytes are a function of our pipeline rather than of the capture tool's encoder.
    That keeps content addresses stable across Xcode versions.
    """
    try:
        image = Image.open(io.BytesIO(data))
    except Exception as exc:  # PIL raises a family of errors for malformed input
        raise RedactionError(
            f"could not decode a {len(data)}-byte image; refusing to store it unmasked"
        ) from exc

    with image:
        canvas = image.convert("RGB")
        draw = ImageDraw.Draw(canvas)

        if policy.mask_status_bar:
            height = int(policy.status_bar_height_pt * policy.scale)
            draw.rectangle((0, 0, canvas.width, height), fill=MASK_FILL)

        for mask in policy.masks:
            draw.rectangle(mask.as_pixels(policy.scale), fill=MASK_FILL)

        out = io.BytesIO()
        canvas.save(out, format="PNG", optimize=False)
        return out.getvalue()


def redact_text(text: str, policy: RedactionPolicy) -> str:
    if not policy.scrub_text:
        return text
    for pattern, replacement in _TEXT_PATTERNS:
        text = pattern.sub(replacement, text)
    return text
