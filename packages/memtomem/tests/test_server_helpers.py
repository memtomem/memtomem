"""Tests for server helper functions: formatters, helpers, error_handler."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from memtomem.config import Mem2MemConfig
from memtomem.errors import StorageError
from memtomem.models import Chunk, ChunkMetadata, SearchResult
from memtomem.server.error_handler import tool_handler
from memtomem.server.formatters import (
    _display_path,
    _format_compact_result,
    _format_results,
    _format_single_result,
    _format_structured_results,
    _format_verbose_result,
    _short_path,
)
from memtomem.server.helpers import _check_embedding_mismatch, _parse_recall_date, _set_config_key


# ===========================================================================
# formatters.py
# ===========================================================================


class TestDisplayPath:
    def test_private_tmp_stripped_on_macos(self):
        """On macOS, /private/tmp/foo should display as /tmp/foo."""
        if sys.platform != "darwin":
            pytest.skip("macOS-only test")
        assert _display_path(Path("/private/tmp/foo")) == "/tmp/foo"

    def test_private_tmp_subpath_stripped(self):
        if sys.platform != "darwin":
            pytest.skip("macOS-only test")
        assert _display_path(Path("/private/tmp/bar/baz.md")) == "/tmp/bar/baz.md"

    def test_non_private_path_unchanged(self):
        """Paths not under /private/tmp should remain unchanged."""
        assert _display_path(Path("/home/user/notes")) == "/home/user/notes"

    def test_string_input(self):
        """_display_path accepts anything with a str() representation."""
        assert _display_path("/home/user/file.md") == "/home/user/file.md"


class TestShortPath:
    def test_extracts_filename(self):
        assert _short_path("/home/user/notes/test.md") == "test.md"

    def test_single_filename(self):
        assert _short_path("file.md") == "file.md"

    def test_path_object(self):
        assert _short_path(Path("/tmp/dir/readme.md")) == "readme.md"


class TestFormatResults:
    def _make_result(self, rank: int = 1, score: float = 0.85, content: str = "hello"):
        chunk = Chunk(
            content=content,
            metadata=ChunkMetadata(
                source_file=Path("/tmp/notes.md"),
                heading_hierarchy=("Section A",),
                tags=("tag1",),
                namespace="default",
            ),
            id=uuid4(),
            embedding=[],
        )
        return SearchResult(chunk=chunk, score=score, rank=rank, source="fused")

    def test_empty_results(self):
        out = _format_results([])
        assert "Found 0 results" in out

    def test_single_result_compact_default(self):
        r = self._make_result(rank=1, score=0.9234, content="test content")
        out = _format_results([r])
        assert "Found 1 results" in out
        assert "0.92" in out
        assert "test content" in out
        # Compact: no UUID, no full path
        assert "id=" not in out
        assert "score=0.9234" not in out

    def test_single_result_verbose(self):
        r = self._make_result(rank=1, score=0.9234, content="test content")
        out = _format_results([r], verbose=True)
        assert "Found 1 results" in out
        assert "score=0.9234" in out
        assert "id=" in out
        assert "test content" in out

    def test_multiple_results(self):
        r1 = self._make_result(rank=1, score=0.9)
        r2 = self._make_result(rank=2, score=0.7)
        out = _format_results([r1, r2])
        assert "Found 2 results" in out


class TestFormatCompactResult:
    def test_compact_format_basic(self):
        chunk = Chunk(
            content="important memory",
            metadata=ChunkMetadata(
                source_file=Path("/home/user/test.md"),
                heading_hierarchy=("Notes", "Sub"),
                namespace="default",
            ),
            id=uuid4(),
            embedding=[],
        )
        r = SearchResult(chunk=chunk, score=0.8765, rank=3, source="bm25")
        out = _format_compact_result(r)
        assert "[3]" in out
        assert "0.88" in out
        assert "test.md" in out
        assert "Notes > Sub" in out
        # No UUID, no full path, no code block
        assert "id=" not in out
        assert "/home/user/" not in out
        assert "```" not in out

    def test_no_heading_hierarchy(self):
        chunk = Chunk(
            content="bare chunk",
            metadata=ChunkMetadata(
                source_file=Path("/tmp/flat.md"),
                heading_hierarchy=(),
                namespace="default",
            ),
            id=uuid4(),
            embedding=[],
        )
        r = SearchResult(chunk=chunk, score=0.5, rank=1, source="dense")
        out = _format_compact_result(r)
        assert "bare chunk" in out

    def test_namespace_badge_shown_for_non_default(self):
        chunk = Chunk(
            content="namespaced",
            metadata=ChunkMetadata(
                source_file=Path("/tmp/ns.md"),
                namespace="work",
            ),
            id=uuid4(),
            embedding=[],
        )
        r = SearchResult(chunk=chunk, score=0.5, rank=1, source="fused")
        out = _format_compact_result(r)
        assert "[work]" in out

    def test_no_namespace_badge_for_default(self):
        chunk = Chunk(
            content="default ns",
            metadata=ChunkMetadata(
                source_file=Path("/tmp/def.md"),
                namespace="default",
            ),
            id=uuid4(),
            embedding=[],
        )
        r = SearchResult(chunk=chunk, score=0.5, rank=1, source="fused")
        out = _format_compact_result(r)
        assert "[default]" not in out


class TestFormatVerboseResult:
    def test_verbose_includes_full_details(self):
        chunk = Chunk(
            content="important memory",
            metadata=ChunkMetadata(
                source_file=Path("/home/user/test.md"),
                heading_hierarchy=("Notes", "Sub"),
                namespace="default",
            ),
            id=uuid4(),
            embedding=[],
        )
        r = SearchResult(chunk=chunk, score=0.8765, rank=3, source="bm25")
        out = _format_verbose_result(r)
        assert "**[3]**" in out
        assert "score=0.8765" in out
        assert "id=" in out
        assert "/home/user/test.md" in out
        assert "Notes > Sub" in out
        assert "```" in out


class TestFormatSingleResultDelegation:
    """_format_single_result delegates to compact/verbose based on flag."""

    def _make_result(self):
        chunk = Chunk(
            content="test",
            metadata=ChunkMetadata(
                source_file=Path("/tmp/t.md"),
                namespace="default",
            ),
            id=uuid4(),
            embedding=[],
        )
        return SearchResult(chunk=chunk, score=0.5, rank=1, source="fused")

    def test_default_is_compact(self):
        r = self._make_result()
        out = _format_single_result(r)
        assert "id=" not in out
        assert "```" not in out

    def test_verbose_flag(self):
        r = self._make_result()
        out = _format_single_result(r, verbose=True)
        assert "id=" in out
        assert "```" in out


class TestFormatStructuredResults:
    """Tests for _format_structured_results JSON output."""

    def _make_result(
        self,
        rank: int = 1,
        score: float = 0.85,
        content: str = "hello",
        source_file: str = "/tmp/notes.md",
        hierarchy: tuple[str, ...] = ("Section A",),
        namespace: str = "default",
    ):
        chunk = Chunk(
            content=content,
            metadata=ChunkMetadata(
                source_file=Path(source_file),
                heading_hierarchy=hierarchy,
                tags=("tag1",),
                namespace=namespace,
            ),
            id=uuid4(),
            embedding=[],
        )
        return SearchResult(chunk=chunk, score=score, rank=rank, source="fused")

    def test_empty_results(self):
        import json

        out = _format_structured_results([])
        parsed = json.loads(out)
        assert parsed == {"results": []}

    def test_required_fields(self):
        import json

        r = self._make_result()
        parsed = json.loads(_format_structured_results([r]))
        item = parsed["results"][0]
        expected_keys = {"rank", "score", "source", "hierarchy", "namespace", "chunk_id", "content"}
        assert set(item.keys()) == expected_keys

    def test_score_precision(self):
        import json

        r = self._make_result(score=0.92345678)
        parsed = json.loads(_format_structured_results([r]))
        assert parsed["results"][0]["score"] == 0.9235

    def test_source_filename_only(self):
        import json

        r = self._make_result(source_file="/home/user/deep/nested/file.md")
        parsed = json.loads(_format_structured_results([r]))
        assert parsed["results"][0]["source"] == "file.md"

    def test_content_not_truncated(self):
        import json

        long_content = "x" * 1000
        r = self._make_result(content=long_content)
        parsed = json.loads(_format_structured_results([r]))
        assert len(parsed["results"][0]["content"]) == 1000

    def test_namespace_always_present(self):
        import json

        r = self._make_result(namespace="default")
        parsed = json.loads(_format_structured_results([r]))
        assert parsed["results"][0]["namespace"] == "default"

    def test_chunk_id_is_uuid(self):
        import json
        from uuid import UUID

        r = self._make_result()
        parsed = json.loads(_format_structured_results([r]))
        chunk_id = parsed["results"][0]["chunk_id"]
        UUID(chunk_id)  # raises ValueError if not valid UUID

    def test_hierarchy_joined(self):
        import json

        r = self._make_result(hierarchy=("A", "B", "C"))
        parsed = json.loads(_format_structured_results([r]))
        assert parsed["results"][0]["hierarchy"] == "A > B > C"

    def test_hierarchy_empty(self):
        import json

        r = self._make_result(hierarchy=())
        parsed = json.loads(_format_structured_results([r]))
        assert parsed["results"][0]["hierarchy"] == ""

    def test_valid_json(self):
        import json

        r = self._make_result()
        out = _format_structured_results([r])
        json.loads(out)  # should not raise

    def test_preserves_input_order(self):
        import json

        r1 = self._make_result(rank=1, score=0.9)
        r2 = self._make_result(rank=2, score=0.8)
        r3 = self._make_result(rank=3, score=0.7)
        parsed = json.loads(_format_structured_results([r1, r2, r3]))
        ranks = [item["rank"] for item in parsed["results"]]
        assert ranks == [1, 2, 3]


# ===========================================================================
# helpers.py — _parse_recall_date
# ===========================================================================


class TestParseRecallDate:
    def test_year_only(self):
        dt = _parse_recall_date("2026")
        assert dt == datetime(2026, 1, 1, tzinfo=timezone.utc)

    def test_year_month(self):
        dt = _parse_recall_date("2026-04")
        assert dt == datetime(2026, 4, 1, tzinfo=timezone.utc)

    def test_year_month_day(self):
        dt = _parse_recall_date("2026-04-06")
        assert dt == datetime(2026, 4, 6, tzinfo=timezone.utc)

    def test_year_end_of_period(self):
        dt = _parse_recall_date("2026", end_of_period=True)
        assert dt == datetime(2027, 1, 1, tzinfo=timezone.utc)

    def test_year_month_end_of_period(self):
        dt = _parse_recall_date("2026-04", end_of_period=True)
        assert dt == datetime(2026, 5, 1, tzinfo=timezone.utc)

    def test_year_month_day_end_of_period(self):
        dt = _parse_recall_date("2026-04-06", end_of_period=True)
        # Should advance to next day
        assert dt == datetime(2026, 4, 7, tzinfo=timezone.utc)

    def test_december_end_of_period_rolls_year(self):
        dt = _parse_recall_date("2026-12", end_of_period=True)
        assert dt == datetime(2027, 1, 1, tzinfo=timezone.utc)

    def test_full_iso_datetime(self):
        dt = _parse_recall_date("2026-04-06T14:30:00+00:00")
        assert dt.hour == 14
        assert dt.minute == 30

    def test_whitespace_stripped(self):
        dt = _parse_recall_date("  2026  ")
        assert dt == datetime(2026, 1, 1, tzinfo=timezone.utc)

    def test_invalid_date_raises(self):
        with pytest.raises(ValueError, match="Invalid date"):
            _parse_recall_date("not-a-date")


# ===========================================================================
# helpers.py — _check_embedding_mismatch
# ===========================================================================


class TestCheckEmbeddingMismatch:
    def test_no_mismatch_returns_none(self):
        app = MagicMock()
        app.storage.embedding_mismatch = None
        assert _check_embedding_mismatch(app) is None

    def test_mismatch_returns_message(self):
        app = MagicMock()
        app.storage.embedding_mismatch = {
            "stored": {"provider": "ollama", "model": "nomic-embed-text", "dimension": 768},
            "configured": {
                "provider": "openai",
                "model": "text-embedding-3-small",
                "dimension": 1536,
            },
        }
        msg = _check_embedding_mismatch(app)
        assert msg is not None
        assert "mismatch" in msg.lower()
        assert "768" in msg
        assert "1536" in msg

    def test_no_storage_attribute(self):
        """If app has no storage at all, should return None gracefully."""
        app = MagicMock(spec=[])  # no attributes
        result = _check_embedding_mismatch(app)
        assert result is None


# ===========================================================================
# helpers.py — _set_config_key
# ===========================================================================


class TestSetConfigKey:
    def test_set_int_field(self):
        config = Mem2MemConfig()
        msg = _set_config_key(config, "search.default_top_k", "20")
        assert "20" in msg
        assert config.search.default_top_k == 20

    def test_set_bool_field(self):
        config = Mem2MemConfig()
        msg = _set_config_key(config, "decay.enabled", "true")
        assert config.decay.enabled is True
        assert msg.startswith("Set ")

    def test_set_float_field(self):
        config = Mem2MemConfig()
        result = _set_config_key(config, "mmr.lambda_param", "0.5")
        assert config.mmr.lambda_param == pytest.approx(0.5)
        assert result.startswith("Set ")

    def test_set_string_field(self):
        config = Mem2MemConfig()
        result = _set_config_key(config, "namespace.default_namespace", "work")
        assert config.namespace.default_namespace == "work"
        assert result.startswith("Set ")

    def test_invalid_key_format_no_dot(self):
        config = Mem2MemConfig()
        msg = _set_config_key(config, "nodot", "value")
        assert "section.field" in msg

    def test_invalid_key_format_too_many_dots(self):
        config = Mem2MemConfig()
        msg = _set_config_key(config, "a.b.c", "value")
        assert "section.field" in msg

    def test_unknown_section(self):
        config = Mem2MemConfig()
        msg = _set_config_key(config, "nonexistent.field", "value")
        assert "not found" in msg

    def test_unknown_field(self):
        config = Mem2MemConfig()
        msg = _set_config_key(config, "search.nonexistent_field", "value")
        assert "not found" in msg

    def test_unsupported_type_rejected(self):
        config = Mem2MemConfig()
        # memory_dirs is not in MUTABLE_FIELDS, so it's rejected as read-only
        msg = _set_config_key(config, "indexing.memory_dirs", "/tmp")
        assert "not mutable" in msg.lower() or "read-only" in msg.lower()

    def test_init_only_field_rejected(self):
        """_set_config_key must reject fields not in MUTABLE_FIELDS."""
        config = Mem2MemConfig()
        original = config.embedding.provider
        msg = _set_config_key(config, "embedding.provider", "openai")
        assert "not mutable" in msg.lower() or "read-only" in msg.lower()
        assert config.embedding.provider == original  # unchanged

    def test_init_only_storage_backend_rejected(self):
        config = Mem2MemConfig()
        msg = _set_config_key(config, "storage.backend", "postgres")
        assert "not mutable" in msg.lower() or "read-only" in msg.lower()

    def test_mutable_field_still_works(self):
        """Mutable fields should still be accepted after adding the guard."""
        config = Mem2MemConfig()
        msg = _set_config_key(config, "search.default_top_k", "25")
        assert msg.startswith("Set ")
        assert config.search.default_top_k == 25

    def test_set_rrf_weights_from_csv(self):
        """MCP path: rrf_weights as CSV string → list[float]."""
        config = Mem2MemConfig()
        msg = _set_config_key(config, "search.rrf_weights", "1.5,0.8")
        assert msg.startswith("Set ")
        assert config.search.rrf_weights == [1.5, 0.8]


# ===========================================================================
# error_handler.py — @tool_handler
# ===========================================================================


class TestToolHandler:
    async def test_success_returns_result(self):
        @tool_handler
        async def good_func():
            return "all good"

        result = await good_func()
        assert result == "all good"

    async def test_value_error_returns_error_string(self):
        @tool_handler
        async def bad_func():
            raise ValueError("bad input")

        result = await bad_func()
        assert result.startswith("Error:")
        assert "bad input" in result

    async def test_storage_error_returns_error_string(self):
        @tool_handler
        async def storage_func():
            raise StorageError("DB corrupt")

        result = await storage_func()
        assert "Error:" in result
        assert "DB corrupt" in result

    async def test_generic_exception_returns_internal_error(self):
        @tool_handler
        async def crash_func():
            raise RuntimeError("segfault")

        result = await crash_func()
        assert "Error:" in result
        assert "internal error" in result
        assert "RuntimeError" in result

    async def test_preserves_function_name(self):
        @tool_handler
        async def my_named_func():
            return "ok"

        assert my_named_func.__name__ == "my_named_func"

    async def test_passes_args_through(self):
        @tool_handler
        async def add(a, b):
            return f"{a + b}"

        result = await add(3, 4)
        assert result == "7"

    async def test_file_not_found_returns_error(self):
        @tool_handler
        async def missing():
            raise FileNotFoundError("no such file")

        result = await missing()
        assert "Error:" in result
        assert "no such file" in result

    async def test_key_error_returns_error(self):
        @tool_handler
        async def key_err():
            raise KeyError("missing_key")

        result = await key_err()
        assert "Error:" in result
