"""Load a skill file: strict YAML frontmatter + a Markdown body.

The body is the model prompt for skills with a judgment step. It is never executed and
never parsed for control flow. See docs/adr/0001-skill-format-and-dispatch.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from sightline.core.skills.frontmatter import SkillFrontmatter

_FENCE = "---"


class SkillLoadError(Exception):
    """A skill failed to load. Fails the skill, never the run."""


@dataclass(frozen=True)
class Skill:
    frontmatter: SkillFrontmatter
    body: str
    source: Path


def load_skill(path: Path) -> Skill:
    text = path.read_text(encoding="utf-8")
    if not text.startswith(_FENCE):
        raise SkillLoadError(f"{path}: missing YAML frontmatter (file must start with ---)")
    parts = text.split(f"\n{_FENCE}\n", 1)
    if len(parts) != 2:
        raise SkillLoadError(f"{path}: frontmatter is not terminated by a --- line")
    raw = parts[0][len(_FENCE) :]
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        raise SkillLoadError(f"{path}: frontmatter is not valid YAML: {exc}") from exc
    try:
        fm = SkillFrontmatter.model_validate(data)
    except Exception as exc:  # pydantic ValidationError, incl. extra='forbid' typos
        raise SkillLoadError(f"{path}: {exc}") from exc
    return Skill(frontmatter=fm, body=parts[1].strip(), source=path)
