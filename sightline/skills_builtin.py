"""Locating built-in skills without hard-coding a path in three places."""

from __future__ import annotations

from pathlib import Path

from sightline.core.skills.loader import Skill, SkillLoadError, load_skill

BUILTIN_DIR = Path(__file__).resolve().parent.parent / "skills"


def load_all(extra_dirs: list[Path] | None = None) -> tuple[list[Skill], list[str]]:
    """Load built-ins plus any repo skill directories.

    A skill that fails to load fails *the skill*, not the run (ADR-0001 §1) — the
    errors come back alongside the good skills so the summary can name them.
    """
    errors: list[str] = []
    by_id: dict[str, Skill] = {}
    for directory in [BUILTIN_DIR, *(extra_dirs or [])]:
        if not Path(directory).is_dir():
            continue
        for path in sorted(Path(directory).rglob("*.md")):
            try:
                skill = load_skill(path)
            except SkillLoadError as exc:
                errors.append(str(exc))
                continue
            # Later directories win: a repo skill replaces a built-in wholesale.
            by_id[skill.frontmatter.id] = skill
    return list(by_id.values()), errors
