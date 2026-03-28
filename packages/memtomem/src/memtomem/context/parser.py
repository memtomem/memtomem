"""Parse .memtomem/context.md into structured sections."""

from __future__ import annotations

import re
from pathlib import Path

CONTEXT_FILENAME = ".memtomem/context.md"

# Known section names (case-insensitive matching)
KNOWN_SECTIONS = {"project", "commands", "architecture", "rules", "style"}


def parse_context(path: Path) -> dict[str, str]:
    """Parse context.md into {section_name: content} dict.

    Sections are delimited by `## SectionName` headings.
    Unknown sections are preserved as-is.
    """
    if not path.exists():
        return {}

    text = path.read_text(encoding="utf-8")
    sections: dict[str, str] = {}
    current_section: str | None = None
    current_lines: list[str] = []

    for line in text.splitlines():
        heading = re.match(r"^##\s+(.+)$", line)
        if heading:
            # Save previous section
            if current_section is not None:
                sections[current_section] = "\n".join(current_lines).strip()
            current_section = heading.group(1).strip()
            current_lines = []
        elif current_section is not None:
            current_lines.append(line)

    # Save last section
    if current_section is not None:
        sections[current_section] = "\n".join(current_lines).strip()

    return sections


def sections_to_markdown(sections: dict[str, str]) -> str:
    """Convert sections dict back to context.md format."""
    lines = ["# Project Context\n"]
    for name, content in sections.items():
        lines.append(f"## {name}\n")
        lines.append(content)
        lines.append("")
    return "\n".join(lines)
