"""The criteria linter. Runs on Linux with no Xcode.

Every message names the fix. A criteria file is often written by a model, and the lint
loop is how that author learns what to change; "invalid" with nothing else is a dead end.

Two layers:

* **Shape** — the pydantic schema in ``criteria.py``. Its errors are rewritten here into
  messages that name the criterion and the fix.
* **Meaning** — rules that need the whole file or the repo config: evidence without an
  observation, unknown surfaces and fixtures, colliding method names.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import ValidationError

from sightline.config import RepoConfig
from sightline.core.acceptance.criteria import (
    OBSERVATION_ATTRIBUTES,
    CriteriaFile,
    Evidence,
)

LOCALE_FLAGS = ("-AppleLanguages", "-AppleLocale")


@dataclass(frozen=True)
class LintIssue:
    level: Literal["error", "warning"]
    criterion: str | None
    message: str

    def __str__(self) -> str:
        where = f"{self.criterion}: " if self.criterion else ""
        return f"{self.level}: {where}{self.message}"


def _criterion_label(raw: Any, loc: tuple[int | str, ...]) -> str | None:
    if len(loc) >= 2 and loc[0] == "criteria" and isinstance(loc[1], int):
        try:
            cid = raw["criteria"][loc[1]].get("id")
        except (KeyError, IndexError, TypeError, AttributeError):
            cid = None
        return str(cid) if cid else f"criteria[{loc[1]}]"
    return None


def _shape_message(error: dict[str, Any]) -> str:
    loc = [p for p in error["loc"] if not isinstance(p, int)]
    field = ".".join(str(p) for p in loc[1:] if p != "criteria") or ".".join(map(str, loc))
    kind = error["type"]
    if kind == "extra_forbidden":
        key = loc[-1] if loc else "?"
        return f"unknown key `{key}`; check the spelling (unknown keys are rejected so nothing is silently unchecked)"
    if kind == "missing":
        return f"missing required key `{loc[-1] if loc else '?'}`"
    if kind == "string_pattern_mismatch" and loc and loc[-1] == "id":
        return (
            "criterion id must be letters and digits separated by `.` or `-`, "
            "starting with a letter (e.g. `CHK-7.AC1`)"
        )
    if kind == "string_pattern_mismatch" and loc and loc[-1] == "event":
        return "log event names are lower_snake_case (e.g. `order_submitted`)"
    if kind == "literal_error" and loc and loc[-1] == "acceptance_schema":
        return "set `acceptance_schema: 1`"
    msg = str(error["msg"]).removeprefix("Value error, ")
    return f"{field}: {msg}" if field and kind != "value_error" else msg


def load(text: str) -> tuple[CriteriaFile | None, list[LintIssue]]:
    """Parse a criteria file, turning every schema error into a LintIssue."""
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        return None, [LintIssue("error", None, f"not valid YAML: {exc}")]
    if not isinstance(raw, dict):
        return None, [LintIssue("error", None, "a criteria file is a YAML mapping")]
    try:
        return CriteriaFile.model_validate(raw), []
    except ValidationError as exc:
        issues = [
            LintIssue("error", _criterion_label(raw, tuple(e["loc"])), _shape_message(dict(e)))
            for e in exc.errors()
        ]
        return None, issues


def lint(criteria: CriteriaFile, config: RepoConfig) -> list[LintIssue]:
    issues: list[LintIssue] = []

    def error(cid: str | None, msg: str) -> None:
        issues.append(LintIssue("error", cid, msg))

    def warn(cid: str | None, msg: str) -> None:
        issues.append(LintIssue("warning", cid, msg))

    seen: dict[str, int] = defaultdict(int)
    methods: dict[str, list[str]] = defaultdict(list)
    for c in criteria.criteria:
        seen[c.id] += 1
        methods[c.method_name].append(c.id)
    for cid, n in seen.items():
        if n > 1:
            error(cid, f"id appears {n} times; criterion ids must be unique")
    for method, ids in methods.items():
        if len(set(ids)) > 1:
            error(
                ids[1],
                f"collides with `{ids[0]}`: both become the test method `{method}` "
                "(`.` and `-` both map to `_`); rename one",
            )

    surfaces = sorted(config.surfaces)
    fixtures = sorted(config.fixtures)
    for c in criteria.criteria:
        if c.surface is not None and c.surface not in config.surfaces:
            error(
                c.id,
                f"surface `{c.surface}` is not configured; add it under `surfaces:` in "
                f".sightline/config.yml or use one of {surfaces or '(none configured)'}",
            )
        if c.fixture is not None and c.fixture not in config.fixtures:
            error(
                c.id,
                f"fixture `{c.fixture}` is not configured; add it under `fixtures:` in "
                f".sightline/config.yml or use one of {fixtures or '(none configured)'}",
            )

        if c.manual:
            if c.steps or c.expect or c.expect_log:
                warn(
                    c.id,
                    "manual criteria are never executed; its steps and expectations are ignored",
                )
            continue

        if c.evidence is Evidence.UI_STATE and not c.expect:
            error(
                c.id,
                "a ui_state criterion needs at least one `expect` "
                "(e.g. `expect: [{id: <accessibility id>, exists: true}]`), "
                "or `manual: true` with a `reason`",
            )
        if c.evidence is Evidence.LOG_EVENT and not c.expect_log:
            error(
                c.id,
                "a log_event criterion needs at least one `expect_log` "
                "(e.g. `expect_log: [{event: order_submitted, count: 1}]`), "
                "or `manual: true` with a `reason`",
            )
        if c.evidence in (Evidence.NETWORK_CALL, Evidence.PERSISTED_STATE):
            warn(
                c.id,
                f"`{c.evidence.value}` has no executor in v1; this criterion will be "
                "not_verifiable(evidence_type_unsupported). Use ui_state or log_event to check it",
            )
            continue
        if c.expect_log and c.evidence is not Evidence.LOG_EVENT:
            warn(
                c.id,
                "has `expect_log` but `evidence` is not log_event; the log expectations still "
                "run, but set `evidence: log_event` so the report labels it",
            )

        for o in c.expect:
            if o.exists is None:
                checked = ", ".join(k for k in OBSERVATION_ATTRIBUTES if k in o.attributes())
                warn(
                    c.id,
                    f"if `{o.id}` is absent this is not_verifiable(no_observable), not a fail "
                    f"(it checks {checked} but not existence). Add `exists: true` if a missing "
                    "element should fail the criterion",
                )

    for name, surface in config.surfaces.items():
        if any(a in LOCALE_FLAGS for a in surface.launch_arguments):
            error(None, f"surface `{name}` sets the locale; remove it, set `acceptance.locale`")
    for name, fixture in config.fixtures.items():
        if any(a in LOCALE_FLAGS for a in fixture.launch_arguments):
            error(None, f"fixture `{name}` sets the locale; remove it, set `acceptance.locale`")

    return issues


def lint_text(text: str, config: RepoConfig) -> tuple[CriteriaFile | None, list[LintIssue]]:
    criteria, issues = load(text)
    if criteria is None:
        return None, issues
    return criteria, lint(criteria, config)


def lint_file(path: Path, config: RepoConfig) -> tuple[CriteriaFile | None, list[LintIssue]]:
    return lint_text(Path(path).read_text(encoding="utf-8"), config)


def has_errors(issues: list[LintIssue]) -> bool:
    return any(i.level == "error" for i in issues)
