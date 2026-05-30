"""Parse .memtomem/context.md into structured sections."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

CONTEXT_FILENAME = ".memtomem/context.md"

# Known section names (case-insensitive matching)
KNOWN_SECTIONS = {"project", "commands", "architecture", "rules", "style"}

_HEADING_RE = re.compile(r"^##\s+(.+)$")
# Opening/closing fence for a Markdown code block (``` or ~~~), allowing
# leading indentation and a trailing language specifier.
_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")


def iter_markdown_sections(text: str) -> Iterator[tuple[str, str]]:
    """Yield ``(heading, body)`` for each ``## Heading`` block in ``text``.

    Shared by :func:`parse_context` and
    :func:`memtomem.context.generator.extract_sections_from_agent_file` so the
    two round-trip halves stay in lock-step. The iterator is deliberately
    forgiving in two ways the previous naive loop got wrong (#1123 B1):

    - ``##`` lines **inside fenced code blocks** are treated as body, not as
      section delimiters, so a code sample containing ``## ...`` no longer
      truncates the real section (B1-1).
    - **Whitespace-only headings** (``##   ``) are treated as body rather than
      opening a section with an empty-string key (B1-3).

    Every real heading block is yielded — none are silently dropped. Duplicate
    headings are yielded once each; merging is left to the caller, which owns
    the heading→canonical-key mapping (B1-2). Preamble before the first heading
    is not yielded; callers that need it handle it separately.
    """
    current: str | None = None
    lines: list[str] = []
    in_code = False

    for line in text.splitlines():
        if _FENCE_RE.match(line):
            in_code = not in_code
            if current is not None:
                lines.append(line)
            continue

        m = None if in_code else _HEADING_RE.match(line)
        heading = m.group(1).strip() if m else ""
        if heading:
            if current is not None:
                yield current, "\n".join(lines).strip()
            current = heading
            lines = []
        elif current is not None:
            lines.append(line)

    if current is not None:
        yield current, "\n".join(lines).strip()


def parse_context(path: Path) -> dict[str, str]:
    """Parse context.md into {section_name: content} dict.

    Sections are delimited by `## SectionName` headings.
    Unknown sections are preserved as-is. Repeated headings are merged
    (content concatenated) rather than the earlier copy being overwritten.
    """
    if not path.exists():
        return {}

    text = path.read_text(encoding="utf-8")
    sections: dict[str, str] = {}
    for name, body in iter_markdown_sections(text):
        if name in sections:
            sections[name] = f"{sections[name]}\n\n{body}".strip()
        else:
            sections[name] = body

    return sections


def sections_to_markdown(sections: dict[str, str]) -> str:
    """Convert sections dict back to context.md format."""
    lines = ["# Project Context\n"]
    for name, content in sections.items():
        lines.append(f"## {name}\n")
        lines.append(content)
        lines.append("")
    return "\n".join(lines)
