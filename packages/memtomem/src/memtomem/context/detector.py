"""Detect agent configuration files in a project directory."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

DetectedKind = Literal["file", "skill_dir"]


@dataclass
class DetectedFile:
    agent: str
    path: Path
    size: int
    kind: DetectedKind = "file"


# Agent name → list of possible file paths (relative to project root)
AGENT_FILES: dict[str, list[str]] = {
    "claude": ["CLAUDE.md"],
    "cursor": [".cursorrules", ".cursor/rules"],
    "gemini": ["GEMINI.md"],
    "codex": ["AGENTS.md"],
    "copilot": [".github/copilot-instructions.md"],
}


# Skill-runtime name → list of possible skill root directories (relative to project root).
# Each entry points at a directory that contains one sub-directory per skill; every valid
# skill sub-directory must contain a SKILL.md file.
#
# Note: Anthropic released the Agent Skills specification as an open standard in 2025-12 and
# OpenAI adopted the same SKILL.md format for Codex CLI. Codex's primary project-scope path
# is ``.agents/skills/`` — which Gemini CLI *also* recognizes as an alias. We therefore
# attribute ``.agents/skills/`` to Codex (primary) and leave Gemini with its own
# ``.gemini/skills/``. When both runtimes are fanned out, Gemini will still pick up the
# Codex copy through its alias resolution.
SKILL_DIRS: dict[str, list[str]] = {
    "claude_skills": [".claude/skills"],
    "gemini_skills": [".gemini/skills"],
    "codex_skills": [".agents/skills"],
}


def detect_agent_files(project_root: Path) -> list[DetectedFile]:
    """Scan project root for known agent configuration files.

    Returns a list of detected files sorted by agent name.
    """
    found: list[DetectedFile] = []

    for agent, paths in AGENT_FILES.items():
        for rel_path in paths:
            full_path = project_root / rel_path
            if full_path.exists():
                if full_path.is_file():
                    found.append(
                        DetectedFile(agent=agent, path=full_path, size=full_path.stat().st_size)
                    )
                elif full_path.is_dir():
                    # .cursor/rules/ is a directory — count md files inside
                    md_files = list(full_path.glob("*.md")) + list(full_path.glob("*.mdc"))
                    for md in md_files:
                        found.append(DetectedFile(agent=agent, path=md, size=md.stat().st_size))

    return sorted(found, key=lambda f: (f.agent, str(f.path)))


def detect_skill_dirs(project_root: Path) -> list[DetectedFile]:
    """Scan project root for runtime-specific skill directories.

    Each discovered skill is reported as a ``DetectedFile`` with
    ``kind="skill_dir"``. The ``path`` points at the skill's root directory
    (e.g. ``.claude/skills/code-review/``) and ``size`` is the byte size of the
    contained ``SKILL.md`` file (``0`` when missing).
    """
    found: list[DetectedFile] = []

    for agent, paths in SKILL_DIRS.items():
        for rel_path in paths:
            root = project_root / rel_path
            if not root.exists() or not root.is_dir():
                continue
            for skill_dir in sorted(p for p in root.iterdir() if p.is_dir()):
                skill_md = skill_dir / "SKILL.md"
                if not skill_md.is_file():
                    # Silently skip non-skill sub-directories so that users can
                    # keep auxiliary folders side-by-side with real skills.
                    continue
                found.append(
                    DetectedFile(
                        agent=agent,
                        path=skill_dir,
                        size=skill_md.stat().st_size,
                        kind="skill_dir",
                    )
                )

    return sorted(found, key=lambda f: (f.agent, str(f.path)))


def detect_all(project_root: Path) -> list[DetectedFile]:
    """Return both agent files and skill directories in a single list."""
    return detect_agent_files(project_root) + detect_skill_dirs(project_root)


__all__ = [
    "AGENT_FILES",
    "DetectedFile",
    "DetectedKind",
    "SKILL_DIRS",
    "detect_agent_files",
    "detect_all",
    "detect_skill_dirs",
]
