"""Chunker protocol and the line convention chunkers share."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from memtomem.models import Chunk


class Chunker(Protocol):
    def supported_extensions(self) -> frozenset[str]: ...
    def chunk_file(self, file_path: Path, content: str) -> list[Chunk]: ...


def split_source_lines(text: str, *, keepends: bool = False) -> list[str]:
    """Split *text* into the lines a chunk's ``start_line``/``end_line`` count.

    A line ends at ``\\n`` only, as editors, tree-sitter rows, ``Chunk.line_map``
    and the web source view count them. ``str.splitlines`` also ends lines at
    U+2028, U+2029, U+0085, ``\\x0b``, ``\\x0c``, ``\\x1c``-``\\x1e`` and a lone
    ``\\r``, so a file holding one of those got every later range placed low and
    the character rewritten to ``\\n`` in chunk content. For LF and CRLF text the
    result equals ``text.splitlines(keepends=keepends)``; files where the two
    disagree are kept read-only by ``IndexEngine.chunk_content``.
    """
    if not text:
        return []
    lines = [line + "\n" for line in text.split("\n")]
    if text.endswith("\n"):
        lines.pop()
    else:
        lines[-1] = lines[-1][:-1]
    if keepends:
        return lines
    return [line.removesuffix("\n").removesuffix("\r") for line in lines]


def physical_line_numbers(text: str) -> list[int]:
    """Map each ``text.splitlines()`` line to the 1-based ``\\n`` line it starts on.

    For parsers that read the extra separators as line breaks (PyYAML does), so
    their logical lines can be reported in the numbering of
    :func:`split_source_lines`.
    """
    numbers: list[int] = []
    line = 1
    for logical in text.splitlines(keepends=True):
        numbers.append(line)
        line += logical.count("\n")
    return numbers
