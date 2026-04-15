"""Tests for ReStructuredText chunker."""

from __future__ import annotations

from pathlib import Path

from memtomem.chunking.restructured_text import ReStructuredTextChunker
from memtomem.models import ChunkType


def _chunk(content: str) -> list:
    return ReStructuredTextChunker().chunk_file(Path("test.rst"), content)


class TestSupportedExtensions:
    def test_rst(self):
        assert ".rst" in ReStructuredTextChunker().supported_extensions()


class TestEmpty:
    def test_empty_string(self):
        assert _chunk("") == []

    def test_whitespace_only(self):
        assert _chunk("   \n\n  ") == []


class TestNoHeadings:
    def test_plain_text(self):
        chunks = _chunk("Just some text.\nAnother line.")
        assert len(chunks) == 1
        assert "Just some text." in chunks[0].content

    def test_chunk_type_is_rst(self):
        chunks = _chunk("Plain text only.")
        assert chunks[0].metadata.chunk_type == ChunkType.RST_SECTION


class TestUnderlineHeaders:
    def test_single_section(self):
        content = "Title\n=====\n\nBody text."
        chunks = _chunk(content)
        assert len(chunks) == 1
        assert "Title" in chunks[0].content
        assert "Body text." in chunks[0].content

    def test_two_sections(self):
        content = "First\n=====\n\nBody 1.\n\nSecond\n======\n\nBody 2."
        chunks = _chunk(content)
        assert len(chunks) == 2
        assert "First" in chunks[0].content
        assert "Second" in chunks[1].content

    def test_hierarchy(self):
        content = "Top\n===\n\nIntro.\n\nSub\n---\n\nDetail."
        chunks = _chunk(content)
        assert len(chunks) == 2
        assert chunks[0].metadata.heading_hierarchy == ("Top",)
        assert chunks[1].metadata.heading_hierarchy == ("Top", "Sub")

    def test_parent_context(self):
        content = "Top\n===\n\nIntro.\n\nSub\n---\n\nDetail."
        chunks = _chunk(content)
        assert chunks[1].metadata.parent_context == "Top"


class TestOverlineHeaders:
    def test_overline_and_underline(self):
        content = "=====\nTitle\n=====\n\nBody."
        chunks = _chunk(content)
        assert len(chunks) == 1
        assert "Title" in chunks[0].content


class TestAdornmentCharacters:
    def test_tilde(self):
        content = "Section\n~~~~~~~\n\nContent."
        chunks = _chunk(content)
        assert len(chunks) == 1
        assert "Section" in chunks[0].content

    def test_caret(self):
        content = "Section\n^^^^^^^\n\nContent."
        chunks = _chunk(content)
        assert len(chunks) == 1

    def test_different_chars_different_levels(self):
        content = "H1\n==\n\nA.\n\nH2\n--\n\nB.\n\nH3\n~~\n\nC."
        chunks = _chunk(content)
        assert len(chunks) == 3
        assert chunks[2].metadata.heading_hierarchy == ("H1", "H2", "H3")


class TestContentBeforeFirstHeader:
    def test_preamble_is_separate_chunk(self):
        content = "Preamble text.\n\nTitle\n=====\n\nBody."
        chunks = _chunk(content)
        assert len(chunks) == 2
        assert "Preamble" in chunks[0].content
        assert chunks[0].metadata.heading_hierarchy == ()


class TestFileContext:
    def test_file_context_includes_headings(self):
        content = "AA\n==\n\nX.\n\nBB\n--\n\nY."
        chunks = _chunk(content)
        for c in chunks:
            assert "test.rst" in c.metadata.file_context
            assert "AA" in c.metadata.file_context


class TestLineNumbers:
    def test_start_line_is_one_indexed(self):
        content = "Title\n=====\n\nBody."
        chunks = _chunk(content)
        assert chunks[0].metadata.start_line >= 1
