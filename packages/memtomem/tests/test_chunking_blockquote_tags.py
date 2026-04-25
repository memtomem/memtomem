"""Section-leading blockquote ``tags:`` promotion (mem_add per-entry tags).

``mem_add`` writes per-entry metadata as a leading blockquote header
(``> created: ...`` plus an optional ``tags:`` line). The chunker must
promote that ``tags:`` value into ``ChunkMetadata.tags`` so
``mem_search(tag_filter=...)`` matches, and strip the header from chunk
content so it does not leak into BM25 / embedding inputs. See
``planning/mem-add-tags-blockquote-promote-rfc.md``.
"""

from pathlib import Path

from memtomem.chunking.markdown import MarkdownChunker


def _chunk(content: str):
    return MarkdownChunker().chunk_file(Path("/test.md"), content)


class TestSectionBlockquoteTags:
    def test_canonical_explicit_blockquote_form(self):
        """Post-RFC writer: every line carries ``> ``."""
        content = (
            "## Cache strategy\n"
            "\n"
            "> created: 2026-04-24T22:12:30+00:00\n"
            '> tags: ["cache", "shared-from=abc"]\n'
            "\n"
            "Use redis with a 5-minute TTL.\n"
        )
        chunks = _chunk(content)
        assert len(chunks) == 1
        assert set(chunks[0].metadata.tags) == {"cache", "shared-from=abc"}
        # Header must be stripped from chunk content.
        assert "tags:" not in chunks[0].content
        assert "created:" not in chunks[0].content
        assert "Use redis" in chunks[0].content

    def test_legacy_lazy_continuation_form(self):
        """Pre-RFC writer: ``tags:`` line has no ``> `` prefix; lazy continuation."""
        content = (
            "## Cache strategy\n"
            "\n"
            "> created: 2026-04-24T22:12:30+00:00\n"
            "tags: ['cache', 'shared-from=abc']\n"
            "\n"
            "Use redis with a 5-minute TTL.\n"
        )
        chunks = _chunk(content)
        assert len(chunks) == 1
        assert set(chunks[0].metadata.tags) == {"cache", "shared-from=abc"}
        assert "tags:" not in chunks[0].content
        assert "Use redis" in chunks[0].content

    def test_frontmatter_and_blockquote_compose_via_union(self):
        """File-level frontmatter tags merge with per-section blockquote tags."""
        content = (
            "---\n"
            "tags: [project, api]\n"
            "---\n"
            "\n"
            "## Cache\n"
            "\n"
            '> tags: ["cache"]\n'
            "\n"
            "Body text.\n"
            "\n"
            "## Auth\n"
            "\n"
            "Different section, frontmatter only.\n"
        )
        chunks = _chunk(content)
        # Locate by heading: chunker emits a no-heading frontmatter chunk
        # too, which the indexing engine later merges. We only care about
        # the per-section chunks here.
        cache_chunk = next(
            c
            for c in chunks
            if c.metadata.heading_hierarchy and "Cache" in c.metadata.heading_hierarchy[0]
        )
        auth_chunk = next(
            c
            for c in chunks
            if c.metadata.heading_hierarchy and "Auth" in c.metadata.heading_hierarchy[0]
        )
        # Cache section gets union: frontmatter + blockquote.
        assert set(cache_chunk.metadata.tags) == {"project", "api", "cache"}
        # Auth section keeps frontmatter only — section-level tags do not bleed.
        assert set(auth_chunk.metadata.tags) == {"project", "api"}

    def test_mid_section_blockquote_is_not_promoted(self):
        """A blockquote that appears after body text is left alone."""
        content = (
            "## Notes\n"
            "\n"
            "Some body prose first.\n"
            "\n"
            "> tags: ['leaked', 'must-not-promote']\n"
            "\n"
            "More body after the quoted block.\n"
        )
        chunks = _chunk(content)
        assert len(chunks) == 1
        # No promotion: section is not blockquote-led.
        assert chunks[0].metadata.tags == ()
        # Mid-section blockquote stays in content.
        assert "tags:" in chunks[0].content
        assert "leaked" in chunks[0].content

    def test_section_leading_blockquote_without_tags_is_noop(self):
        """``> created:`` only — no ``tags:`` line — leaves chunk untouched."""
        content = "## Heading\n\n> created: 2026-04-24T22:12:30+00:00\n\nBody text.\n"
        chunks = _chunk(content)
        assert len(chunks) == 1
        assert chunks[0].metadata.tags == ()
        # Without a tags hit we leave the blockquote in content as-is —
        # callers may still want to render the ``created:`` line.
        assert "created:" in chunks[0].content
        assert "Body text" in chunks[0].content

    def test_legacy_repr_form_strip_handles_both_quote_styles(self):
        """Legacy ``repr(list)`` output uses single quotes; canonical uses double."""
        content_single = "## A\n\n> tags: ['x', 'y']\n\nbody\n"
        content_double = '## A\n\n> tags: ["x", "y"]\n\nbody\n'
        chunks_single = _chunk(content_single)
        chunks_double = _chunk(content_double)
        assert set(chunks_single[0].metadata.tags) == {"x", "y"}
        assert set(chunks_double[0].metadata.tags) == {"x", "y"}

    def test_block_list_shape_in_blockquote(self):
        """``> tags:`` followed by ``> - a`` / ``> - b`` block-list lines.

        The current writer never emits this shape, but the shared
        ``_parse_tags_value`` helper supports it via the same code path
        as YAML frontmatter block lists. Lock it in so reusing the
        helper doesn't regress.
        """
        content = "## Block-list section\n\n> tags:\n> - alpha\n> - beta\n\nBody paragraph.\n"
        chunks = _chunk(content)
        assert len(chunks) == 1
        assert set(chunks[0].metadata.tags) == {"alpha", "beta"}
        assert "tags:" not in chunks[0].content
        assert "Body paragraph." in chunks[0].content


class _SmallChunkConfig:
    """Tiny token budget so any prose triggers `_split_section`."""

    max_chunk_tokens = 30
    chunk_overlap_tokens = 0
    paragraph_split_threshold = 20


class TestOversizeSectionLineDrift:
    """Sub-chunks of an oversize section that begins with a blockquote
    header must report ``start_line`` / ``end_line`` aligned with the
    actual file lines they cover.

    Pre-fix the chunker stripped the blockquote (~3 lines for
    ``> created`` + ``> tags`` + trailing blank) before passing text to
    ``_split_section`` while still seeding ``base_line`` from the
    heading line. Sub-chunk 2..N's reported ``start_line`` therefore
    pointed K lines earlier than the actual body — which silently
    dropped K real body lines on a subsequent ``mem_edit`` of a non-
    first sub-chunk. See ``planning/mem-add-tags-blockquote-promote-rfc.md``
    §Follow-ups #2.
    """

    def test_blockquote_header_does_not_drag_sub_chunk_start_lines(self):
        """Sub-chunk 2's start_line must land at or after the body."""
        # Layout (line numbers in the source string):
        # 1  ## Big section
        # 2  (blank)
        # 3  > created: 2026-04-25
        # 4  > tags: ["alpha"]
        # 5  (blank)
        # 6  Para 1 ...           ← body actually starts here
        # 7  (blank)
        # 8  Para 2 ...
        # 9  (blank)
        # 10 Para 3 ...
        para1 = ("First paragraph words " * 12).strip()
        para2 = ("Second paragraph words " * 12).strip()
        para3 = ("Third paragraph words " * 12).strip()
        content = (
            "## Big section\n"
            "\n"
            "> created: 2026-04-25\n"
            '> tags: ["alpha"]\n'
            "\n"
            f"{para1}\n"
            "\n"
            f"{para2}\n"
            "\n"
            f"{para3}\n"
        )
        chunker = MarkdownChunker(indexing_config=_SmallChunkConfig())
        chunks = chunker.chunk_file(Path("/test.md"), content)

        assert len(chunks) >= 2, "test setup must trigger _split_section"
        # All sub-chunks inherit the section's blockquote tag.
        for c in chunks:
            assert "alpha" in c.metadata.tags

        # First sub-chunk anchors at the heading line — preserves
        # mem_edit's existing convention (heading + header preserved
        # via _find_body_start_index when editing).
        assert chunks[0].metadata.start_line == 1

        # Critical invariant: subsequent sub-chunks must NOT point into
        # the blockquote header range (lines 1..5). Body starts at
        # line 6.
        for c in chunks[1:]:
            assert c.metadata.start_line >= 6, (
                f"sub-chunk start_line {c.metadata.start_line} drifted into the "
                "stripped header range (heading + blockquote = lines 1..5); "
                "mem_edit of this sub-chunk would consume real body lines."
            )

    def test_oversize_section_without_blockquote_unchanged(self):
        """No header to strip → ``body_offset = 0``: pre-PR-A behavior."""
        para1 = ("First paragraph words " * 12).strip()
        para2 = ("Second paragraph words " * 12).strip()
        # Layout:
        # 1  ## Plain
        # 2  (blank)
        # 3  para1
        # 4  (blank)
        # 5  para2
        content = f"## Plain\n\n{para1}\n\n{para2}\n"
        chunker = MarkdownChunker(indexing_config=_SmallChunkConfig())
        chunks = chunker.chunk_file(Path("/test.md"), content)
        assert len(chunks) >= 2
        # No tags promoted — there was no blockquote.
        for c in chunks:
            assert c.metadata.tags == ()
        # First sub-chunk still anchored at heading.
        assert chunks[0].metadata.start_line == 1

    def test_oversize_section_with_no_tags_blockquote_keeps_header_inline(self):
        """``> created:`` blockquote without ``tags:`` is left in place.

        ``_extract_section_blockquote_tags`` short-circuits when no
        ``tags:`` key is found and returns ``([], text, 0)`` — so the
        blockquote stays in the chunk content, ``blockquote_strip_lines``
        is 0, and ``body_offset`` reduces to ``1 + leading_strip_lines``.
        Sub-chunks 2..N must still align with the file (no drift), and
        the ``> created:`` line must stay in the first sub-chunk's
        content so ``mem_edit`` can preserve it on a body-only edit.
        """
        para1 = ("First paragraph words " * 12).strip()
        para2 = ("Second paragraph words " * 12).strip()
        # Layout:
        # 1  ## Note
        # 2  (blank)
        # 3  > created: 2026-04-25T12:00:00+00:00
        # 4  (blank)
        # 5  para1                          ← body starts here
        # 6  (blank)
        # 7  para2
        content = f"## Note\n\n> created: 2026-04-25T12:00:00+00:00\n\n{para1}\n\n{para2}\n"
        chunker = MarkdownChunker(indexing_config=_SmallChunkConfig())
        chunks = chunker.chunk_file(Path("/test.md"), content)
        assert len(chunks) >= 2
        # No tags key → no promotion.
        for c in chunks:
            assert c.metadata.tags == ()
        # First sub-chunk anchors at heading; ``> created:`` survives in
        # its content because the no-tags branch leaves the blockquote
        # alone (mem_edit's ``_find_body_start_index`` still recognises
        # it on a body-only edit).
        assert chunks[0].metadata.start_line == 1
        assert "> created:" in chunks[0].content
        # No drift: subsequent sub-chunks must land at or after the body
        # (line 5), not inside the heading / blockquote / blank range.
        for c in chunks[1:]:
            assert c.metadata.start_line >= 5


class TestOversizeSectionParagraphGap:
    """Closed-out sub-chunks (every chunk except the trailing one, which
    inherits ``section['end_line']`` directly) must report ``end_line``
    on the actual last content line of their paragraphs — not on the
    blank separator that follows. See
    ``planning/mem-add-tags-blockquote-promote-rfc.md`` §Follow-ups #5.

    Pre-fix the line counter advanced by ``count('\\n') + 2`` per part
    where the ``+2`` covered the ``\\n\\n`` join separator. When closing
    a chunk the formula then read ``base_line + line_offset - 1``, which
    landed one line past the paragraph (on the blank). ``mem_edit``'s
    ``replace_chunk_body`` reads ``lines[start..end]`` inclusive, so the
    overstated ``end_line`` pulled the next paragraph's blank separator
    into the replacement on save — collapsing the paragraph gap.
    Cosmetic, not data-loss.
    """

    def test_plain_section_sub_chunk_end_lines_skip_blank_separator(self):
        """Three single-line paragraphs → three sub-chunks, exact lines.

        Layout (line numbers in the source string):
          1  ## Plain
          2  (blank)
          3  para1                ← chunk[0] body
          4  (blank)
          5  para2                ← chunk[1] body
          6  (blank)
          7  para3                ← chunk[2] body
        """
        para1 = ("First paragraph words " * 12).strip()
        para2 = ("Second paragraph words " * 12).strip()
        para3 = ("Third paragraph words " * 12).strip()
        content = f"## Plain\n\n{para1}\n\n{para2}\n\n{para3}\n"
        chunker = MarkdownChunker(indexing_config=_SmallChunkConfig())
        chunks = chunker.chunk_file(Path("/test.md"), content)
        assert len(chunks) == 3, "test setup must produce exactly 3 sub-chunks"

        # chunk[0]: heading-anchored at line 1; body is para1 (line 3).
        # end_line must be 3, not 4 — line 4 is the blank separator
        # before para2 and belongs to no chunk.
        assert chunks[0].metadata.start_line == 1
        assert chunks[0].metadata.end_line == 3

        # chunk[1]: para2 (line 5). end_line must be 5, not 6.
        assert chunks[1].metadata.start_line == 5
        assert chunks[1].metadata.end_line == 5

        # chunk[2]: trailing sub-chunk inherits section["end_line"] (= 7),
        # which is the last content line. Asserted for completeness.
        assert chunks[2].metadata.start_line == 7
        assert chunks[2].metadata.end_line == 7

    def test_blockquote_section_sub_chunk_end_lines_skip_blank_separator(self):
        """Same exact-alignment invariant when ``body_offset`` is non-zero.

        Layout:
          1  ## Big section
          2  (blank)
          3  > created: 2026-04-25
          4  > tags: ["alpha"]
          5  (blank)
          6  para1               ← chunk[0] body (heading-anchored)
          7  (blank)
          8  para2               ← chunk[1] body
          9  (blank)
          10 para3               ← chunk[2] body
        """
        para1 = ("First paragraph words " * 12).strip()
        para2 = ("Second paragraph words " * 12).strip()
        para3 = ("Third paragraph words " * 12).strip()
        content = (
            "## Big section\n"
            "\n"
            "> created: 2026-04-25\n"
            '> tags: ["alpha"]\n'
            "\n"
            f"{para1}\n"
            "\n"
            f"{para2}\n"
            "\n"
            f"{para3}\n"
        )
        chunker = MarkdownChunker(indexing_config=_SmallChunkConfig())
        chunks = chunker.chunk_file(Path("/test.md"), content)
        assert len(chunks) == 3

        # chunk[0]: heading-anchored at 1; body para1 ends at line 6.
        # end_line must be 6, not 7 (line 7 is the blank between para1
        # and para2).
        assert chunks[0].metadata.start_line == 1
        assert chunks[0].metadata.end_line == 6

        # chunk[1]: para2 at line 8. end_line must be 8, not 9.
        assert chunks[1].metadata.start_line == 8
        assert chunks[1].metadata.end_line == 8

        # chunk[2]: trailing inherits section end (line 10).
        assert chunks[2].metadata.start_line == 10
        assert chunks[2].metadata.end_line == 10
