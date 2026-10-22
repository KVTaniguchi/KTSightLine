"""Acceptance criteria file schema (v1).

The file states what a spec promised, in a form Sightline can execute. Given/when/then
are prose for the human reader and the report, and are never parsed. ``surface``,
``fixture``, ``steps``, ``expect`` and ``expect_log`` are what runs.
See docs/adr/0004-acceptance-tier.md §2.

Steps and observation attributes are **closed vocabularies**, in the sense of ADR-0001
§3: the interpreter in ``runners/xcode/acceptance_driver.py`` implements exactly these
words, and a criterion needing another word is rejected with that word's name. The name
is the ticket title.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

CRITERION_ID_PATTERN = r"^[A-Za-z][A-Za-z0-9]*(?:[.-][A-Za-z0-9]+)*$"
EVENT_PATTERN = r"^[a-z][a-z0-9_]*$"

STEP_ACTIONS = ("tap", "type", "wait_for")
OBSERVATION_ATTRIBUTES = ("exists", "label", "label_contains", "value", "enabled")

METHOD_PREFIX = "test_AC_"


def method_name(criterion_id: str) -> str:
    """The generated XCTest method for one criterion.

    `.` and `-` both become `_`, so two IDs can collide. The linter rejects that; the ID
    pattern forbids `_` so no other collision is possible.
    """
    return METHOD_PREFIX + criterion_id.replace(".", "_").replace("-", "_")


class Evidence(StrEnum):
    UI_STATE = "ui_state"  # observed on screen. The v1 executor.
    LOG_EVENT = "log_event"  # SIGHTLINE-AC| lines the app logs (ADR-0004 §5)
    NETWORK_CALL = "network_call"  # parses; no executor in v1
    PERSISTED_STATE = "persisted_state"  # parses; no executor in v1


EXECUTABLE_EVIDENCE = frozenset({Evidence.UI_STATE, Evidence.LOG_EVENT})


class TypeText(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    into: str
    text: str


class Step(BaseModel):
    """One action in the `when`. Written in YAML as ``{tap: id}``, ``{wait_for: id}``,
    or ``{type: {into: id, text: "..."}}``; normalized here to ``action`` + ``target``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: Literal["tap", "type", "wait_for"]
    target: str = Field(min_length=1)
    text: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _from_yaml_shape(cls, data: Any) -> Any:
        if not isinstance(data, dict) or "action" in data:
            return data
        if len(data) != 1:
            raise ValueError(
                f"a step is exactly one action, got {sorted(data)}; "
                f"write each action as its own list item ({', '.join(STEP_ACTIONS)})"
            )
        ((action, arg),) = data.items()
        if action not in STEP_ACTIONS:
            raise ValueError(
                f"step `{action}` is not in the v1 vocabulary ({', '.join(STEP_ACTIONS)})"
            )
        if action == "type":
            typed = TypeText.model_validate(arg)
            return {"action": "type", "target": typed.into, "text": typed.text}
        return {"action": action, "target": arg}

    @model_validator(mode="after")
    def _text_only_for_type(self) -> Step:
        if (self.action == "type") != (self.text is not None):
            raise ValueError("only a `type` step carries text")
        return self


class Observation(BaseModel):
    """One ``ui_state`` expectation: an element, by accessibility identifier, and the
    attributes it must have. Every attribute given must hold for the observation to be met.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    exists: bool | None = None
    label: str | None = None
    label_contains: str | None = None
    value: str | None = None
    enabled: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def _closed_vocabulary(cls, data: Any) -> Any:
        if isinstance(data, dict):
            unknown = sorted(set(data) - {"id", *OBSERVATION_ATTRIBUTES})
            if unknown:
                raise ValueError(
                    f"expectation attribute `{unknown[0]}` is not in the v1 vocabulary "
                    f"({', '.join(OBSERVATION_ATTRIBUTES)})"
                )
        return data

    @model_validator(mode="after")
    def _says_something(self) -> Observation:
        attrs = self.attributes()
        if not attrs:
            raise ValueError(
                f"expectation on `{self.id}` checks nothing; give at least one of "
                f"{', '.join(OBSERVATION_ATTRIBUTES)}"
            )
        if self.exists is False and len(attrs) > 1:
            raise ValueError(
                f"expectation on `{self.id}` says `exists: false` and also checks "
                f"{sorted(set(attrs) - {'exists'})}; an absent element has no attributes"
            )
        return self

    def attributes(self) -> dict[str, str | bool]:
        """The attributes this observation asserts, in vocabulary order."""
        return {
            name: value
            for name in OBSERVATION_ATTRIBUTES
            if (value := getattr(self, name)) is not None
        }


class LogObservation(BaseModel):
    """One ``log_event`` expectation: how many ``SIGHTLINE-AC|<event>|`` lines the app
    logged during this criterion (ADR-0004 §5). ``count`` is exact; ``min_count`` is a
    floor. ``count: 0`` asserts the event never happened."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event: Annotated[str, Field(pattern=EVENT_PATTERN)]
    count: Annotated[int, Field(ge=0)] | None = None
    min_count: Annotated[int, Field(ge=1)] | None = None

    @model_validator(mode="after")
    def _one_bound(self) -> LogObservation:
        if (self.count is None) == (self.min_count is None):
            raise ValueError(
                f"log expectation `{self.event}` needs exactly one of count, min_count"
            )
        return self

    def met_by(self, observed: int) -> bool:
        if self.count is not None:
            return observed == self.count
        return observed >= (self.min_count or 0)

    def expected(self) -> dict[str, int]:
        return (
            {"count": self.count} if self.count is not None else {"min_count": self.min_count or 0}
        )


class Criterion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: Annotated[str, Field(pattern=CRITERION_ID_PATTERN)]
    given: str
    when: str
    then: str
    surface: str | None = None
    fixture: str | None = None
    steps: list[Step] = Field(default_factory=list)
    expect: list[Observation] = Field(default_factory=list)
    expect_log: list[LogObservation] = Field(default_factory=list)
    evidence: Evidence = Evidence.UI_STATE
    manual: bool = False
    reason: str | None = None

    @model_validator(mode="after")
    def _manual_says_why(self) -> Criterion:
        if self.manual and not (self.reason or "").strip():
            raise ValueError("a manual criterion needs a `reason` saying why it cannot run")
        return self

    @property
    def method_name(self) -> str:
        return method_name(self.id)

    @property
    def executable(self) -> bool:
        return not self.manual and self.evidence in EXECUTABLE_EVIDENCE


class CriteriaFile(BaseModel):
    """``extra='forbid'`` everywhere, like ``SkillFrontmatter``.

    A typo'd key that silently means "never checked" is the failure that makes people
    stop trusting this.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    acceptance_schema: Literal[1]
    spec_id: str = Field(min_length=1)
    provenance: dict[str, str] = Field(default_factory=dict)
    """Opaque. Copied into the report untouched; Sightline never interprets it."""
    criteria: Annotated[list[Criterion], Field(min_length=1)]

    @classmethod
    def from_yaml(cls, text: str) -> CriteriaFile:
        return cls.model_validate(yaml.safe_load(text) or {})

    @classmethod
    def load(cls, path: Path) -> CriteriaFile:
        return cls.from_yaml(Path(path).read_text(encoding="utf-8"))
