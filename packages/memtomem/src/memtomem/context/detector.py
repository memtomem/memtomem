"""Detect agent configuration files in a project directory."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class DetectedFile:
    agent: str
    path: Path
    size: int


# Agent name → list of possible file paths (relative to project root)
AGENT_FILES: dict[str, list[str]] = {
    "claude": ["CLAUDE.md"],
    "cursor": [".cursorrules", ".cursor/rules"],
    "gemini": ["GEMINI.md"],
    "codex": ["AGENTS.md"],
    "copilot": [".github/copilot-instructions.md"],
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
