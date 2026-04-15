"""ReStructuredText chunker: splits by section headers."""

from __future__ import annotations

import re
from pathlib import Path

from memtomem.models import Chunk, ChunkMetadata, ChunkType

# RST adornment characters (any non-alphanumeric printable ASCII)
_ADORNMENT_RE = re.compile(r"^([=\-~^\"'+#*:.`_!])\1+$")


class ReStructuredTextChunker:
    """Split .rst files on section header boundaries.

    RST marks sections with underline (and optional overline) characters.
    Heading level is determined by order of first appearance of each
    adornment character — there is no fixed mapping like Markdown's ``#``.
    """

    def supported_extensions(self) -> frozenset[str]:
        return frozenset({".rst"})

    def chunk_file(self, file_path: Path, content: str) -> list[Chunk]:
        if not content.strip():
            return []

        sections = self._split_by_headings(content)
        chunks: list[Chunk] = []

        # Build file context: filename + all headings
        all_headings = [h for s in sections for h in s["hierarchy"]]
        file_ctx = f"{file_path.name}"
        if all_headings:
            unique = dict.fromkeys(all_headings)
            file_ctx += " > " + " | ".join(unique)

        for section in sections:
            text = section["text"].strip()
            if not text:
                continue

            hierarchy = section["hierarchy"]
            parent_ctx = hierarchy[-2] if len(hierarchy) >= 2 else ""

            chunks.append(
                Chunk(
                    content=text,
                    metadata=ChunkMetadata(
                        source_file=file_path,
                        heading_hierarchy=tuple(hierarchy),
                        chunk_type=ChunkType.RST_SECTION,
                        start_line=section["start_line"],
                        end_line=section["end_line"],
                        parent_context=parent_ctx,
                        file_context=file_ctx,
                    ),
                )
            )

        return chunks

    def _split_by_headings(self, content: str) -> list[dict]:
        lines = content.splitlines()
        # Detect section headers and their levels.
        # level_order maps adornment character to its depth (0-indexed).
        level_order: dict[tuple[str, bool], int] = {}
        headers: list[tuple[int, str, int]] = []  # (line_idx, title, depth)

        for i, line in enumerate(lines):
            if not _ADORNMENT_RE.match(line):
                continue
            char = line[0]
            adorn_len = len(line.rstrip())

            # Check for title on the line above the adornment
            if i > 0 and lines[i - 1].strip():
                title = lines[i - 1].strip()
                if adorn_len >= len(title):
                    # Check for optional overline (line above title)
                    has_overline = bool(
                        i >= 2 and _ADORNMENT_RE.match(lines[i - 2]) and lines[i - 2][0] == char
                    )
                    key = (char, has_overline)
                    if key not in level_order:
                        level_order[key] = len(level_order)
                    depth = level_order[key]
                    # header_start is the overline (if present) or the title line
                    header_start = i - 2 if has_overline else i - 1
                    headers.append((header_start, title, depth))

        if not headers:
            # No headings: return whole content as one chunk
            return [
                {
                    "hierarchy": [],
                    "text": content,
                    "start_line": 1,
                    "end_line": len(lines),
                }
            ]

        sections: list[dict] = []
        current_hierarchy: list[tuple[int, str]] = []

        # Content before first header
        if headers[0][0] > 0:
            pre = "\n".join(lines[: headers[0][0]])
            if pre.strip():
                sections.append(
                    {
                        "hierarchy": [],
                        "text": pre,
                        "start_line": 1,
                        "end_line": headers[0][0],
                    }
                )

        for idx, (hdr_start, title, depth) in enumerate(headers):
            # Update hierarchy: keep only levels shallower than current
            current_hierarchy = [h for h in current_hierarchy if h[0] < depth]
            current_hierarchy.append((depth, title))

            # Section body starts after the underline
            # Find the underline: it's either hdr_start+1 (no overline) or hdr_start+2 (overline)
            body_start = hdr_start + 2  # title + underline
            if hdr_start >= 1 and _ADORNMENT_RE.match(lines[hdr_start]):
                body_start = hdr_start + 3  # overline + title + underline

            if idx + 1 < len(headers):
                body_end = headers[idx + 1][0]
            else:
                body_end = len(lines)

            body = "\n".join(lines[body_start:body_end])
            hierarchy_strs = [h[1] for h in current_hierarchy]

            sections.append(
                {
                    "hierarchy": list(hierarchy_strs),
                    "text": f"{title}\n{body}" if body.strip() else title,
                    "start_line": hdr_start + 1,  # 1-indexed
                    "end_line": body_end,
                }
            )

        return sections
