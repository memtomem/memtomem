"""Detect agent configuration files in a project directory."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

DetectedKind = Literal["file", "skill_dir", "agent_file", "command_file"]


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

# Sub-agent-runtime name → project-scope directories containing ``<name>.md`` sub-agent
# files. Codex sub-agents are user-scope only (``~/.codex/agents/``) so they are not
# discoverable via the project root and intentionally omitted here.
AGENT_DIRS: dict[str, list[str]] = {
    "claude_agents": [".claude/agents"],
    "gemini_agents": [".gemini/agents"],
}

# Custom-command-runtime name → project-scope directories containing command files.
# Claude uses ``.md`` files, Gemini uses ``.toml`` — the detector reports both so
# context/commands.py can reverse-import whichever is present. Codex is deferred to
# Phase 3.5 (upstream-deprecated) and omitted here.
COMMAND_DIRS: dict[str, tuple[str, str]] = {
    "claude_commands": (".claude/commands", ".md"),
    "gemini_commands": (".gemini/commands", ".toml"),
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


def detect_agent_dirs(project_root: Path) -> list[DetectedFile]:
    """Scan project root for runtime-specific sub-agent files.

    Each discovered ``<name>.md`` file under a registered ``AGENT_DIRS`` entry
    is reported as a ``DetectedFile`` with ``kind="agent_file"``. Codex
    sub-agents live in ``~/.codex/agents/`` (user-scope) and are therefore
    **not** discoverable here — use :func:`memtomem.context.agents.diff_agents`
    for the Codex side.
    """
    found: list[DetectedFile] = []

    for agent, paths in AGENT_DIRS.items():
        for rel_path in paths:
            root = project_root / rel_path
            if not root.is_dir():
                continue
            for md_file in sorted(root.glob("*.md")):
                if not md_file.is_file():
                    continue
                found.append(
                    DetectedFile(
                        agent=agent,
                        path=md_file,
                        size=md_file.stat().st_size,
                        kind="agent_file",
                    )
                )

    return sorted(found, key=lambda f: (f.agent, str(f.path)))


def detect_command_dirs(project_root: Path) -> list[DetectedFile]:
    """Scan project root for runtime-specific slash-command files.

    Each file under a ``COMMAND_DIRS`` entry (matching the runtime's expected
    extension — ``.md`` for Claude, ``.toml`` for Gemini) is reported as a
    ``DetectedFile`` with ``kind="command_file"``.
    """
    found: list[DetectedFile] = []

    for runtime, (rel_path, suffix) in COMMAND_DIRS.items():
        root = project_root / rel_path
        if not root.is_dir():
            continue
        for cmd_file in sorted(root.iterdir()):
            if not cmd_file.is_file() or cmd_file.suffix != suffix:
                continue
            found.append(
                DetectedFile(
                    agent=runtime,
                    path=cmd_file,
                    size=cmd_file.stat().st_size,
                    kind="command_file",
                )
            )

    return sorted(found, key=lambda f: (f.agent, str(f.path)))


def detect_all(project_root: Path) -> list[DetectedFile]:
    """Return project-memory files, skill directories, sub-agent files, and commands."""
    return (
        detect_agent_files(project_root)
        + detect_skill_dirs(project_root)
        + detect_agent_dirs(project_root)
        + detect_command_dirs(project_root)
    )


__all__ = [
    "AGENT_DIRS",
    "AGENT_FILES",
    "COMMAND_DIRS",
    "DetectedFile",
    "DetectedKind",
    "SKILL_DIRS",
    "detect_agent_dirs",
    "detect_agent_files",
    "detect_all",
    "detect_command_dirs",
    "detect_skill_dirs",
]
