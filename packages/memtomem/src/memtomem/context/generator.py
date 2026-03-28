"""Generate agent-specific configuration files from unified context."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class AgentGenerator(Protocol):
    """Protocol for agent-specific file generators."""

    name: str
    output_path: str  # relative to project root

    def generate(self, sections: dict[str, str]) -> str:
        """Generate the agent file content from context sections."""
        ...

    def detect(self, project_root: Path) -> Path | None:
        """Return path if agent file exists, else None."""
        ...


# ── Generator registry ────────────────────────────────────────────────

GENERATORS: dict[str, AgentGenerator] = {}


def _register(gen: AgentGenerator) -> AgentGenerator:
    GENERATORS[gen.name] = gen
    return gen


# ── Helpers ────────────────────────────────────────────────────────────


def _section_block(heading: str, content: str) -> str:
    return f"## {heading}\n\n{content}\n"


def _compact_rules(sections: dict[str, str]) -> str:
    """Extract Rules + Style as compact bullet points."""
    parts = []
    for key in ("Rules", "Style"):
        if key in sections:
            parts.append(sections[key])
    return "\n\n".join(parts)


# ── Claude Code ────────────────────────────────────────────────────────


@dataclass
class ClaudeGenerator:
    name: str = "claude"
    output_path: str = "CLAUDE.md"

    def generate(self, sections: dict[str, str]) -> str:
        lines = [
            "# CLAUDE.md\n",
            "This file provides guidance to Claude Code (claude.ai/code) "
            "when working with code in this repository.\n",
        ]
        if "Project" in sections:
            lines.append(_section_block("What is this project?", sections["Project"]))
        if "Commands" in sections:
            lines.append(_section_block("Build & Development Commands", sections["Commands"]))
        if "Architecture" in sections:
            lines.append(_section_block("Architecture", sections["Architecture"]))
        if "Rules" in sections:
            lines.append(_section_block("Coding Rules", sections["Rules"]))
        if "Style" in sections:
            lines.append(_section_block("Style", sections["Style"]))
        # Include any agent-specific overrides
        if "Claude" in sections:
            lines.append(_section_block("Claude-Specific", sections["Claude"]))
        return "\n".join(lines)

    def detect(self, project_root: Path) -> Path | None:
        p = project_root / self.output_path
        return p if p.exists() else None


_register(ClaudeGenerator())


# ── Cursor ─────────────────────────────────────────────────────────────


@dataclass
class CursorGenerator:
    name: str = "cursor"
    output_path: str = ".cursorrules"

    def generate(self, sections: dict[str, str]) -> str:
        lines = []
        if "Project" in sections:
            lines.append(sections["Project"])
            lines.append("")
        if "Commands" in sections:
            lines.append("## Commands\n")
            lines.append(sections["Commands"])
            lines.append("")
        rules = _compact_rules(sections)
        if rules:
            lines.append("## Rules\n")
            lines.append(rules)
            lines.append("")
        if "Architecture" in sections:
            lines.append("## Architecture\n")
            lines.append(sections["Architecture"])
            lines.append("")
        if "Cursor" in sections:
            lines.append("## Cursor-Specific\n")
            lines.append(sections["Cursor"])
            lines.append("")
        return "\n".join(lines)

    def detect(self, project_root: Path) -> Path | None:
        p = project_root / self.output_path
        return p if p.exists() else None


_register(CursorGenerator())


# ── Gemini CLI ─────────────────────────────────────────────────────────


@dataclass
class GeminiGenerator:
    name: str = "gemini"
    output_path: str = "GEMINI.md"

    def generate(self, sections: dict[str, str]) -> str:
        lines = [
            "# GEMINI.md\n",
            "This file provides guidance to Gemini CLI "
            "when working with code in this repository.\n",
        ]
        if "Project" in sections:
            lines.append(_section_block("Project", sections["Project"]))
        if "Commands" in sections:
            lines.append(_section_block("Commands", sections["Commands"]))
        if "Architecture" in sections:
            lines.append(_section_block("Architecture", sections["Architecture"]))
        if "Rules" in sections:
            lines.append(_section_block("Rules", sections["Rules"]))
        if "Style" in sections:
            lines.append(_section_block("Style", sections["Style"]))
        if "Gemini" in sections:
            lines.append(_section_block("Gemini-Specific", sections["Gemini"]))
        return "\n".join(lines)

    def detect(self, project_root: Path) -> Path | None:
        p = project_root / self.output_path
        return p if p.exists() else None


_register(GeminiGenerator())


# ── OpenAI Codex ───────────────────────────────────────────────────────


@dataclass
class CodexGenerator:
    name: str = "codex"
    output_path: str = "AGENTS.md"

    def generate(self, sections: dict[str, str]) -> str:
        lines = ["# AGENTS.md\n"]
        if "Project" in sections:
            lines.append(_section_block("Project", sections["Project"]))
        if "Commands" in sections:
            lines.append(_section_block("Commands", sections["Commands"]))
        if "Architecture" in sections:
            lines.append(_section_block("Architecture", sections["Architecture"]))
        rules = _compact_rules(sections)
        if rules:
            lines.append(_section_block("Rules", rules))
        if "Codex" in sections:
            lines.append(_section_block("Codex-Specific", sections["Codex"]))
        return "\n".join(lines)

    def detect(self, project_root: Path) -> Path | None:
        p = project_root / self.output_path
        return p if p.exists() else None


_register(CodexGenerator())


# ── GitHub Copilot ─────────────────────────────────────────────────────


@dataclass
class CopilotGenerator:
    name: str = "copilot"
    output_path: str = ".github/copilot-instructions.md"

    def generate(self, sections: dict[str, str]) -> str:
        lines = []
        if "Project" in sections:
            lines.append(sections["Project"])
            lines.append("")
        rules = _compact_rules(sections)
        if rules:
            lines.append("## Rules\n")
            lines.append(rules)
            lines.append("")
        if "Commands" in sections:
            lines.append("## Commands\n")
            lines.append(sections["Commands"])
            lines.append("")
        if "Copilot" in sections:
            lines.append(sections["Copilot"])
            lines.append("")
        return "\n".join(lines)

    def detect(self, project_root: Path) -> Path | None:
        p = project_root / self.output_path
        return p if p.exists() else None


_register(CopilotGenerator())


# ── Public API ─────────────────────────────────────────────────────────


def generate_for_agent(agent: str, sections: dict[str, str]) -> str:
    """Generate agent file content. Raises KeyError if agent unknown."""
    gen = GENERATORS[agent]
    return gen.generate(sections)


def generate_all(sections: dict[str, str]) -> dict[str, str]:
    """Generate all agent files. Returns {agent_name: content}."""
    return {name: gen.generate(sections) for name, gen in GENERATORS.items()}


def extract_sections_from_agent_file(content: str) -> dict[str, str]:
    """Reverse-extract sections from an existing agent file (CLAUDE.md, etc.).

    Maps agent-specific headings back to canonical section names.
    """
    # Heading aliases → canonical section name
    aliases: dict[str, str] = {
        "what is this project?": "Project",
        "project": "Project",
        "build & development commands": "Commands",
        "build and development commands": "Commands",
        "commands": "Commands",
        "architecture": "Architecture",
        "coding rules": "Rules",
        "rules": "Rules",
        "style": "Style",
    }

    sections: dict[str, str] = {}
    current: str | None = None
    lines: list[str] = []

    for line in content.splitlines():
        m = re.match(r"^##\s+(.+)$", line)
        if m:
            if current is not None:
                sections[current] = "\n".join(lines).strip()
            heading = m.group(1).strip()
            current = aliases.get(heading.lower(), heading)
            lines = []
        elif current is not None:
            lines.append(line)

    if current is not None:
        sections[current] = "\n".join(lines).strip()

    return sections
