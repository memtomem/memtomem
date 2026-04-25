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
            raw_section_text = section["text"]
            text = raw_section_text.strip()
            if not text:
                continue

            # Track lines stripped from the front of section content while
            # reaching the body. Used by ``_split_section`` so an oversized
            # section's sub-chunks 2..N report ``start_line`` aligned with
            # the actual file lines they cover, instead of pointing K lines
            # too early (where K is the heading + blockquote header span).
            leading_strip_lines = raw_section_text[
                : len(raw_section_text) - len(raw_section_text.lstrip())
            ].count("\n")

            # Per-entry metadata blockquote (``> created: ...`` / ``> tags:
            # [...]``) is promoted to ``metadata.tags`` and stripped from
            # the chunk content so it doesn't leak into BM25/embedding.
            section_tags, text, blockquote_strip_lines = self._extract_section_blockquote_tags(text)
            if not text.strip():
                continue
            combined_tags = tuple(sorted(set(fm_tags) | set(section_tags)))

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
                            tags=combined_tags,
                            parent_context=parent_ctx,
                            file_context=file_ctx,
                        ),
                    )
                )
            else:
                # Body offset from the heading line: 1 (heading itself) +
                # blank lines stripped by .strip() before the blockquote +
                # blockquote group + trailing blanks. ``_split_section``
                # uses this to seed its internal line counter so sub-chunk
                # boundaries map back to real file lines.
                body_offset = 1 + leading_strip_lines + blockquote_strip_lines
                sub_chunks = self._split_section(text, section, body_offset=body_offset)
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
                                tags=combined_tags,
                                parent_context=parent_ctx,
                                file_context=file_ctx,
                            ),
                        )
                    )

        return chunks

    @staticmethod
    def _parse_tags_value(value: str, trailing_lines: list[str]) -> list[str]:
        """Parse a ``tags:`` value across the four shapes we accept.

        - Inline list: ``["a", "b"]`` / ``['a', 'b']`` / ``[a, b]``
        - Single bare value: ``mytag``
        - Block list: empty ``value`` followed by ``- item`` lines in
          ``trailing_lines`` (any leading ``> `` should already have been
          stripped by the caller).

        Returns ``[]`` when nothing parses.
        """
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            return [t.strip().strip("'\"") for t in value[1:-1].split(",") if t.strip()]
        if value:
            return [value.strip("'\"")]
        # Block list shape — walk trailing lines until a non-``- `` line.
        tags: list[str] = []
        for raw in trailing_lines:
            ns = raw.strip()
            if ns.startswith("- "):
                tags.append(ns[2:].strip().strip("'\""))
            elif ns and not ns.startswith("#"):
                break
        return tags

    @classmethod
    def _extract_frontmatter_tags(cls, content: str) -> list[str]:
        """Extract tags from YAML frontmatter if present."""
        match = _FRONT_MATTER_RE.match(content)
        if not match:
            return []
        fm_lines = match.group(1).splitlines()
        for idx, line in enumerate(fm_lines):
            stripped = line.strip()
            if stripped.startswith("tags:"):
                return cls._parse_tags_value(stripped[5:], fm_lines[idx + 1 :])
        return []

    @classmethod
    def _extract_section_blockquote_tags(cls, text: str) -> tuple[list[str], str, int]:
        """Detect a section-leading blockquote group, extract ``tags:`` from it.

        ``mem_add`` writes per-entry metadata as a blockquote header
        (``> created: ...`` plus an optional ``tags:`` line). The chunker
        promotes that ``tags:`` value into ``ChunkMetadata.tags`` so
        ``mem_search(tag_filter=...)`` can match. The header itself is
        stripped from the returned text so it does not leak into BM25 or
        embedding inputs.

        Section-leading only: the blockquote must be the first non-blank
        block in *text*. Blockquotes that appear mid-section (a quoted
        paragraph in body prose) are left untouched, even if they happen
        to contain a ``tags:`` line.

        Recognises both shapes the writer has emitted:
        - Canonical: every line starts with ``> `` (post-RFC writer).
        - Legacy lazy-continuation: a ``> created:`` line followed by a
          bare ``tags: [...]`` line (CommonMark glues them into one
          blockquote at render time, and the ``tags`` line carries no
          ``> `` prefix on disk).

        Returns a triple ``(tags, text_after_strip, lines_consumed)``:

        - On a no-hit case (no leading blockquote, or the blockquote
          contains no ``tags:`` key) → ``([], text, 0)``: input unchanged.
        - On a hit → ``(tags, stripped_text, n)`` where *n* is the count
          of input lines that were stripped from the front of *text* to
          reach *stripped_text* (blockquote group + trailing blank
          lines). Callers that track file-line offsets — notably
          ``chunk_file`` calling ``_split_section`` for an oversized
          section — use *n* to keep sub-chunk ``start_line`` /
          ``end_line`` aligned with the source file after the strip.
        """
        lines = text.splitlines()
        # Skip leading blank lines (text is usually .strip()ed already, but
        # be defensive — _split_section can pass non-stripped chunks).
        i = 0
        while i < len(lines) and not lines[i].strip():
            i += 1
        if i == len(lines) or not lines[i].lstrip().startswith(">"):
            return [], text, 0

        # Collect the contiguous blockquote group, including lazy-continuation
        # lines: a non-blank, non-``>``-prefixed line that immediately follows
        # a ``>``-prefixed line without an intervening blank line.
        block_inner: list[str] = []
        while i < len(lines):
            line = lines[i]
            stripped = line.lstrip()
            if not stripped:
                break
            if stripped.startswith(">"):
                # Strip leading ``>`` and one optional space.
                inner = stripped[1:]
                if inner.startswith(" "):
                    inner = inner[1:]
                block_inner.append(inner)
                i += 1
                continue
            # Non-blank, non-``>``: lazy continuation only if we already
            # collected at least one ``>`` line. Otherwise this is body text
            # that just happens to start the section, not a blockquote.
            if block_inner:
                block_inner.append(stripped)
                i += 1
                continue
            break

        # Find a ``tags:`` key inside the block (case-sensitive).
        tags: list[str] = []
        for idx, inner in enumerate(block_inner):
            inner_stripped = inner.strip()
            if inner_stripped.startswith("tags:"):
                tags = cls._parse_tags_value(inner_stripped[5:], block_inner[idx + 1 :])
                break

        if not tags:
            return [], text, 0

        # Strip the blockquote group plus immediately-trailing blank lines.
        rest_start = i
        while rest_start < len(lines) and not lines[rest_start].strip():
            rest_start += 1
        return tags, "\n".join(lines[rest_start:]), rest_start

    def _split_section(self, text: str, section: dict, *, body_offset: int = 0) -> list[dict]:
        """Split an oversized section by paragraphs or sentences.

        ``body_offset`` is the number of file lines between
        ``section["start_line"]`` (the heading) and the first line of
        ``text``. Callers that strip a header (``> created:`` /
        ``> tags:`` blockquote) from *text* before calling here pass the
        stripped line count so ``start_line`` / ``end_line`` on the
        emitted sub-chunks line up with the file. With ``body_offset=0``
        the math reduces to the pre-strip behaviour.
        """
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
        # Seed the line counter with ``body_offset`` so file-line math is
        # right after the caller stripped a header. Sub-chunk 1 keeps
        # ``current_start = base_line`` (heading) until the first
        # boundary; subsequent sub-chunks pick up
        # ``base_line + line_offset`` and inherit the seed.
        line_offset = body_offset

        for part in parts:
            if current and len(current) + len(part) + 2 > max_chars:
                # ``line_offset`` was just incremented past the previous
                # part *plus* its trailing ``\n\n`` separator (the ``+2``
                # below). Subtract that separator so ``end_line`` lands
                # on the part's last content line, not on the blank line
                # between paragraphs — otherwise ``mem_edit``'s
                # ``replace_chunk_body`` would absorb the gap on save.
                result.append(
                    {
                        "text": current.strip(),
                        "start_line": current_start,
                        "end_line": base_line + line_offset - 2,
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
