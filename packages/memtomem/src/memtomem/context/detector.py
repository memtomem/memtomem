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

# Sub-agent-runtime name → project-scope directories containing sub-agent files.
# Claude / Gemini use Markdown (``<name>.md``); Codex uses TOML (``<name>.toml``).
# ``detect_agent_dirs`` is responsible for matching the right suffix per runtime.
AGENT_DIRS: dict[str, list[str]] = {
    "claude_agents": [".claude/agents"],
    "gemini_agents": [".gemini/agents"],
    "codex_agents": [".codex/agents"],
}

# Per-runtime suffix used by ``detect_agent_dirs`` when scanning ``AGENT_DIRS``.
AGENT_FILE_SUFFIX: dict[str, str] = {
    "claude_agents": ".md",
    "gemini_agents": ".md",
    "codex_agents": ".toml",
}

# Custom-command-runtime name → project-scope directories containing command files.
# Claude uses ``.md`` files, Gemini uses ``.toml`` — the detector reports both so
# context/commands.py can reverse-import whichever is present. Codex commands live
# in ``~/.codex/prompts/`` (user-scope) and are therefore not discoverable via the
# project root — use :func:`memtomem.context.commands.diff_commands` for the Codex
# side (symmetric with ``AGENT_DIRS``).
COMMAND_DIRS: dict[str, tuple[str, str]] = {
    "claude_commands": (".claude/commands", ".md"),
    "gemini_commands": (".gemini/commands", ".toml"),
}

# Settings runtimes are detected via the generators registered in
# ``context/settings.py``.  Unlike the dicts above there is no static
# mapping here — ``detect_settings_files()`` delegates to each
# generator's ``is_available()`` / ``target_file()`` at call time so
# that ``monkeypatch``-based HOME overrides in tests work correctly.


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

    Each discovered file under a registered ``AGENT_DIRS`` entry is reported as
    a ``DetectedFile`` with ``kind="agent_file"``. The expected suffix is per
    runtime (``.md`` for Claude / Gemini, ``.toml`` for Codex) — see
    ``AGENT_FILE_SUFFIX``.

    Codex CLI also accepts ``~/.codex/agents/`` (user-scope), but memtomem
    intentionally only fans out to and scans the project-scope path so that
    a project's canonical agents stay contained within the repository.
    """
    found: list[DetectedFile] = []

    for agent, paths in AGENT_DIRS.items():
        suffix = AGENT_FILE_SUFFIX.get(agent, ".md")
        for rel_path in paths:
            root = project_root / rel_path
            if not root.is_dir():
                continue
            for agent_file in sorted(root.glob(f"*{suffix}")):
                if not agent_file.is_file():
                    continue
                found.append(
                    DetectedFile(
                        agent=agent,
                        path=agent_file,
                        size=agent_file.stat().st_size,
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


def detect_settings_files() -> list[DetectedFile]:
    """Detect user-scope settings files for registered runtimes.

    Unlike the other ``detect_*`` functions this does **not** take a
    *project_root* because settings files live in the user's home directory.
    A runtime is reported only when its home directory exists (e.g.
    ``~/.claude/``), matching :meth:`ClaudeSettingsGenerator.is_available`.

    Delegates to generators from ``context/settings.py`` at call time (lazy
    import) to ensure ``monkeypatch``-based HOME overrides in tests work.
    """
    from memtomem.context.settings import SETTINGS_GENERATORS

    found: list[DetectedFile] = []
    for name, gen in SETTINGS_GENERATORS.items():
        if not gen.is_available():
            continue
        # project_root is not used by user-scope generators, but the
        # protocol requires it — pass cwd as a harmless default.
        target = gen.target_file(Path.cwd())
        if target.is_file():
            found.append(
                DetectedFile(
                    agent=name,
                    path=target,
                    size=target.stat().st_size,
                    kind="file",
                )
            )
        else:
            found.append(
                DetectedFile(
                    agent=name,
                    path=target,
                    size=0,
                    kind="file",
                )
            )
    return sorted(found, key=lambda f: (f.agent, str(f.path)))


def detect_all(project_root: Path) -> list[DetectedFile]:
    """Return project-memory files, skill dirs, sub-agents, commands, and settings."""
    return (
        detect_agent_files(project_root)
        + detect_skill_dirs(project_root)
        + detect_agent_dirs(project_root)
        + detect_command_dirs(project_root)
        + detect_settings_files()
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
    "detect_settings_files",
    "detect_skill_dirs",
]
