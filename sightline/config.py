"""`.sightline/config.yml` — everything specific to a repo.

The brief is explicit: anything specific to one app lives in config, never in code. The
load-bearing part is `surfaces`, which is how a repo says *how to reach* each screen.
Impact analysis decides which screens a diff touches; config says how to get there. That
split keeps the deterministic layer app-agnostic without the harness having to guess at
navigation.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from sightline.runners.xcode.driver_template import Surface

CONFIG_RELPATH = "config.yml"
CONFIG_DIR = ".sightline"


class SurfaceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    taps: list[str] = Field(default_factory=list)
    wait_for: str | None = None
    view: str | None = None
    """The Swift type that renders this surface.

    Lets impact analysis match a changed `struct CheckoutSummaryView: View` to the
    surface that shows it. Defaults to the surface's own key.
    """


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
    redaction: RedactionConfig = Field(default_factory=RedactionConfig)
    budget_usd: float = 0.50
    skills_dirs: list[str] = Field(default_factory=lambda: [".sightline/skills"])

    @classmethod
    def load(cls, root: Path) -> RepoConfig:
        """Load from `<root>/.sightline/config.yml`. A missing file is a valid state."""
        path = Path(root) / CONFIG_DIR / CONFIG_RELPATH
        if not path.exists():
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
                out.append(Surface(name=name, taps=tuple(surface.taps), wait_for=surface.wait_for))
        return out

    def all_surfaces(self) -> list[Surface]:
        return [
            Surface(name=name, taps=tuple(s.taps), wait_for=s.wait_for)
            for name, s in self.surfaces.items()
        ]
