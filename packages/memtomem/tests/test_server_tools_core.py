"""Tests for core MCP tool logic: browse, search, recall, status/config, memory CRUD.

Exercises the underlying component methods that back the MCP tools, without
going through the MCP context layer.  All tests use real SQLite storage with
tmp_path isolation — no mocks.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from memtomem.config import Mem2MemConfig
from memtomem.models import Chunk, ChunkMetadata, NamespaceFilter, SearchResult
from memtomem.server.formatters import (
    _display_path,
    _format_results,
    _format_structured_results,
)
from memtomem.server.helpers import _check_embedding_mismatch, _parse_recall_date, _set_config_key
from memtomem.tools.memory_writer import append_entry

from helpers import make_chunk


# ---------------------------------------------------------------------------
# Browse — mem_list logic
# ---------------------------------------------------------------------------


class TestMemList:
    """Tests for the mem_list / source-file listing flow."""

    async def test_source_files_with_counts_empty_db(self, storage):
        result = await storage.get_source_files_with_counts()
        assert result == []

    async def test_source_files_with_counts_single_source(self, storage):
        c1 = make_chunk("first chunk", source="notes.md")
        c2 = make_chunk("second chunk", source="notes.md")
        await storage.upsert_chunks([c1, c2])

        result = await storage.get_source_files_with_counts()
        assert len(result) == 1
        path, count, _updated, _ns, _avg, _min, _max = result[0]
        assert count == 2
        assert "notes.md" in str(path)

    async def test_source_files_with_counts_multiple_sources(self, storage):
        c1 = make_chunk("a", source="alpha.md")
        c2 = make_chunk("b", source="beta.md")
        c3 = make_chunk("c", source="beta.md")
        await storage.upsert_chunks([c1, c2, c3])

        result = await storage.get_source_files_with_counts()
        assert len(result) == 2
        # Match by filename suffix since /tmp may resolve to /private/tmp on macOS
        counts = {Path(r[0]).name: r[1] for r in result}
        assert counts["alpha.md"] == 1
        assert counts["beta.md"] == 2

    async def test_source_files_reports_namespaces(self, storage):
        c1 = make_chunk("ns1 chunk", source="mixed.md", namespace="work")
        c2 = make_chunk("ns2 chunk", source="mixed.md", namespace="personal")
        await storage.upsert_chunks([c1, c2])

        result = await storage.get_source_files_with_counts()
        assert len(result) == 1
        _path, _count, _updated, namespaces, _avg, _min, _max = result[0]
        # GROUP_CONCAT returns comma-separated namespaces
        assert "work" in namespaces
        assert "personal" in namespaces

    async def test_source_filter_substring_match(self, storage):
        """Recall's source_filter uses LIKE %filter%, simulating mem_list filter."""
        c1 = make_chunk("a", source="project/notes.md")
        c2 = make_chunk("b", source="journal/diary.md")
        await storage.upsert_chunks([c1, c2])

        # recall_chunks with source_filter exercises the same LIKE logic
        result = await storage.recall_chunks(source_filter="project", limit=10)
        assert len(result) == 1
        assert "project" in str(result[0].metadata.source_file)

    async def test_namespace_filter_exact_match(self, storage):
        c1 = make_chunk("work item", namespace="work")
        c2 = make_chunk("play item", namespace="play")
        await storage.upsert_chunks([c1, c2])

        ns_filter = NamespaceFilter.parse("work")
        result = await storage.recall_chunks(namespace_filter=ns_filter, limit=10)
        assert len(result) == 1
        assert result[0].metadata.namespace == "work"


# ---------------------------------------------------------------------------
# Browse — mem_read logic
# ---------------------------------------------------------------------------


class TestMemRead:
    """Tests for the mem_read / chunk retrieval flow."""

    async def test_get_chunk_returns_content_and_metadata(self, storage):
        chunk = make_chunk(
            "important info",
            source="docs.md",
            tags=("python", "tips"),
            heading=("Guide", "Basics"),
        )
        await storage.upsert_chunks([chunk])

        result = await storage.get_chunk(chunk.id)
        assert result is not None
        assert result.content == "important info"
        assert result.metadata.tags == ("python", "tips")
        assert result.metadata.heading_hierarchy == ("Guide", "Basics")

    async def test_get_chunk_nonexistent_returns_none(self, storage):
        result = await storage.get_chunk(uuid4())
        assert result is None

    async def test_get_chunks_batch(self, storage):
        c1 = make_chunk("one")
        c2 = make_chunk("two")
        c3 = make_chunk("three")
        await storage.upsert_chunks([c1, c2, c3])

        batch = await storage.get_chunks_batch([c1.id, c3.id])
        assert len(batch) == 2
        assert batch[c1.id].content == "one"
        assert batch[c3.id].content == "three"

    async def test_get_chunks_batch_empty(self, storage):
        batch = await storage.get_chunks_batch([])
        assert batch == {}


# ---------------------------------------------------------------------------
# Search — mem_search logic (BM25 only, no embedding dependency)
# ---------------------------------------------------------------------------


class TestMemSearch:
    """Tests for BM25-based search through storage."""

    async def test_bm25_search_returns_results(self, storage):
        c1 = make_chunk("python asyncio tutorial for beginners", source="tut.md")
        c2 = make_chunk("javascript framework comparison review", source="js.md")
        await storage.upsert_chunks([c1, c2])

        results = await storage.bm25_search("python asyncio", top_k=5)
        assert len(results) >= 1
        contents = [r.chunk.content for r in results]
        assert any("python" in c for c in contents)

    async def test_bm25_search_no_match(self, storage):
        c1 = make_chunk("quantum physics lecture notes")
        await storage.upsert_chunks([c1])

        results = await storage.bm25_search("javascript react hooks", top_k=5)
        assert len(results) == 0

    async def test_bm25_search_with_namespace_filter(self, storage):
        c1 = make_chunk("python programming guide", namespace="work")
        c2 = make_chunk("python cooking recipes", namespace="personal")
        await storage.upsert_chunks([c1, c2])

        ns = NamespaceFilter.parse("work")
        results = await storage.bm25_search("python", top_k=5, namespace_filter=ns)
        assert all(r.chunk.metadata.namespace == "work" for r in results)

    async def test_bm25_search_respects_top_k(self, storage):
        chunks = [make_chunk(f"python topic number {i}") for i in range(10)]
        await storage.upsert_chunks(chunks)

        results = await storage.bm25_search("python topic", top_k=3)
        assert len(results) <= 3


# ---------------------------------------------------------------------------
# Recall — mem_recall logic
# ---------------------------------------------------------------------------


class TestMemRecall:
    """Tests for time-based chunk recall."""

    async def test_recall_returns_recent_chunks(self, storage):
        c1 = make_chunk("recent note")
        await storage.upsert_chunks([c1])

        result = await storage.recall_chunks(limit=10)
        assert len(result) == 1
        assert result[0].content == "recent note"

    async def test_recall_date_range_since(self, storage):
        old = make_chunk("old entry")
        # Backdate the chunk via direct attribute manipulation before upserting
        old_time = datetime(2023, 1, 1, tzinfo=timezone.utc)
        # Create chunk with old timestamp
        old = Chunk(
            content="old entry",
            metadata=ChunkMetadata(source_file=Path("/tmp/old.md")),
            content_hash=f"hash-{uuid4().hex[:8]}",
            embedding=[0.1] * 1024,
            created_at=old_time,
            updated_at=old_time,
        )
        new = make_chunk("new entry")
        await storage.upsert_chunks([old, new])

        since = datetime(2024, 1, 1, tzinfo=timezone.utc)
        result = await storage.recall_chunks(since=since, limit=10)
        # Only the new entry should appear (created_at >= 2024-01-01)
        assert all(r.content != "old entry" for r in result)

    async def test_recall_date_range_until(self, storage):
        now = datetime.now(timezone.utc)
        future = now + timedelta(days=365 * 10)
        chunk = make_chunk("present chunk")
        await storage.upsert_chunks([chunk])

        # until far in the future should include everything
        result = await storage.recall_chunks(until=future, limit=10)
        assert len(result) >= 1

    async def test_recall_source_filter(self, storage):
        c1 = make_chunk("work note", source="work/tasks.md")
        c2 = make_chunk("home note", source="home/todo.md")
        await storage.upsert_chunks([c1, c2])

        result = await storage.recall_chunks(source_filter="work", limit=10)
        assert len(result) == 1
        assert "work" in str(result[0].metadata.source_file)

    async def test_recall_namespace_filter(self, storage):
        c1 = make_chunk("proj A", namespace="alpha")
        c2 = make_chunk("proj B", namespace="beta")
        await storage.upsert_chunks([c1, c2])

        ns = NamespaceFilter.parse("alpha")
        result = await storage.recall_chunks(namespace_filter=ns, limit=10)
        assert len(result) == 1
        assert result[0].metadata.namespace == "alpha"

    async def test_recall_limit(self, storage):
        chunks = [make_chunk(f"item {i}") for i in range(5)]
        await storage.upsert_chunks(chunks)

        result = await storage.recall_chunks(limit=2)
        assert len(result) == 2


# ---------------------------------------------------------------------------
# Status/Config — mem_status / mem_config logic
# ---------------------------------------------------------------------------


class TestStatus:
    """Tests for get_stats and config operations."""

    async def test_get_stats_empty(self, storage):
        stats = await storage.get_stats()
        assert stats["total_chunks"] == 0
        assert stats["total_sources"] == 0

    async def test_get_stats_after_insert(self, storage):
        c1 = make_chunk("a", source="f1.md")
        c2 = make_chunk("b", source="f1.md")
        c3 = make_chunk("c", source="f2.md")
        await storage.upsert_chunks([c1, c2, c3])

        stats = await storage.get_stats()
        assert stats["total_chunks"] == 3
        assert stats["total_sources"] == 2


class TestSetConfigKey:
    """Tests for the _set_config_key runtime config setter."""

    def test_set_int_field(self):
        config = Mem2MemConfig()
        msg = _set_config_key(config, "search.default_top_k", "20")
        assert "Set search.default_top_k" in msg
        assert config.search.default_top_k == 20

    def test_set_bool_field_true(self):
        config = Mem2MemConfig()
        msg = _set_config_key(config, "decay.enabled", "true")
        assert config.decay.enabled is True
        assert msg.startswith("Set ")

    def test_set_bool_field_false(self):
        config = Mem2MemConfig()
        config.decay.enabled = True
        _set_config_key(config, "decay.enabled", "false")
        assert config.decay.enabled is False

    def test_set_float_field(self):
        config = Mem2MemConfig()
        _set_config_key(config, "decay.half_life_days", "7.5")
        assert config.decay.half_life_days == 7.5

    def test_set_string_field(self):
        """Mutable string field (namespace.default_namespace) should be settable."""
        config = Mem2MemConfig()
        _set_config_key(config, "namespace.default_namespace", "work")
        assert config.namespace.default_namespace == "work"

    def test_init_only_field_rejected(self):
        """Init-only fields like embedding.provider must be rejected."""
        config = Mem2MemConfig()
        msg = _set_config_key(config, "embedding.provider", "openai")
        assert "not mutable" in msg.lower()

    def test_invalid_section(self):
        config = Mem2MemConfig()
        msg = _set_config_key(config, "nonexistent.field", "val")
        assert "not found" in msg

    def test_invalid_field(self):
        config = Mem2MemConfig()
        msg = _set_config_key(config, "search.nonexistent", "val")
        assert "not found" in msg

    def test_invalid_key_format_no_dot(self):
        config = Mem2MemConfig()
        msg = _set_config_key(config, "nodot", "val")
        assert "section.field" in msg

    def test_invalid_key_format_too_many_dots(self):
        config = Mem2MemConfig()
        msg = _set_config_key(config, "a.b.c", "val")
        assert "section.field" in msg

    def test_set_rrf_weights_csv(self):
        """rrf_weights accepts CSV string and coerces to list[float]."""
        config = Mem2MemConfig()
        msg = _set_config_key(config, "search.rrf_weights", "1.5,0.8")
        assert msg.startswith("Set ")
        assert config.search.rrf_weights == [1.5, 0.8]

    def test_set_rrf_weights_wrong_length(self):
        config = Mem2MemConfig()
        msg = _set_config_key(config, "search.rrf_weights", "1.0,2.0,3.0")
        assert "Invalid" in msg or "length" in msg


# ---------------------------------------------------------------------------
# Memory CRUD — add, edit, delete, batch
# ---------------------------------------------------------------------------


class TestMemoryCRUD:
    """Tests for memory write/index/delete flows."""

    async def test_append_entry_creates_file(self, memory_dir):
        target = memory_dir / "test_add.md"
        append_entry(target, "Hello, world!", title="Greeting")

        assert target.exists()
        text = target.read_text(encoding="utf-8")
        assert "## Greeting" in text
        assert "Hello, world!" in text

    async def test_append_entry_with_tags(self, memory_dir):
        target = memory_dir / "tagged.md"
        append_entry(target, "Tagged content", title="Tagged", tags=["python", "tips"])

        text = target.read_text(encoding="utf-8")
        assert "tags:" in text
        assert "python" in text

    async def test_append_entry_auto_title(self, memory_dir):
        target = memory_dir / "auto.md"
        append_entry(target, "No explicit title given")

        text = target.read_text(encoding="utf-8")
        # Auto-generated title uses "Entry <timestamp>"
        assert "## Entry" in text

    async def test_append_entry_preserves_existing_content(self, memory_dir):
        target = memory_dir / "multi.md"
        append_entry(target, "First entry", title="First")
        append_entry(target, "Second entry", title="Second")

        text = target.read_text(encoding="utf-8")
        assert "## First" in text
        assert "## Second" in text
        assert "First entry" in text
        assert "Second entry" in text

    async def test_append_entry_content_starts_with_heading(self, memory_dir):
        """When content already starts with ##, no extra heading is added."""
        target = memory_dir / "heading.md"
        append_entry(target, "## Custom heading\n\nBody text")

        text = target.read_text(encoding="utf-8")
        assert text.count("## Custom heading") == 1

    async def test_upsert_then_delete_by_chunk_id(self, storage):
        chunk = make_chunk("deletable content")
        await storage.upsert_chunks([chunk])
        assert await storage.get_chunk(chunk.id) is not None

        deleted = await storage.delete_chunks([chunk.id])
        assert deleted == 1
        assert await storage.get_chunk(chunk.id) is None

    async def test_delete_by_source(self, storage):
        source = Path("/tmp/remove_me.md")
        c1 = make_chunk("a", source="remove_me.md")
        c2 = make_chunk("b", source="remove_me.md")
        c3 = make_chunk("c", source="keep_me.md")
        await storage.upsert_chunks([c1, c2, c3])

        deleted = await storage.delete_by_source(source)
        assert deleted == 2

        stats = await storage.get_stats()
        assert stats["total_chunks"] == 1

    async def test_delete_by_namespace(self, storage):
        c1 = make_chunk("work thing", namespace="temp")
        c2 = make_chunk("perm thing", namespace="permanent")
        await storage.upsert_chunks([c1, c2])

        deleted = await storage.delete_by_namespace("temp")
        assert deleted == 1

        stats = await storage.get_stats()
        assert stats["total_chunks"] == 1

    async def test_delete_nonexistent_chunk(self, storage):
        deleted = await storage.delete_chunks([uuid4()])
        assert deleted == 0

    async def test_batch_add_multiple_entries(self, memory_dir):
        """Simulates batch_add: multiple entries appended to one file."""
        target = memory_dir / "batch.md"
        entries = [
            ("Fact one", "The sky is blue"),
            ("Fact two", "Water is wet"),
            ("Fact three", "Fire is hot"),
        ]
        for title, content in entries:
            append_entry(target, content, title=title)

        text = target.read_text(encoding="utf-8")
        assert text.count("## Fact") == 3
        assert "The sky is blue" in text
        assert "Water is wet" in text
        assert "Fire is hot" in text

    async def test_upsert_updates_existing_chunk(self, storage):
        """Upserting a chunk with the same ID updates its content."""
        chunk = make_chunk("original content")
        await storage.upsert_chunks([chunk])

        updated = Chunk(
            content="updated content",
            metadata=chunk.metadata,
            id=chunk.id,
            content_hash=f"hash-{uuid4().hex[:8]}",
            embedding=[0.1] * 1024,
        )
        await storage.upsert_chunks([updated])

        result = await storage.get_chunk(chunk.id)
        assert result is not None
        assert result.content == "updated content"

        stats = await storage.get_stats()
        assert stats["total_chunks"] == 1  # no duplicate


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


class TestParseRecallDate:
    """Tests for _parse_recall_date partial date parser."""

    def test_year_only_since(self):
        dt = _parse_recall_date("2024")
        assert dt == datetime(2024, 1, 1, tzinfo=timezone.utc)

    def test_year_only_until(self):
        dt = _parse_recall_date("2024", end_of_period=True)
        assert dt == datetime(2025, 1, 1, tzinfo=timezone.utc)

    def test_year_month_since(self):
        dt = _parse_recall_date("2024-06")
        assert dt == datetime(2024, 6, 1, tzinfo=timezone.utc)

    def test_year_month_until(self):
        dt = _parse_recall_date("2024-06", end_of_period=True)
        assert dt == datetime(2024, 7, 1, tzinfo=timezone.utc)

    def test_year_month_december_until(self):
        """December rolls over to January of next year."""
        dt = _parse_recall_date("2024-12", end_of_period=True)
        assert dt == datetime(2025, 1, 1, tzinfo=timezone.utc)

    def test_full_date_since(self):
        dt = _parse_recall_date("2024-03-15")
        assert dt == datetime(2024, 3, 15, tzinfo=timezone.utc)

    def test_full_date_until(self):
        dt = _parse_recall_date("2024-03-15", end_of_period=True)
        assert dt == datetime(2024, 3, 16, tzinfo=timezone.utc)

    def test_iso_datetime_passthrough(self):
        dt = _parse_recall_date("2024-03-15T10:30:00+00:00")
        assert dt.hour == 10
        assert dt.minute == 30

    def test_iso_datetime_until_with_time(self):
        """Full datetime with time part is NOT advanced by a day."""
        dt = _parse_recall_date("2024-03-15T10:30:00+00:00", end_of_period=True)
        assert dt.day == 15  # not 16, because time was present

    def test_invalid_date_raises(self):
        with pytest.raises(ValueError, match="Invalid date"):
            _parse_recall_date("not-a-date")

    def test_whitespace_trimmed(self):
        dt = _parse_recall_date("  2024  ")
        assert dt == datetime(2024, 1, 1, tzinfo=timezone.utc)


class TestCheckEmbeddingMismatch:
    """Tests for _check_embedding_mismatch helper."""

    def test_returns_none_when_no_mismatch(self):
        class FakeApp:
            class storage:
                embedding_mismatch = None

        assert _check_embedding_mismatch(FakeApp()) is None

    def test_returns_none_when_no_storage(self):
        class FakeApp:
            pass

        assert _check_embedding_mismatch(FakeApp()) is None

    def test_returns_error_message_on_mismatch(self):
        class FakeApp:
            class storage:
                embedding_mismatch = {
                    "stored": {"provider": "ollama", "model": "nomic", "dimension": 768},
                    "configured": {"provider": "openai", "model": "ada-002", "dimension": 1536},
                }

        msg = _check_embedding_mismatch(FakeApp())
        assert msg is not None
        assert "mismatch" in msg.lower()
        assert "768" in msg
        assert "1536" in msg


class TestDisplayPath:
    """Tests for the _display_path formatter."""

    def test_regular_path(self):
        assert _display_path("/home/user/notes.md") == "/home/user/notes.md"

    def test_macos_private_tmp_stripped(self):
        if sys.platform == "darwin":
            assert _display_path("/private/tmp/test.md") == "/tmp/test.md"

    def test_non_tmp_private_path_kept(self):
        # /private/var should NOT be stripped
        result = _display_path("/private/var/data.md")
        assert result == "/private/var/data.md"


class TestFormatResults:
    """Tests for _format_results search output formatter."""

    def test_format_empty_results(self):
        output = _format_results([])
        assert "Found 0 results" in output

    def test_format_single_result_compact(self):
        chunk = Chunk(
            content="Test content here",
            metadata=ChunkMetadata(
                source_file=Path("/tmp/test.md"),
                heading_hierarchy=("Section", "Subsection"),
                namespace="default",
            ),
            embedding=[],
        )
        result = SearchResult(chunk=chunk, score=0.85, rank=1, source="bm25")
        output = _format_results([result])

        assert "Found 1 results" in output
        assert "0.85" in output
        assert "test.md" in output
        assert "Test content here" in output
        assert "Section > Subsection" in output
        # Compact: no UUID, no full path prefix
        assert "id=" not in output

    def test_format_single_result_verbose(self):
        chunk = Chunk(
            content="Test content here",
            metadata=ChunkMetadata(
                source_file=Path("/tmp/test.md"),
                heading_hierarchy=("Section", "Subsection"),
                namespace="default",
            ),
            embedding=[],
        )
        result = SearchResult(chunk=chunk, score=0.85, rank=1, source="bm25")
        output = _format_results([result], verbose=True)

        assert "Found 1 results" in output
        assert "score=0.8500" in output
        assert "id=" in output
        assert "Test content here" in output
        assert "Section > Subsection" in output

    def test_format_result_non_default_namespace(self):
        chunk = Chunk(
            content="Work item",
            metadata=ChunkMetadata(
                source_file=Path("/tmp/work.md"),
                namespace="work",
            ),
            embedding=[],
        )
        result = SearchResult(chunk=chunk, score=0.5, rank=1, source="fused")
        output = _format_results([result])
        assert "[work]" in output

    def test_format_result_default_namespace_no_badge(self):
        chunk = Chunk(
            content="Default ns",
            metadata=ChunkMetadata(
                source_file=Path("/tmp/d.md"),
                namespace="default",
            ),
            embedding=[],
        )
        result = SearchResult(chunk=chunk, score=0.5, rank=1, source="fused")
        output = _format_results([result])
        assert "[default]" not in output

    def test_format_multiple_results(self):
        results = []
        for i in range(3):
            chunk = Chunk(
                content=f"Result {i}",
                metadata=ChunkMetadata(source_file=Path(f"/tmp/r{i}.md")),
                embedding=[],
            )
            results.append(
                SearchResult(chunk=chunk, score=0.9 - i * 0.1, rank=i + 1, source="fused")
            )

        output = _format_results(results)
        assert "Found 3 results" in output
        assert "Result 0" in output
        assert "Result 2" in output

    def test_format_structured_produces_json(self):
        """output_format='structured' path returns valid JSON with expected keys."""
        import json

        chunk = Chunk(
            content="Structured test",
            metadata=ChunkMetadata(
                source_file=Path("/tmp/s.md"),
                heading_hierarchy=("Auth",),
                namespace="default",
            ),
            embedding=[],
        )
        result = SearchResult(chunk=chunk, score=0.92, rank=1, source="fused")
        output = _format_structured_results([result])
        parsed = json.loads(output)
        assert "results" in parsed
        assert parsed["results"][0]["content"] == "Structured test"
        assert parsed["results"][0]["hierarchy"] == "Auth"

    def test_verbose_true_when_output_format_compact(self):
        """verbose=True with default output_format should produce verbose output."""
        chunk = Chunk(
            content="Verbose compat",
            metadata=ChunkMetadata(
                source_file=Path("/tmp/v.md"),
                namespace="default",
            ),
            embedding=[],
        )
        result = SearchResult(chunk=chunk, score=0.75, rank=1, source="fused")
        # verbose=True with compact format should yield verbose output
        output = _format_results([result], verbose=True)
        assert "id=" in output
        assert "score=0.7500" in output

    def test_output_format_overrides_verbose(self):
        """output_format='structured' should produce JSON regardless of verbose flag."""
        import json

        chunk = Chunk(
            content="Override test",
            metadata=ChunkMetadata(
                source_file=Path("/tmp/o.md"),
                namespace="work",
            ),
            embedding=[],
        )
        result = SearchResult(chunk=chunk, score=0.88, rank=1, source="fused")
        # Structured format should be JSON, not verbose text
        output = _format_structured_results([result])
        parsed = json.loads(output)
        assert parsed["results"][0]["namespace"] == "work"
        # Must NOT contain verbose text markers
        assert "id=" not in output
        assert "```" not in output

    def test_verbose_and_output_format_verbose_redundant(self):
        """verbose=True + output_format='verbose' is redundant but should work."""
        chunk = Chunk(
            content="Redundant test",
            metadata=ChunkMetadata(
                source_file=Path("/tmp/r.md"),
                heading_hierarchy=("Notes",),
                namespace="default",
            ),
            embedding=[],
        )
        result = SearchResult(chunk=chunk, score=0.65, rank=1, source="fused")
        output = _format_results([result], verbose=True)
        assert "id=" in output
        assert "score=0.6500" in output
        assert "Redundant test" in output
