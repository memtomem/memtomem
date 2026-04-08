"""Helpers for reading/writing markdown memory files."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path


_FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def append_entry(
    file_path: Path,
    content: str,
    title: str | None = None,
    tags: list[str] | None = None,
) -> None:
    """Append a new entry to a markdown file, creating it if needed."""
    file_path.parent.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tag_str = f"\ntags: {tags}" if tags else ""

    # Skip heading if content already starts with one (e.g., from a template)
    stripped = content.strip()
    if stripped.startswith("## "):
        block = f"\n> created: {now}{tag_str}\n\n{stripped}\n"
    else:
        heading = f"## {title}" if title else f"## Entry {now}"
        block = f"\n{heading}\n\n> created: {now}{tag_str}\n\n{stripped}\n"

    with open(file_path, "a", encoding="utf-8") as f:
        if file_path.stat().st_size == 0 if file_path.exists() else True:
            pass  # append normally
        f.write(block)


def _validate_line_range(start_line: int, end_line: int, total_lines: int) -> None:
    """Validate 1-based inclusive line range."""
    if start_line < 1:
        raise ValueError(f"start_line must be >= 1, got {start_line}")
    if start_line > end_line:
        raise ValueError(f"start_line ({start_line}) must be <= end_line ({end_line})")
    if end_line > total_lines:
        raise ValueError(f"end_line ({end_line}) exceeds file length ({total_lines} lines)")


def replace_lines(file_path: Path, start_line: int, end_line: int, new_content: str) -> None:
    """Replace lines [start_line, end_line] (1-based, inclusive) with new_content."""
    text = file_path.read_text(encoding="utf-8")
    trailing_newline = text.endswith("\n") or text.endswith("\r\n")
    lines = text.splitlines()
    _validate_line_range(start_line, end_line, len(lines))
    before = lines[: start_line - 1]
    after = lines[end_line:]
    new_lines = before + new_content.splitlines() + after
    result = "\n".join(new_lines)
    if trailing_newline:
        result += "\n"
    file_path.write_text(result, encoding="utf-8")


def remove_lines(file_path: Path, start_line: int, end_line: int) -> None:
    """Remove lines [start_line, end_line] (1-based, inclusive) from file."""
    text = file_path.read_text(encoding="utf-8")
    trailing_newline = text.endswith("\n") or text.endswith("\r\n")
    lines = text.splitlines()
    _validate_line_range(start_line, end_line, len(lines))
    new_lines = lines[: start_line - 1] + lines[end_line:]
    result = "\n".join(new_lines)
    if trailing_newline and new_lines:
        result += "\n"
    file_path.write_text(result, encoding="utf-8")
