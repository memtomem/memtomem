"""Generate agent-specific configuration files from unified context."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from memtomem.context.parser import iter_markdown_sections, split_preamble


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


_RESERVED_SECTION_KEYS = {
    "Project",
    "Commands",
    "Architecture",
    "Rules",
    "Style",
    "Claude",
    "Cursor",
    "Gemini",
    "Codex",
    "Copilot",
}

_RESERVED_SECTION_KEYS_CASEFOLD = {
    "claude-specific",
    "cursor-specific",
    "gemini-specific",
    "codex-specific",
    "copilot-specific",
}


def _append_unknown_sections(lines: list[str], sections: dict[str, str]) -> None:
    for heading, content in sections.items():
        if (
            heading not in _RESERVED_SECTION_KEYS
            and heading.lower() not in _RESERVED_SECTION_KEYS_CASEFOLD
        ):
            lines.append(_section_block(heading, content))


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
        _append_unknown_sections(lines, sections)
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
        _append_unknown_sections(lines, sections)
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
        _append_unknown_sections(lines, sections)
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
        _append_unknown_sections(lines, sections)
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
            lines.append("## Copilot-Specific\n")
            lines.append(sections["Copilot"])
            lines.append("")
        _append_unknown_sections(lines, sections)
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


# Boilerplate each generator's generate() emits *before* the first "##"
# heading. On reverse-import we strip it so it is not re-imported as project
# text. MUST stay in lock-step with the generate() methods above and with
# parser.sections_to_markdown's "# Project Context" wrapper (_WRAPPER_H1).
# cursor/copilot emit the Project body directly as leading text with no H1 —
# nothing to strip; for them the preamble *is* the Project section (the
# round-trip loss this fixes, #1147 B1-3).
_SOURCE_BOILERPLATE: dict[str, tuple[str, ...]] = {
    "claude": (
        "# CLAUDE.md",
        "This file provides guidance to Claude Code (claude.ai/code) "
        "when working with code in this repository.",
    ),
    "gemini": (
        "# GEMINI.md",
        "This file provides guidance to Gemini CLI when working with code in this repository.",
    ),
    "codex": ("# AGENTS.md",),
    "cursor": (),
    "copilot": (),
}

# Wrapper H1 emitted by parser.sections_to_markdown, regardless of source.
_WRAPPER_H1 = "# Project Context"


def _clean_preamble(preamble: str, source: str) -> str:
    """Drop generated boilerplate lines from a reverse-import preamble.

    Removes the canonical wrapper H1 plus ``source``'s known boilerplate
    lines, then returns the remaining real project prose (stripped). An empty
    return means the preamble was pure boilerplate (the claude/gemini/codex
    case) and contributes nothing.
    """
    drop = {_WRAPPER_H1, *_SOURCE_BOILERPLATE.get(source, ())}
    kept = [line for line in preamble.splitlines() if line.strip() not in drop]
    return "\n".join(kept).strip()


def preamble_source(agent: str | None, path: Path) -> str | None:
    """Return *agent* as a reverse-import preamble source only when *path* is
    that agent's canonical Project-bearing file (its generator ``output_path``).

    ``detect_agent_files`` reports rule fragments such as ``.cursor/rules/*.mdc``
    with ``agent="cursor"`` too; those are not Project prose, so importing them
    via ``extract_sections_from_agent_file(..., source="cursor")`` would wrongly
    seed ``Project`` with rule-fragment content. Gating on the generator's own
    ``output_path`` keeps fragments on the ``source=None`` (drop-preamble) path,
    while ``.cursorrules`` / ``CLAUDE.md`` / ``GEMINI.md`` / ``AGENTS.md`` /
    ``copilot-instructions.md`` still capture their leading Project prose
    (#1147 B1-3 review).
    """
    if agent is None:
        return None
    gen = GENERATORS.get(agent)
    if gen is None:
        return None
    return agent if path.name == Path(gen.output_path).name else None


def extract_sections_from_agent_file(content: str, source: str | None = None) -> dict[str, str]:
    """Reverse-extract sections from an existing agent file (CLAUDE.md, etc.).

    Maps agent-specific headings back to canonical section names.

    ``source`` is the detecting generator's name (``"claude"``, ``"cursor"``,
    ``"gemini"``, ``"codex"``, ``"copilot"``). When given, leading text before
    the first ``##`` heading — which is real project prose for the
    cursor/copilot targets that emit the Project body with no H1 — is captured
    into the canonical ``Project`` section after stripping the source's
    generated boilerplate (#1147 B1-3). Every ``##`` starts a section, so a
    captured ``Project`` body never contains a ``##`` that would re-split on
    the next round-trip; keep subheadings inside a section with ``###``. When
    ``source`` is ``None`` the preamble is dropped exactly as before
    (back-compat): the canonical parser contract is unchanged.

    Pass ``source`` only for a generator's *canonical* Project-bearing file
    (see :func:`preamble_source`); rule fragments such as ``.cursor/rules/*``
    are also detected as ``agent="cursor"`` but are not Project prose, so they
    must reach this function with ``source=None``.
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
        # Agent-specific override sections — must round-trip through
        # generate() which emits "## <Agent>-Specific" headings.
        "claude-specific": "Claude",
        "cursor-specific": "Cursor",
        "gemini-specific": "Gemini",
        "codex-specific": "Codex",
        "copilot-specific": "Copilot",
    }

    # Every ## heading starts a section; the text before the first one is the
    # preamble (captured into Project for known sources below). A section's own
    # subheadings must use ### so they are not mis-read as separate sections —
    # this keeps the captured Project body free of round-trip-unstable ## lines.
    preamble, rest = split_preamble(content)

    sections: dict[str, str] = {}
    for heading, body in iter_markdown_sections(rest):
        key = aliases.get(heading.lower(), heading)
        # Two source headings can alias to one canonical key (e.g. "Rules" and
        # "Coding Rules" → "Rules"); merge rather than overwrite so the earlier
        # block's content is not silently lost (#1123 B1-2).
        if key in sections:
            sections[key] = f"{sections[key]}\n\n{body}".strip()
        else:
            sections[key] = body

    # Capture non-boilerplate leading prose into Project for known sources,
    # prepended to any existing ## Project body to preserve document order.
    if source is not None:
        leading = _clean_preamble(preamble, source)
        if leading:
            if "Project" in sections:
                sections["Project"] = f"{leading}\n\n{sections['Project']}".strip()
            else:
                sections["Project"] = leading

    return sections
