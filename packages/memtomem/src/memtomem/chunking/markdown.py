"""Markdown chunker: splits by heading hierarchy, preserving context."""

from __future__ import annotations

import re
from pathlib import Path

from memtomem.models import Chunk, ChunkMetadata, ChunkType


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
_FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
# Code fence opener/closer: up to 3 leading spaces, then ``` or ~~~ (length is
# the matched run). Language tag after opener is allowed; closer must be the
# same character and at least as long (CommonMark §4.5).
_FENCE_OPEN_RE = re.compile(r"^[ ]{0,3}(`{3,}|~{3,})(.*)$")


_TOKEN_CHAR_RATIO = 4  # rough chars-per-token estimate (English-oriented)
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
# Line starting with a bold label (``**Label:**``, ``**Q:**``, ``**Added:**`` etc.).
# Used as a soft split boundary when paragraph splitting did not produce enough
# granularity — common in FAQ, changelog, and structured-note formats.
_BOLD_LABEL_RE = re.compile(r"^[ \t]*\*\*[^*\n]+\*\*", re.MULTILINE)


def _fence_line_set(text: str) -> frozenset[int]:
    """Return 1-indexed line numbers that sit *inside* a code fence (exclusive of
    the opener/closer lines themselves — those are marker lines that should not
    carry interior content like heading-looking strings).

    Opener and closer lines are also included so that a ``# heading``-shaped
    fence-marker metadata row is never treated as a true markdown heading.

    Handles unclosed fences at EOF by treating the rest of the file as fenced.
    """
    lines = text.splitlines()
    inside: set[int] = set()
    i = 0
    while i < len(lines):
        m = _FENCE_OPEN_RE.match(lines[i])
        if not m:
            i += 1
            continue
        run = m.group(1)
        fence_char = run[0]
        min_len = len(run)
        start = i
        j = i + 1
        while j < len(lines):
            m2 = _FENCE_OPEN_RE.match(lines[j])
            if m2:
                run2 = m2.group(1)
                if run2[0] == fence_char and len(run2) >= min_len and not m2.group(2).strip():
                    break
            j += 1
        end = j if j < len(lines) else len(lines) - 1
        for k in range(start, end + 1):
            inside.add(k + 1)  # 1-indexed
        i = end + 1
    return frozenset(inside)


def _split_paragraphs_fence_aware(text: str) -> list[str]:
    """Split *text* on blank lines, but keep each code fence as one atomic block.

    Equivalent to ``text.split("\\n\\n")`` when no fences are present. When a
    fence spans blank lines (e.g. code with empty lines inside), the entire
    fenced region — opener through closer — is emitted as a single part so the
    downstream merger cannot cut a code block in half.

    Unclosed fences at EOF absorb the rest of the text, matching the protective
    convention used by ``_fence_line_set``.
    """
    lines = text.splitlines(keepends=True)
    parts: list[str] = []
    buf: list[str] = []
    i = 0
    while i < len(lines):
        m = _FENCE_OPEN_RE.match(lines[i].rstrip("\n"))
        if m:
            # Flush current paragraph before the fence.
            if buf:
                joined = "".join(buf).strip("\n")
                if joined:
                    parts.append(joined)
                buf = []
            run = m.group(1)
            fence_char = run[0]
            min_len = len(run)
            start = i
            j = i + 1
            while j < len(lines):
                m2 = _FENCE_OPEN_RE.match(lines[j].rstrip("\n"))
                if (
                    m2
                    and m2.group(1)[0] == fence_char
                    and len(m2.group(1)) >= min_len
                    and not m2.group(2).strip()
                ):
                    break
                j += 1
            end = j if j < len(lines) else len(lines) - 1
            fence_text = "".join(lines[start : end + 1]).rstrip("\n")
            if fence_text:
                parts.append(fence_text)
            i = end + 1
            continue
        if lines[i].strip() == "":
            if buf:
                joined = "".join(buf).strip("\n")
                if joined:
                    parts.append(joined)
                buf = []
            i += 1
            continue
        buf.append(lines[i])
        i += 1
    if buf:
        joined = "".join(buf).strip("\n")
        if joined:
            parts.append(joined)
    return parts if parts else [text]


def _split_on_bold_labels(text: str) -> list[str]:
    """Split *text* before each bold-label line, returning a list of parts.

    Returns ``[text]`` unchanged when fewer than two bold-label boundaries
    are present, so single-label docs (e.g. one ``**Note:**`` in a prose
    section) stay intact.
    """
    positions = [m.start() for m in _BOLD_LABEL_RE.finditer(text)]
    if len(positions) < 2:
        return [text]
    parts: list[str] = []
    prev = 0
    for pos in positions:
        if pos <= prev:
            continue
        segment = text[prev:pos].rstrip()
        if segment:
            parts.append(segment)
        prev = pos
    tail = text[prev:].rstrip()
    if tail:
        parts.append(tail)
    return parts or [text]


class MarkdownChunker:
    def __init__(self, indexing_config=None):
        self._max_tokens = 512
        self._overlap_tokens = 0
        self._para_threshold = 800
        if indexing_config is not None:
            self._max_tokens = indexing_config.max_chunk_tokens
            self._overlap_tokens = indexing_config.chunk_overlap_tokens
            self._para_threshold = getattr(indexing_config, "paragraph_split_threshold", 800)

    def supported_extensions(self) -> frozenset[str]:
        return frozenset({".md", ".markdown"})

    def chunk_file(self, file_path: Path, content: str) -> list[Chunk]:
        if not content.strip():
            return []

        # Extract tags from YAML frontmatter
        fm_tags = self._extract_frontmatter_tags(content)

        # Resolve wikilinks: [[target|alias]] → alias, [[target]] → target
        content = _WIKILINK_RE.sub(lambda m: m.group(2) or m.group(1), content)

        sections = self._split_by_headings(content)
        chunks: list[Chunk] = []

        # Build file context: filename + all headings
        all_headings = [h for s in sections for h in s["hierarchy"]]
        file_ctx = f"{file_path.name}"
        if all_headings:
            unique = dict.fromkeys(all_headings)  # preserve order, dedup
            file_ctx += " > " + " | ".join(unique)

        for section in sections:
            text = section["text"].strip()
            if not text:
                continue

            hierarchy = section["hierarchy"]
            est_tokens = len(text) // _TOKEN_CHAR_RATIO

            # Parent context: parent heading text (if depth >= 2)
            parent_ctx = hierarchy[-2] if len(hierarchy) >= 2 else ""

            if est_tokens <= self._max_tokens:
                chunks.append(
                    Chunk(
                        content=text,
                        metadata=ChunkMetadata(
                            source_file=file_path,
                            heading_hierarchy=tuple(hierarchy),
                            chunk_type=ChunkType.MARKDOWN_SECTION,
                            start_line=section["start_line"],
                            end_line=section["end_line"],
                            tags=tuple(fm_tags),
                            parent_context=parent_ctx,
                            file_context=file_ctx,
                        ),
                    )
                )
            else:
                sub_chunks = self._split_section(text, section)
                for sc in sub_chunks:
                    chunks.append(
                        Chunk(
                            content=sc["text"],
                            metadata=ChunkMetadata(
                                source_file=file_path,
                                heading_hierarchy=tuple(hierarchy),
                                chunk_type=ChunkType.MARKDOWN_SECTION,
                                start_line=sc["start_line"],
                                end_line=sc["end_line"],
                                overlap_before=sc.get("overlap_before", 0),
                                overlap_after=sc.get("overlap_after", 0),
                                tags=tuple(fm_tags),
                                parent_context=parent_ctx,
                                file_context=file_ctx,
                            ),
                        )
                    )

        return chunks

    @staticmethod
    def _extract_frontmatter_tags(content: str) -> list[str]:
        """Extract tags from YAML frontmatter if present."""
        match = _FRONT_MATTER_RE.match(content)
        if not match:
            return []
        fm_text = match.group(1)
        # Parse tags line: "tags: [a, b, c]" or "tags:\n  - a\n  - b"
        for line in fm_text.splitlines():
            stripped = line.strip()
            if stripped.startswith("tags:"):
                value = stripped[5:].strip()
                if value.startswith("[") and value.endswith("]"):
                    # Inline list: tags: [project, api, backend]
                    return [t.strip().strip("'\"") for t in value[1:-1].split(",") if t.strip()]
                elif value:
                    # Single value: tags: sometag
                    return [value.strip("'\"")]
                else:
                    # Block list: tags:\n  - a\n  - b
                    tags = []
                    for next_line in fm_text.splitlines()[fm_text.splitlines().index(line) + 1 :]:
                        ns = next_line.strip()
                        if ns.startswith("- "):
                            tags.append(ns[2:].strip().strip("'\""))
                        elif ns and not ns.startswith("#"):
                            break
                    return tags
        return []

    def _split_section(self, text: str, section: dict) -> list[dict]:
        """Split an oversized section by paragraphs or sentences."""
        max_chars = self._max_tokens * _TOKEN_CHAR_RATIO
        overlap_chars = self._overlap_tokens * _TOKEN_CHAR_RATIO
        base_line = section["start_line"]

        # Try paragraph-level splitting first. Fence-aware so code blocks
        # (including ones with blank lines inside) stay atomic — otherwise the
        # size-based merger below could slice a ``` block mid-code.
        est_tokens = len(text) // _TOKEN_CHAR_RATIO
        if est_tokens >= self._para_threshold:
            parts = _split_paragraphs_fence_aware(text)
        else:
            parts = [text]

        # Bold-label soft boundary: ``**Label:**``-prefixed lines mark
        # pseudo-headings (FAQ, changelog entries, structured notes).
        # Try this before falling through to sentence split so the
        # natural structure survives.
        if len(parts) == 1 and len(parts[0]) > max_chars:
            bold_parts = _split_on_bold_labels(text)
            if len(bold_parts) > 1:
                parts = bold_parts

        # Last resort: split by sentences. Skipped entirely when the whole
        # section is inside one fenced block — sentence splitting a code
        # block would mangle it. The block is accepted as oversize instead.
        if (
            len(parts) == 1
            and len(parts[0]) > max_chars
            and not _FENCE_OPEN_RE.match(parts[0].lstrip("\n").splitlines()[0] if parts[0] else "")
        ):
            parts = _SENTENCE_RE.split(text)

        # Merge small parts into chunks respecting max_chars
        result: list[dict] = []
        current = ""
        current_start = base_line
        line_offset = 0

        for part in parts:
            if current and len(current) + len(part) + 2 > max_chars:
                result.append(
                    {
                        "text": current.strip(),
                        "start_line": current_start,
                        "end_line": base_line + line_offset - 1,
                    }
                )
                # Apply overlap
                if overlap_chars > 0:
                    overlap_text = current[-overlap_chars:]
                    current = overlap_text + "\n\n" + part
                else:
                    current = part
                current_start = base_line + line_offset
            else:
                if current:
                    current += "\n\n" + part
                else:
                    current = part
            line_offset += part.count("\n") + 2

        if current.strip():
            result.append(
                {
                    "text": current.strip(),
                    "start_line": current_start,
                    "end_line": section["end_line"],
                }
            )

        # Mark overlap
        for i, r in enumerate(result):
            r["overlap_before"] = overlap_chars if i > 0 and overlap_chars > 0 else 0
            r["overlap_after"] = overlap_chars if i < len(result) - 1 and overlap_chars > 0 else 0

        return (
            result
            if result
            else [
                {"text": text, "start_line": section["start_line"], "end_line": section["end_line"]}
            ]
        )

    def _split_by_headings(self, content: str) -> list[dict]:
        lines = content.splitlines()
        fence_lines = _fence_line_set(content)
        sections: list[dict] = []
        current_hierarchy: list[str] = []
        current_lines: list[str] = []
        current_start = 1

        for i, line in enumerate(lines, 1):
            match = _HEADING_RE.match(line) if i not in fence_lines else None
            if match:
                # Flush previous section
                if current_lines:
                    sections.append(
                        {
                            "hierarchy": list(current_hierarchy),
                            "text": "\n".join(current_lines),
                            "start_line": current_start,
                            "end_line": i - 1,
                        }
                    )

                level = len(match.group(1))
                heading_text = match.group(2).strip()
                heading_full = f"{'#' * level} {heading_text}"

                # Update hierarchy: trim to current level, then append
                current_hierarchy = [
                    h for h in current_hierarchy if len(h.split(" ", 1)[0]) < level
                ]
                current_hierarchy.append(heading_full)

                current_lines = []
                current_start = i
            else:
                current_lines.append(line)

        # Flush last section
        if current_lines:
            sections.append(
                {
                    "hierarchy": list(current_hierarchy),
                    "text": "\n".join(current_lines),
                    "start_line": current_start,
                    "end_line": len(lines),
                }
            )

        # If no headings found, return the whole content as one chunk
        if not sections and content.strip():
            sections.append(
                {
                    "hierarchy": [],
                    "text": content,
                    "start_line": 1,
                    "end_line": len(lines),
                }
            )

        return sections
