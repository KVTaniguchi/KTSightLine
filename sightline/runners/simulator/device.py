"""`simctl` wrapper carrying ADR-0003 §4's lifecycle policy.

Vendored rather than depending on an MCP server (D4): we need about a dozen
invocations, and every one of them carries retry, boot gating, and determinism policy
that an upstream wrapper has no reason to implement our way.

Every flag here was verified against Xcode 26.6 on 2026-08-31 (P6, P7). Do not add one
without checking `xcrun simctl help <subcommand>` first — a hallucinated flag costs an
afternoon.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


class SimctlError(RuntimeError):
    pass


class Appearance(StrEnum):
    LIGHT = "light"
    DARK = "dark"


class ContentSize(StrEnum):
    """Verified against `simctl help ui`. These exact spellings, no others."""

    EXTRA_SMALL = "extra-small"
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"
    EXTRA_LARGE = "extra-large"
    EXTRA_EXTRA_LARGE = "extra-extra-large"
    EXTRA_EXTRA_EXTRA_LARGE = "extra-extra-extra-large"
    AX_MEDIUM = "accessibility-medium"
    AX_LARGE = "accessibility-large"
    AX_EXTRA_LARGE = "accessibility-extra-large"
    AX_EXTRA_EXTRA_LARGE = "accessibility-extra-extra-large"
    AX5 = "accessibility-extra-extra-extra-large"


# `simctl privacy` has no camera and no notifications service (found while verifying
# P6/P7). Anything not in this set needs a different mechanism — do not silently
# pretend to revoke it.
PRIVACY_SERVICES = frozenset(
    {
        "all", "calendar", "contacts-limited", "contacts", "location",
        "location-always", "photos-add", "photos", "media-library",
        "microphone", "motion", "reminders", "siri",
    }
)  # fmt: skip


def _default_runner(cmd: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), capture_output=True, text=True, check=False)


@dataclass
class Simulator:
    """One simulator device, driven deterministically."""

    udid: str
    run: Runner = _default_runner

    # --- plumbing ---

    def _simctl(self, *args: str, check: bool = True) -> str:
        proc = self.run(["xcrun", "simctl", *args])
        if check and proc.returncode != 0:
            raise SimctlError(
                f"simctl {' '.join(args)} failed ({proc.returncode}): {(proc.stderr or '').strip()}"
            )
        return (proc.stdout or "").strip()

    # --- lifecycle (ADR-0003 §4) ---

    @classmethod
    def create(
        cls, name: str, device_type: str, runtime: str, *, run: Runner = _default_runner
    ) -> Simulator:
        proc = run(["xcrun", "simctl", "create", name, device_type, runtime])
        if proc.returncode != 0:
            raise SimctlError(f"could not create {name}: {(proc.stderr or '').strip()}")
        return cls(udid=(proc.stdout or "").strip(), run=run)

    def boot(self, *, attempts: int = 3) -> None:
        """Boot and block on readiness, erasing between attempts.

        Two deliberate choices. `bootstatus -b` blocks on real readiness instead of
        sleeping — sleeping is how you get a 15% flake rate. And a failed attempt is
        retried on an *erased* device rather than re-booting the sick one, because a
        simulator that failed to boot once usually fails again.
        """
        last = ""
        for attempt in range(1, attempts + 1):
            proc = self.run(["xcrun", "simctl", "bootstatus", self.udid, "-b"])
            if proc.returncode == 0:
                return
            last = (proc.stderr or proc.stdout or "").strip()
            if attempt < attempts:
                self.run(["xcrun", "simctl", "shutdown", self.udid])
                self.run(["xcrun", "simctl", "erase", self.udid])
        raise SimctlError(f"device {self.udid} failed to boot after {attempts} attempts: {last}")

    def shutdown(self) -> None:
        self._simctl("shutdown", self.udid, check=False)

    def erase(self) -> None:
        self._simctl("erase", self.udid, check=False)

    def delete(self) -> None:
        self._simctl("delete", self.udid, check=False)

    # --- deterministic state ---

    def set_content_size(self, size: ContentSize) -> None:
        self._simctl("ui", self.udid, "content_size", str(size))

    def set_appearance(self, appearance: Appearance) -> None:
        self._simctl("ui", self.udid, "appearance", str(appearance))

    def set_increase_contrast(self, enabled: bool) -> None:
        self._simctl("ui", self.udid, "increase_contrast", "enabled" if enabled else "disabled")

    def freeze_status_bar(
        self,
        *,
        time: str = "9:41",
        operator_name: str = "Sightline",
    ) -> None:
        """Pin every mutable status-bar element.

        Non-determinism here is indistinguishable from a real regression to the
        differential verifier — a clock that ticks between the base and head renders
        produces a diff on every run.

        ``--time`` takes a bare time string. `simctl help status_bar` claims "if the
        string is a valid ISO date string it will also set the date", but every ISO form
        tried was rejected on iOS 26.5 with "Invalid, non-ISO date/time string"
        (2026-08-31). Use `9:41` — Apple's own marketing time, and what the docs use.
        """
        self._simctl(
            "status_bar", self.udid, "override",
            "--time", time,
            "--dataNetwork", "wifi",
            "--wifiMode", "active",
            "--wifiBars", "3",
            "--cellularMode", "active",
            "--cellularBars", "4",
            "--operatorName", operator_name,
            "--batteryState", "charged",
            "--batteryLevel", "100",
        )  # fmt: skip

    def clear_status_bar(self) -> None:
        self._simctl("status_bar", self.udid, "clear", check=False)

    def revoke_privacy(self, service: str, bundle_id: str) -> None:
        if service not in PRIVACY_SERVICES:
            raise SimctlError(
                f"'{service}' is not a simctl privacy service. Available: "
                f"{sorted(PRIVACY_SERVICES)}. Camera and notifications are NOT among "
                "them and need another mechanism."
            )
        self._simctl("privacy", self.udid, "revoke", service, bundle_id)

    # --- capture ---

    def screenshot(self, dest: Path) -> Path:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._simctl("io", self.udid, "screenshot", str(dest))
        return dest

    def content_size(self) -> str:
        return self._simctl("ui", self.udid, "content_size")

    def appearance(self) -> str:
        return self._simctl("ui", self.udid, "appearance")

    def prepare(
        self,
        *,
        content_size: ContentSize = ContentSize.LARGE,
        appearance: Appearance = Appearance.LIGHT,
    ) -> dict[str, str]:
        """Boot and put the device in a known state. Returns the ArtifactRef context."""
        self.boot()
        self.freeze_status_bar()
        self.set_content_size(content_size)
        self.set_appearance(appearance)
        return {
            "udid": self.udid,
            "content_size": str(content_size),
            "appearance": str(appearance),
            "status_bar": "frozen",
        }
