"""A heading-shaped line inside YAML frontmatter does not split the block (#3096).

A YAML comment such as ``# Metadata`` or ``# Comment`` inside leading
frontmatter used to be matched by ``_HEADING_RE``, splitting the frontmatter
block across chunks and inserting the comment as a heading in the document
outline and search detail view. Lines inside leading frontmatter are now
treated like code fences in ``_split_by_headings``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from memtomem.chunking.markdown import MarkdownChunker, leading_frontmatter


def test_frontmatter_yaml_comment_does_not_split_or_enter_outline():
    chunker = MarkdownChunker()
    content = "---\n# Metadata\ntags: [alpha]\n---\n\n# Cache policy\n\nRedis LRU eviction.\n"

    chunks = chunker.chunk_file(Path("/test.md"), content)

    assert len(chunks) == 2

    # First chunk keeps the intact frontmatter block and has no heading hierarchy
    c0 = chunks[0]
    assert c0.metadata.start_line == 1
    assert c0.metadata.end_line == 5
    assert c0.metadata.heading_hierarchy == ()
    assert c0.metadata.tags == ("alpha",)
    assert c0.content == "---\n# Metadata\ntags: [alpha]\n---"
    assert leading_frontmatter(c0.content) is not None

    # Second chunk is the real markdown section under '# Cache policy'
    c1 = chunks[1]
    assert c1.metadata.start_line == 6
    assert c1.metadata.end_line == 8
    assert c1.metadata.heading_hierarchy == ("# Cache policy",)
    assert c1.metadata.tags == ("alpha",)
    assert c1.content == "Redis LRU eviction."

    # Document outline / file context contains only real headings
    assert "# Metadata" not in c0.metadata.file_context
    assert "# Metadata" not in c1.metadata.file_context
    assert c0.metadata.file_context == "test.md > # Cache policy"


def test_frontmatter_multiple_heading_comments_and_levels():
    chunker = MarkdownChunker()
    content = (
        "---\n"
        "# Primary Comment\n"
        "## Sub comment\n"
        "title: Structured Note\n"
        "### Third comment\n"
        "tags: [alpha, beta]\n"
        "---\n"
        "\n"
        "## Real Section\n"
        "\n"
        "Section body.\n"
    )

    chunks = chunker.chunk_file(Path("/notes.md"), content)

    assert len(chunks) == 2
    c0, c1 = chunks

    # Frontmatter block stays atomic
    assert c0.metadata.heading_hierarchy == ()
    assert "tags: [alpha, beta]" in c0.content
    assert "# Primary Comment" in c0.content
    assert c0.metadata.tags == ("alpha", "beta")

    # Real section heading is preserved
    assert c1.metadata.heading_hierarchy == ("## Real Section",)
    assert c1.metadata.tags == ("alpha", "beta")
    assert c1.content == "Section body."

    # Outline must not include any comment headings
    for c in chunks:
        assert "# Primary Comment" not in c.metadata.file_context
        assert "## Sub comment" not in c.metadata.file_context
        assert "### Third comment" not in c.metadata.file_context


def test_frontmatter_comment_headingless_file():
    chunker = MarkdownChunker()
    content = (
        "---\n"
        "# Comment in auto-memory\n"
        "tags: [memory]\n"
        "---\n"
        "\n"
        "Auto-memory body text with no headings.\n"
    )

    chunks = chunker.chunk_file(Path("/auto.md"), content)

    assert len(chunks) == 1
    c = chunks[0]
    assert c.metadata.heading_hierarchy == ()
    assert c.metadata.tags == ("memory",)
    assert c.metadata.start_line == 1
    assert c.metadata.end_line == 6
    assert "# Comment in auto-memory" not in c.metadata.file_context
    assert c.metadata.file_context == "auto.md"


def test_frontmatter_comment_immediate_heading_without_blank_line():
    chunker = MarkdownChunker()
    content = (
        "---\n# Comment\ntags: [quick]\n---\n# Section Immediately After\nContent on line 6.\n"
    )

    chunks = chunker.chunk_file(Path("/compact.md"), content)

    assert len(chunks) == 2
    c0, c1 = chunks
    assert c0.metadata.start_line == 1
    assert c0.metadata.end_line == 4
    assert c0.metadata.heading_hierarchy == ()
    assert c0.content == "---\n# Comment\ntags: [quick]\n---"

    assert c1.metadata.start_line == 5
    assert c1.metadata.end_line == 6
    assert c1.metadata.heading_hierarchy == ("# Section Immediately After",)
    assert c1.content == "Content on line 6."


def test_fenced_code_and_frontmatter_comments_both_protected():
    chunker = MarkdownChunker()
    content = (
        "---\n"
        "# Frontmatter comment\n"
        "tags: [code]\n"
        "---\n"
        "\n"
        "# Real Heading\n"
        "\n"
        "```python\n"
        "# Python comment inside fence\n"
        "x = 1\n"
        "```\n"
    )

    chunks = chunker.chunk_file(Path("/both.md"), content)

    assert len(chunks) == 2
    c0, c1 = chunks
    assert c0.metadata.heading_hierarchy == ()
    assert c1.metadata.heading_hierarchy == ("# Real Heading",)
    assert "# Python comment inside fence" in c1.content


def test_horizontal_rule_later_in_document_not_treated_as_frontmatter():
    chunker = MarkdownChunker()
    content = (
        "# Top Heading\n"
        "\n"
        "Paragraph before rule.\n"
        "\n"
        "---\n"
        "\n"
        "# Subsequent Heading\n"
        "\n"
        "Paragraph after rule.\n"
    )

    chunks = chunker.chunk_file(Path("/rules.md"), content)

    # Both headings must be recognized
    assert any(c.metadata.heading_hierarchy == ("# Top Heading",) for c in chunks)
    assert any(c.metadata.heading_hierarchy == ("# Subsequent Heading",) for c in chunks)


@pytest.mark.asyncio
async def test_index_engine_index_file_regression(bm25_only_components):
    """End-to-end regression test: building chunks through IndexEngine.index_file (#3096)."""
    comp, mem_dir = bm25_only_components

    source = mem_dir / "cache_policy.md"
    content = (
        "---\n"
        "# Metadata\n"
        "tags: [alpha]\n"
        "owner: zqxvteam\n"
        "---\n"
        "\n"
        "# Cache policy\n"
        "\n"
        "Redis LRU eviction with high TTL.\n"
    )
    source.write_text(content, encoding="utf-8")

    stats = await comp.index_engine.index_file(source, ingress_guarded=True)
    assert not stats.errors

    chunks = await comp.storage.list_chunks_by_source(source)
    assert chunks

    # None of the indexed chunks should have '# Metadata' in their outline or hierarchy
    for chunk in chunks:
        assert "# Metadata" not in chunk.metadata.heading_hierarchy
        assert "# Metadata" not in chunk.metadata.file_context

    # The first chunk must contain the frontmatter block intact at its start
    first_chunk = min(chunks, key=lambda c: c.metadata.start_line)
    assert first_chunk.content.startswith("---\n# Metadata\ntags: [alpha]\n")
    assert leading_frontmatter(first_chunk.content) is not None
    assert "alpha" in first_chunk.metadata.tags

    # The real heading '# Cache policy' is preserved in the outline
    assert any("# Cache policy" in c.metadata.heading_hierarchy for c in chunks)
