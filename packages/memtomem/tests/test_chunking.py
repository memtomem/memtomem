"""Tests for adaptive markdown chunking."""

from pathlib import Path
from memtomem.chunking.markdown import MarkdownChunker


class FakeIndexingConfig:
    max_chunk_tokens = 50  # very small for testing
    min_chunk_tokens = 10
    chunk_overlap_tokens = 5
    paragraph_split_threshold = 30


class TestAdaptiveChunking:
    def test_small_section_not_split(self):
        chunker = MarkdownChunker(indexing_config=FakeIndexingConfig())
        content = "## Title\n\nShort paragraph."
        chunks = chunker.chunk_file(Path("/test.md"), content)
        assert len(chunks) == 1

    def test_large_section_split_by_paragraphs(self):
        chunker = MarkdownChunker(indexing_config=FakeIndexingConfig())
        # Create content that exceeds max_chunk_tokens (50 tokens * 3 chars = 150 chars)
        para1 = "First paragraph with enough words to be meaningful content. " * 3
        para2 = "Second paragraph also with enough words to be meaningful content. " * 3
        para3 = "Third paragraph completing the test with more content here. " * 3
        content = f"## Title\n\n{para1}\n\n{para2}\n\n{para3}"
        chunks = chunker.chunk_file(Path("/test.md"), content)
        assert len(chunks) > 1

    def test_overlap_applied(self):
        config = FakeIndexingConfig()
        config.chunk_overlap_tokens = 10
        chunker = MarkdownChunker(indexing_config=config)
        para1 = "Alpha bravo charlie delta echo foxtrot. " * 5
        para2 = "Golf hotel india juliet kilo lima mike. " * 5
        content = f"## Section\n\n{para1}\n\n{para2}"
        chunks = chunker.chunk_file(Path("/test.md"), content)
        if len(chunks) > 1:
            assert chunks[1].metadata.overlap_before > 0

    def test_no_config_uses_defaults(self):
        chunker = MarkdownChunker()  # no config
        content = "## Heading\n\nSome text."
        chunks = chunker.chunk_file(Path("/test.md"), content)
        assert len(chunks) == 1

    def test_empty_content(self):
        chunker = MarkdownChunker(indexing_config=FakeIndexingConfig())
        assert chunker.chunk_file(Path("/test.md"), "") == []

    def test_heading_hierarchy_preserved(self):
        chunker = MarkdownChunker(indexing_config=FakeIndexingConfig())
        content = "# Top\n\n## Sub\n\nContent here."
        chunks = chunker.chunk_file(Path("/test.md"), content)
        sub_chunk = [c for c in chunks if "Content" in c.content]
        assert sub_chunk
        assert len(sub_chunk[0].metadata.heading_hierarchy) >= 1
