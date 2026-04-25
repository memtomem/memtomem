"""Helpers for reading/writing markdown memory files."""

from __future__ import annotations

import json
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
    """Append a new entry to a markdown file, creating it if needed.

    The per-entry metadata (``> created: ...`` and optional
    ``> tags: [...]``) is emitted as a single explicit blockquote group
    — every line carries a leading ``> `` so we never rely on CommonMark
    lazy continuation, and the tag list is JSON (double-quoted) so it
    parses as YAML downstream. The chunker promotes ``> tags:`` into
    ``ChunkMetadata.tags`` and strips the header from chunk content (see
    ``memtomem.chunking.markdown.MarkdownChunker``).
    """
    file_path.parent.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tag_line = f"\n> tags: {json.dumps(list(tags))}" if tags else ""

    # Skip heading if content already starts with one (e.g., from a template)
    stripped = content.strip()
    if stripped.startswith("## "):
        block = f"\n> created: {now}{tag_line}\n\n{stripped}\n"
    else:
        heading = f"## {title}" if title else f"## Entry {now}"
        block = f"\n{heading}\n\n> created: {now}{tag_line}\n\n{stripped}\n"

    with open(file_path, "a", encoding="utf-8") as f:
        f.write(block)


def _find_body_start_index(chunk_lines: list[str]) -> int:
    """Return the index in *chunk_lines* where the entry body starts.

    Entry shape (as written by ``append_entry``):

    - Optional heading line (``## ...``)
    - Zero or more blank lines
    - Optional section-leading blockquote group (``>``-prefixed lines,
      with lazy-continuation lines accepted for legacy compatibility)
    - Zero or more blank lines
    - Body

    The index returned points to the first body line. A chunk that
    starts mid-section (an oversized section's non-first sub-chunk) has
    no heading or blockquote at its first line; the function then
    returns 0.
    """
    i = 0
    if i < len(chunk_lines) and chunk_lines[i].lstrip().startswith("#"):
        i += 1
    while i < len(chunk_lines) and not chunk_lines[i].strip():
        i += 1
    if i < len(chunk_lines) and chunk_lines[i].lstrip().startswith(">"):
        block_started = False
        while i < len(chunk_lines):
            stripped = chunk_lines[i].lstrip()
            if not stripped:
                break
            if stripped.startswith(">"):
                block_started = True
                i += 1
                continue
            if block_started:
                # Lazy continuation
                i += 1
                continue
            break
        while i < len(chunk_lines) and not chunk_lines[i].strip():
            i += 1
    return i


def replace_chunk_body(file_path: Path, start_line: int, end_line: int, new_content: str) -> None:
    """Replace a chunk's body in *file_path* while preserving its header.

    "Header" means the heading line and any section-leading blockquote
    group (``> created: ...`` / ``> tags: ...``). The chunker strips the
    blockquote header from chunk content, so callers of ``mem_edit``
    typically supply body-only ``new_content`` and would otherwise
    accidentally erase the metadata header.

    If ``new_content`` itself starts with ``## ``, the call is treated
    as a full replacement (preserving the pre-RFC ``mem_edit`` semantic
    where the user supplied the entire entry including heading); no
    header preservation is applied.
    """
    text = file_path.read_text(encoding="utf-8")
    trailing_newline = text.endswith("\n") or text.endswith("\r\n")
    lines = text.splitlines()
    _validate_line_range(start_line, end_line, len(lines))

    stripped_new = new_content.lstrip("\n")
    # ``append_entry`` always emits H2 for entry headings; other heading
    # levels in user input are treated as body content rather than a
    # header override, so only ``## `` triggers full-replacement.
    if stripped_new.lstrip().startswith("## "):
        # Full replacement — caller is overriding heading + header explicitly.
        replacement = new_content.splitlines()
    else:
        chunk_lines = lines[start_line - 1 : end_line]
        body_start = _find_body_start_index(chunk_lines)
        preserved = chunk_lines[:body_start]
        new_body_lines = stripped_new.splitlines()
        replacement = preserved + new_body_lines

    new_lines = lines[: start_line - 1] + replacement + lines[end_line:]
    result = "\n".join(new_lines)
    if trailing_newline:
        result += "\n"
    file_path.write_text(result, encoding="utf-8")


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
