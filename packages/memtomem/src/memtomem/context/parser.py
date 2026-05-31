"""Parse .memtomem/context.md into structured sections."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

CONTEXT_FILENAME = ".memtomem/context.md"

# Known section names (case-insensitive matching)
KNOWN_SECTIONS = {"project", "commands", "architecture", "rules", "style"}

_HEADING_RE = re.compile(r"^##\s+(.+)$")
# A Markdown code fence: 3+ backticks or tildes, indented up to 3 spaces
# (CommonMark). group(1) is the fence run — its marker char and length
# identify the block; group(2) is the trailing text, which is an info string
# on an opening fence and must be blank on a closing fence.
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def iter_markdown_sections(text: str) -> Iterator[tuple[str, str]]:
    """Yield ``(heading, body)`` for each ``## Heading`` block in ``text``.

    Shared by :func:`parse_context` and
    :func:`memtomem.context.generator.extract_sections_from_agent_file` so the
    two round-trip halves stay in lock-step. The iterator is deliberately
    forgiving in two ways the previous naive loop got wrong (#1123 B1):

    - ``##`` lines **inside fenced code blocks** are treated as body, not as
      section delimiters, so a code sample containing ``## ...`` no longer
      truncates the real section (B1-1). Fences are matched by marker *type*
      and length (CommonMark): a ``~~~`` line inside a ``` ``` ``` block does
      not close it, so nested fenced examples round-trip correctly.
    - **Whitespace-only headings** (``##   ``) are treated as body rather than
      opening a section with an empty-string key (B1-3).

    Every real heading block is yielded — none are silently dropped. Duplicate
    headings are yielded once each; merging is left to the caller, which owns
    the heading→canonical-key mapping (B1-2). Preamble before the first heading
    is not yielded; callers that need it handle it separately.
    """
    current: str | None = None
    lines: list[str] = []
    # While inside a fenced code block these hold the opening fence's marker
    # char ("`" or "~") and length; ``fence_char is None`` means "not in code".
    fence_char: str | None = None
    fence_len = 0

    for line in text.splitlines():
        fence = _FENCE_RE.match(line)
        if fence:
            run, rest = fence.group(1), fence.group(2)
            if fence_char is None:
                # Opening fence — an info string after the run is allowed.
                fence_char, fence_len = run[0], len(run)
            elif run[0] == fence_char and len(run) >= fence_len and not rest.strip():
                # Closing fence: same marker, at least as long, no info string.
                # A non-matching fence (e.g. ``~~~`` inside a ``` ``` ``` block)
                # leaves the block open and falls through to body (B1-1).
                fence_char, fence_len = None, 0
            if current is not None:
                lines.append(line)
            continue

        m = None if fence_char is not None else _HEADING_RE.match(line)
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


def split_preamble(text: str) -> tuple[str, str]:
    """Split ``text`` at the first real ``## Heading`` line.

    Returns ``(preamble, rest)`` where ``rest`` begins at the first heading
    that :func:`iter_markdown_sections` would recognise and ``preamble`` is
    everything before it. If there is no real heading, the whole text is the
    preamble and ``rest`` is empty.

    Every ``## Heading`` is a section boundary: on reverse-import the text
    before the first one is the preamble (mapped to ``Project`` for known
    sources) and each heading starts its own section. This keeps a captured
    ``Project`` body free of ``##`` headings that would re-split on the next
    round-trip — to keep a subheading inside a section body, use ``###``
    (#1147 B1-3).

    The fence/heading rules are kept in lock-step with
    :func:`iter_markdown_sections` so a ``##`` inside a fenced code block is
    not a false boundary (B1-1) and a whitespace-only ``##`` is not treated
    as a heading (B1-3). Callers that want the preamble (e.g.
    :func:`memtomem.context.generator.extract_sections_from_agent_file`)
    use this; the section iterator itself still drops it.
    """
    lines = text.splitlines()
    fence_char: str | None = None
    fence_len = 0

    for i, line in enumerate(lines):
        fence = _FENCE_RE.match(line)
        if fence:
            run, rest = fence.group(1), fence.group(2)
            if fence_char is None:
                fence_char, fence_len = run[0], len(run)
            elif run[0] == fence_char and len(run) >= fence_len and not rest.strip():
                fence_char, fence_len = None, 0
            continue
        m = None if fence_char is not None else _HEADING_RE.match(line)
        if m and m.group(1).strip():
            return "\n".join(lines[:i]), "\n".join(lines[i:])

    return text, ""


def parse_context(path: Path) -> dict[str, str]:
    """Parse context.md into {section_name: content} dict.

    Sections are delimited by `## SectionName` headings.
    Unknown sections are preserved as-is. Repeated headings are merged
    (content concatenated) rather than the earlier copy being overwritten.

    Text before the first `## SectionName` heading is not a section and is
    not preserved — put project description under `## Project`. (Runtime-file
    reverse-import via
    :func:`memtomem.context.generator.extract_sections_from_agent_file` does
    capture such leading prose for known sources; canonical-parser preamble
    capture is deferred — see #1147 B1-3.)
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
