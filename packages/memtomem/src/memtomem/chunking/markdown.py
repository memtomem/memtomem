"""Markdown chunker: splits by heading hierarchy, preserving context."""

from __future__ import annotations

import re
from pathlib import Path

from memtomem.models import Chunk, ChunkMetadata, ChunkType


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
_FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")


_TOKEN_CHAR_RATIO = 3  # rough chars-per-token estimate
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


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

        for section in sections:
            text = section["text"].strip()
            if not text:
                continue

            hierarchy = section["hierarchy"]
            est_tokens = len(text) // _TOKEN_CHAR_RATIO

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

        # Try paragraph-level splitting first
        est_tokens = len(text) // _TOKEN_CHAR_RATIO
        if est_tokens >= self._para_threshold:
            parts = text.split("\n\n")
        else:
            parts = [text]

        # If paragraph splitting didn't help enough, split by sentences
        if len(parts) == 1 and len(parts[0]) > max_chars:
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
        sections: list[dict] = []
        current_hierarchy: list[str] = []
        current_lines: list[str] = []
        current_start = 1

        for i, line in enumerate(lines, 1):
            match = _HEADING_RE.match(line)
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
