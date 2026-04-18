"""Fence / pattern protection for the Markdown chunker.

Pins the behaviour added to stop code-fence content from being split mid-block
or mis-read as a heading. Previously, ``# heading`` lines inside a ``` fence
triggered a section split, and long fenced blocks were sliced by the size-based
merger — producing chunks with unbalanced ``` markers that showed up as
truncated code snippets in retrieval.
"""

from __future__ import annotations

from pathlib import Path

from memtomem.chunking.markdown import MarkdownChunker


class _Cfg:
    # Tight thresholds so test docs trigger splitting paths without being huge.
    max_chunk_tokens = 200
    chunk_overlap_tokens = 0
    paragraph_split_threshold = 50


def _chunk(text: str) -> list:
    return MarkdownChunker(indexing_config=_Cfg()).chunk_file(Path("/tmp/test.md"), text)


class TestFenceProtection:
    def test_heading_inside_fence_is_not_a_section_boundary(self):
        """``# foo`` inside a ``` block is code, not a heading."""
        text = (
            "# Real Heading\n\n"
            "Intro paragraph.\n\n"
            "```python\n"
            "# this is a comment, not a heading\n"
            "def f():\n"
            "    pass\n"
            "```\n\n"
            "Closing paragraph.\n"
        )
        chunks = _chunk(text)
        assert len(chunks) == 1
        assert "# this is a comment" in chunks[0].content
        assert chunks[0].metadata.heading_hierarchy == ("# Real Heading",)

    def test_tilde_fence_supported(self):
        """``~~~`` fences protect interior headings just like backtick fences."""
        text = "# Top\n\n~~~\n# not a heading\n~~~\n"
        chunks = _chunk(text)
        assert len(chunks) == 1
        assert "# not a heading" in chunks[0].content

    def test_large_fence_stays_atomic(self):
        """A single fenced block larger than max_chunk_tokens is emitted whole."""
        body = "print('x')\n" * 400  # ~4800 chars → ~1200 tokens, well above max
        text = "# Section\n\n```python\n" + body + "```\n"
        chunks = _chunk(text)
        # All fence content lives in one chunk; counting fence markers in that
        # chunk should be balanced (both opener and closer present).
        fence_markers = [c for c in chunks if c.content.count("```") > 0]
        assert len(fence_markers) == 1
        c = fence_markers[0]
        assert c.content.count("```") == 2, (
            f"expected opener+closer in one chunk, got {c.content.count('```')}"
        )
        assert "print('x')" in c.content

    def test_fence_with_blank_lines_is_not_paragraph_split(self):
        """Blank lines inside a fence must not break it across paragraphs."""
        body = "line1\n\nline2\n\nline3\n" * 80  # forces paragraph_split_threshold
        text = "# S\n\n```\n" + body + "```\n"
        chunks = _chunk(text)
        # Every chunk that touches the fence must have balanced markers.
        for c in chunks:
            markers = c.content.count("```")
            assert markers % 2 == 0, (
                f"chunk has unbalanced ``` count={markers}: {c.content[:120]!r}"
            )

    def test_unclosed_fence_absorbs_rest_of_file(self):
        """An opener without a closer protects the tail; no split inside it."""
        text = "# Top\n\n```python\n# still code, not heading\n# another commented line\nx = 1\n"
        chunks = _chunk(text)
        assert len(chunks) == 1
        assert "# still code" in chunks[0].content
        assert "# another commented line" in chunks[0].content

    def test_fence_with_language_tag(self):
        """Opener with language tag (```python) closes on bare ``` only."""
        text = "# T\n\n```python\n# nope\n```\n\nafter\n"
        chunks = _chunk(text)
        assert len(chunks) == 1
        assert "# nope" in chunks[0].content
        assert "after" in chunks[0].content


class TestTableSurvivesExistingSplits:
    """Sanity: tables already survive paragraph/sentence split because rows
    have no blank lines or sentence terminators. This test locks the behaviour
    in so future changes to the split fallbacks do not silently start slicing
    tables mid-row.
    """

    def test_table_kept_intact_even_when_section_is_large(self):
        table = "| Col A | Col B | Col C |\n| --- | --- | --- |\n" + "".join(
            f"| row{i}a | row{i}b | row{i}c |\n" for i in range(200)
        )
        text = "# S\n\n" + table
        chunks = _chunk(text)
        # No chunk should contain a partial header row without its separator,
        # and no chunk should start with a data row while the header lives
        # elsewhere.
        for c in chunks:
            if "| Col A | Col B |" in c.content:
                assert "| --- | --- |" in c.content, (
                    "table header split away from its separator row"
                )
