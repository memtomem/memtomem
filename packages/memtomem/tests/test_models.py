"""Tests for models.py — core data models (ChunkType, ChunkMetadata, Chunk,
NamespaceFilter, ContextInfo, SearchResult, IndexingStats)."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from memtomem.models import (
    Chunk,
    ChunkMetadata,
    ChunkType,
    ContextInfo,
    IndexingStats,
    NamespaceFilter,
    SearchResult,
)


class TestChunkType:
    def test_all_values_are_strings(self):
        for ct in ChunkType:
            assert isinstance(ct.value, str)
            assert ct.value == ct  # StrEnum: value compares equal to member

    def test_markdown_section_equals_literal_string(self):
        assert ChunkType.MARKDOWN_SECTION == "markdown_section"

    def test_unknown_value_raises(self):
        with pytest.raises(ValueError):
            ChunkType("not_a_real_type")


class TestChunkMetadata:
    def test_defaults(self):
        md = ChunkMetadata(source_file=Path("/t.md"))

        assert md.heading_hierarchy == ()
        assert md.chunk_type is ChunkType.RAW_TEXT
        assert md.language == "en"
        assert md.tags == ()
        assert md.namespace == "default"
        assert md.start_line == 0
        assert md.end_line == 0

    def test_is_frozen(self):
        md = ChunkMetadata(source_file=Path("/t.md"))
        with pytest.raises(FrozenInstanceError):
            md.language = "ko"  # type: ignore[misc]


class TestNamespaceFilterParse:
    def test_none_value_without_system_prefixes_returns_none(self):
        assert NamespaceFilter.parse(None) is None

    def test_none_value_with_system_prefixes_returns_exclude_filter(self):
        f = NamespaceFilter.parse(None, system_prefixes=("archive:",))

        assert f is not None
        assert f.exclude_prefixes == ("archive:",)
        assert f.namespaces == ()
        assert f.pattern is None

    def test_single_string_produces_exact_match(self):
        f = NamespaceFilter.parse("work")

        assert f is not None
        assert f.namespaces == ("work",)
        assert f.pattern is None

    def test_comma_separated_produces_union(self):
        f = NamespaceFilter.parse("work, personal ,  misc")

        assert f is not None
        # Values are stripped.
        assert f.namespaces == ("work", "personal", "misc")
        assert f.pattern is None

    def test_glob_pattern_preserved(self):
        f = NamespaceFilter.parse("proj:*")

        assert f is not None
        assert f.pattern == "proj:*"
        assert f.namespaces == ()

    def test_list_input_becomes_namespaces_tuple(self):
        f = NamespaceFilter.parse(["a", "b"])

        assert f is not None
        assert f.namespaces == ("a", "b")
        assert f.pattern is None

    def test_explicit_value_ignores_system_prefixes(self):
        # Caller explicitly named a namespace → opt-in, don't shadow with excludes.
        f = NamespaceFilter.parse("archive:summary", system_prefixes=("archive:",))

        assert f is not None
        assert f.namespaces == ("archive:summary",)
        assert f.exclude_prefixes == ()


class TestChunk:
    def test_content_hash_is_auto_generated(self):
        c = Chunk(content="hello", metadata=ChunkMetadata(source_file=Path("/t.md")))

        assert c.content_hash  # non-empty
        assert len(c.content_hash) == 64  # sha256 hex

    def test_content_hash_is_deterministic_for_same_content(self):
        md = ChunkMetadata(source_file=Path("/t.md"))
        a = Chunk(content="same text", metadata=md)
        b = Chunk(content="same text", metadata=md)

        assert a.content_hash == b.content_hash
        # But IDs must still differ (uuid4 default).
        assert a.id != b.id

    def test_content_hash_differs_for_different_content(self):
        md = ChunkMetadata(source_file=Path("/t.md"))
        a = Chunk(content="one", metadata=md)
        b = Chunk(content="two", metadata=md)

        assert a.content_hash != b.content_hash

    def test_content_hash_is_nfc_normalized(self):
        md = ChunkMetadata(source_file=Path("/t.md"))
        # "é" can be encoded as either NFC (single codepoint U+00E9) or NFD
        # (U+0065 + U+0301). After NFC normalization both must hash identically.
        nfc = Chunk(content="caf\u00e9", metadata=md)
        nfd = Chunk(content="cafe\u0301", metadata=md)

        assert nfc.content_hash == nfd.content_hash

    def test_explicit_content_hash_is_preserved(self):
        md = ChunkMetadata(source_file=Path("/t.md"))
        c = Chunk(content="whatever", metadata=md, content_hash="preset")

        assert c.content_hash == "preset"

    def test_retrieval_content_is_plain_when_no_hierarchy(self):
        c = Chunk(content="body", metadata=ChunkMetadata(source_file=Path("/t.md")))

        assert c.retrieval_content == "body"

    def test_retrieval_content_prefixes_hierarchy(self):
        md = ChunkMetadata(
            source_file=Path("/t.md"),
            heading_hierarchy=("Top", "Sub"),
        )
        c = Chunk(content="body text", metadata=md)

        assert c.retrieval_content == "Top > Sub\n\nbody text"


class TestContextInfo:
    def test_defaults(self):
        ctx = ContextInfo()

        assert ctx.window_before == ()
        assert ctx.window_after == ()
        assert ctx.parent_content is None
        assert ctx.chunk_position == 0
        assert ctx.context_tier_used is None

    def test_is_frozen(self):
        ctx = ContextInfo()
        with pytest.raises(FrozenInstanceError):
            ctx.chunk_position = 5  # type: ignore[misc]


class TestSearchResult:
    def test_construction_and_defaults(self):
        chunk = Chunk(content="c", metadata=ChunkMetadata(source_file=Path("/t.md")))
        sr = SearchResult(chunk=chunk, score=0.8, rank=1, source="bm25")

        assert sr.chunk is chunk
        assert sr.score == 0.8
        assert sr.rank == 1
        assert sr.source == "bm25"
        assert sr.context is None

    def test_is_frozen(self):
        chunk = Chunk(content="c", metadata=ChunkMetadata(source_file=Path("/t.md")))
        sr = SearchResult(chunk=chunk, score=0.5, rank=1, source="dense")
        with pytest.raises(FrozenInstanceError):
            sr.score = 0.9  # type: ignore[misc]


class TestIndexingStats:
    def test_defaults_for_optional_fields(self):
        stats = IndexingStats(
            total_files=1,
            total_chunks=5,
            indexed_chunks=5,
            skipped_chunks=0,
            deleted_chunks=0,
            duration_ms=10.0,
        )

        assert stats.errors == ()
        assert stats.new_chunk_ids == ()

    def test_is_frozen(self):
        stats = IndexingStats(
            total_files=1,
            total_chunks=1,
            indexed_chunks=1,
            skipped_chunks=0,
            deleted_chunks=0,
            duration_ms=1.0,
        )
        with pytest.raises(FrozenInstanceError):
            stats.total_files = 2  # type: ignore[misc]
