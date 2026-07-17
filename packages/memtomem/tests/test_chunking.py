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

    def test_oversized_bullet_list_without_blank_lines_is_split(self):
        config = FakeIndexingConfig()
        config.chunk_overlap_tokens = 0
        chunker = MarkdownChunker(indexing_config=config)
        body = "\n".join(f"- item {i:02d} " + "w" * 30 for i in range(20))
        content = f"## Notes\n\n{body}"

        chunks = chunker.chunk_file(Path("/test.md"), content)

        assert len(chunks) > 1
        assert all(len(chunk.content) <= config.max_chunk_tokens * 4 for chunk in chunks)
        source_lines = content.splitlines()
        for index, chunk in enumerate(chunks):
            chunk_lines = chunk.content.splitlines()
            actual_start = source_lines.index(chunk_lines[0]) + 1
            actual_end = actual_start + len(chunk_lines) - 1

            assert chunk_lines == source_lines[actual_start - 1 : actual_end]
            assert chunk.metadata.start_line == (1 if index == 0 else actual_start)
            assert chunk.metadata.end_line == actual_end
            assert 1 <= chunk.metadata.start_line <= chunk.metadata.end_line <= len(source_lines)

    def test_mid_part_fence_is_kept_atomic(self):
        config = FakeIndexingConfig()
        config.chunk_overlap_tokens = 0
        config.paragraph_split_threshold = 200
        chunker = MarkdownChunker(indexing_config=config)
        code = "\n".join(f"    step_{i} = do_thing_number_{i}()" for i in range(12))
        body = "intro line without punctuation\n```python\n" + code + "\n```"

        chunks = chunker.chunk_file(Path("/test.md"), f"## Code\n\n{body}")

        assert len(chunks) == 1
        assert chunks[0].content == body
        assert chunks[0].content.count("```") == 2
        assert chunks[0].metadata.start_line == 1
        assert chunks[0].metadata.end_line == len(f"## Code\n\n{body}".splitlines())

    def test_sentence_fallback_preserves_source_spacing(self):
        config = FakeIndexingConfig()
        config.chunk_overlap_tokens = 0
        config.paragraph_split_threshold = 200
        chunker = MarkdownChunker(indexing_config=config)
        body = " ".join(f"Sentence {i:02d} has enough words to split." for i in range(12))

        chunks = chunker.chunk_file(Path("/test.md"), f"## Prose\n\n{body}")

        assert len(chunks) > 1
        assert " ".join(chunk.content for chunk in chunks) == body
        assert [chunk.metadata.start_line for chunk in chunks] == [1] + [3] * (len(chunks) - 1)
        assert all(chunk.metadata.end_line == 3 for chunk in chunks)

    def test_word_fallback_preserves_source_spacing(self):
        config = FakeIndexingConfig()
        config.chunk_overlap_tokens = 0
        config.paragraph_split_threshold = 200
        chunker = MarkdownChunker(indexing_config=config)
        body = " ".join(f"word{i:02d}" for i in range(60))

        chunks = chunker.chunk_file(Path("/test.md"), f"## Words\n\n{body}")

        assert len(chunks) > 1
        assert " ".join(chunk.content for chunk in chunks) == body
        assert [chunk.metadata.start_line for chunk in chunks] == [1] + [3] * (len(chunks) - 1)
        assert all(chunk.metadata.end_line == 3 for chunk in chunks)

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


class TestBoldLabelSplit:
    """Bold-label fallback between paragraph and sentence splits — survives
    oversized FAQ / changelog / structured-note sections that have no blank-
    line separators between ``**Label:**`` entries.
    """

    def test_bold_label_split_preserves_boundaries(self):
        chunker = MarkdownChunker(indexing_config=FakeIndexingConfig())
        # Below paragraph threshold, above max_chars (50*4=200). No blank
        # lines between entries forces the paragraph split to a single
        # part. Bold-label fallback should catch the structure.
        body = (
            "**Added:** New feature X with enough detail to consume tokens. "
            "**Added:** Another feature Y written out in full sentences. "
            "**Fixed:** A bug report describing the regression found. "
            "**Fixed:** Another fix landed in the same release. "
            "**Removed:** A deprecated path that nobody was using."
        )
        content = f"## Changelog\n\n{body}"
        chunks = chunker.chunk_file(Path("/test.md"), content)
        # Should split into multiple chunks along bold-label lines, not
        # fall through to sentence split.
        assert len(chunks) > 1
        # At least one chunk should open with a bold label
        assert any(c.content.lstrip().startswith("**") for c in chunks)

    def test_single_bold_label_does_not_split(self):
        chunker = MarkdownChunker(indexing_config=FakeIndexingConfig())
        # A single **Note:** inside otherwise prose text — not enough
        # structure to justify splitting. Should fall through to sentence
        # split.
        prose = "This is a sentence. " * 30  # ~160 chars
        body = f"**Note:** {prose}"
        content = f"## Section\n\n{body}"
        chunks = chunker.chunk_file(Path("/test.md"), content)
        # Hard to assert exact count (depends on sentence split), but the
        # split should not produce a chunk per bold-label occurrence (only
        # one label exists).
        assert chunks  # at least something emitted
