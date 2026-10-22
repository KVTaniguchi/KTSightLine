"""`.sightline/config.yml` — everything specific to a repo.

The brief is explicit: anything specific to one app lives in config, never in code. The
load-bearing part is `surfaces`, which is how a repo says *how to reach* each screen.
Impact analysis decides which screens a diff touches; config says how to get there. That
split keeps the deterministic layer app-agnostic without the harness having to guess at
navigation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from sightline.runners.xcode.driver_template import Surface

CONFIG_RELPATH = "config.yml"
CONFIG_DIR = ".sightline"


class SurfaceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    taps: list[str] = Field(default_factory=list)
    wait_for: str | None = None
    launch_arguments: list[str] = Field(default_factory=list)
    view: str | None = None
    """The Swift type that renders this surface.

    Lets impact analysis match a changed `struct CheckoutSummaryView: View` to the
    surface that shows it. Defaults to the surface's own key.
    """


class FixtureConfig(BaseModel):
    """Per-criterion app state for the acceptance tier (ADR-0004 §2).

    Sightline passes these at launch and does not know what they mean; honouring them is
    the app's job. Navigation belongs in `surfaces:`, not here.
    """

    model_config = ConfigDict(extra="forbid")

    launch_arguments: list[str] = Field(default_factory=list)
    launch_environment: dict[str, str] = Field(default_factory=dict)


class AcceptanceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: str = "en"
    locale: str = "en_US"
    """Pinned at every launch. `fr_FR` renders `$441.00` as `US$441,00`
    (verification-2026-10-11 V6), so an unpinned simulator changes verdicts."""
    log_subsystem: str | None = None
    """The `Logger` subsystem the app uses for `SIGHTLINE-AC|` lines. Read by the Claude
    Code skill when it proposes instrumentation; the parser keys on the prefix, because
    the subsystem does not appear in the captured output."""
    timeout_s: float = 10.0
    """Bound on every wait the driver makes. There is no sleep anywhere."""


PermissionAnswer = Literal["allow", "deny"]
"""How a run answers every system permission prompt it meets."""


class SimulatorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    udid: str | None = None
    device_type: str | None = None
    runtime: str | None = None
    name: str = "Sightline Device"


class RedactionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mask_status_bar: bool = False
    masks: list[dict[str, float | str]] = Field(default_factory=list)
    scale: float = 1.0


class RepoConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: str | None = None
    app_target: str | None = None
    ui_test_target: str | None = None
    scheme: str | None = None
    simulator: SimulatorConfig = Field(default_factory=SimulatorConfig)
    surfaces: dict[str, SurfaceConfig] = Field(default_factory=dict)
    dismiss_alerts: list[str] = Field(default_factory=list)
    """Button titles the driver may tap on a *system* alert (a permission prompt) that
    covers the app after launch. With an alert up, the audit throws "Invalid target
    app" instead of auditing anything.

    Not `simctl privacy grant`: on the iOS 26.5 simulator it exits 0 for `location`
    and the prompt still appears (measured on Nearby Meows, 2026-10-03). It also has
    no camera or notifications service, so the catalog's permission checks need this
    mechanism regardless. Titles are locale-dependent; the simulator runs in English.
    """
    deny_alerts: list[str] = Field(default_factory=list)
    """Button titles that *deny* a system permission prompt — "Don't Allow" and its
    per-service variants.

    Separate from `dismiss_alerts` rather than appended to it because the driver taps
    the first title it finds: one merged list would answer whichever button the alert
    happens to show first, which is the opposite of a controlled condition. A run asks
    for one answer or the other through `alert_titles()`, and a prompt carrying neither
    title is still `SIGHTLINE-UNDISMISSED-ALERT`.

    This is the only mechanism available for camera and notifications: neither is a
    `simctl privacy` service (re-confirmed on Xcode 27.1, 2026-10-20 — the list is
    calendar, contacts, contacts-limited, location, location-always, photos, photos-add,
    media-library, microphone, motion, reminders, siri).
    """
    redaction: RedactionConfig = Field(default_factory=RedactionConfig)
    fixtures: dict[str, FixtureConfig] = Field(default_factory=dict)
    acceptance: AcceptanceConfig = Field(default_factory=AcceptanceConfig)
    budget_usd: float = 0.50
    skills_dirs: list[str] = Field(default_factory=lambda: [".sightline/skills"])

    def alert_titles(self, *, answer: PermissionAnswer = "allow") -> tuple[str, ...]:
        """The button titles this run permits the driver to tap.

        Deliberately not a union. Under `answer="deny"` only denying titles are
        permitted, so a prompt whose deny button we have no title for fails the run
        loudly instead of being quietly granted — a silently granted permission makes a
        permission-denied check pass for the wrong reason, which is worse than no check.
        """
        return tuple(self.deny_alerts if answer == "deny" else self.dismiss_alerts)

    @classmethod
    def load(cls, root: Path, *, override: Path | None = None) -> RepoConfig:
        """Load from `<root>/.sightline/config.yml`. A missing file is a valid state.

        ``override`` points at a config kept outside the repo — for reviewing an app
        whose repository we may not commit a `.sightline/` directory to.
        """
        path = Path(override) if override else Path(root) / CONFIG_DIR / CONFIG_RELPATH
        if not path.exists():
            if override:
                raise FileNotFoundError(f"config {override} does not exist")
            return cls()
        data = yaml.safe_load(path.read_text()) or {}
        return cls.model_validate(data)

    def surfaces_for(self, views: frozenset[str]) -> list[Surface]:
        """The screens a change reaches, in declaration order.

        A surface matches when its `view` (or its own name) is among the changed view
        types. No match means no runtime work — which is the correct answer, not a
        failure: a diff that touches no configured screen has no screen to audit.
        """
        out = []
        for name, surface in self.surfaces.items():
            if (surface.view or name) in views:
                out.append(_surface(name, surface))
        return out

    def all_surfaces(self) -> list[Surface]:
        return [_surface(name, s) for name, s in self.surfaces.items()]


def _surface(name: str, config: SurfaceConfig) -> Surface:
    return Surface(
        name=name,
        taps=tuple(config.taps),
        wait_for=config.wait_for,
        launch_arguments=tuple(config.launch_arguments),
    )
