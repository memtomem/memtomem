"""Tests for remaining STM modules: _fastmcp_compat, tracing, protocols, metrics, mcp_client."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from memtomem_stm.proxy._fastmcp_compat import (
    _PASSTHROUGH_METADATA,
    _ProxyPassthroughArgs,
    register_proxy_tool,
)
from memtomem_stm.proxy.metrics import CallMetrics, TokenTracker
from memtomem_stm.proxy.protocols import FileIndexer, IndexResult
from memtomem_stm.surfacing.mcp_client import McpClientSearchAdapter, RemoteSearchResult


# ── _fastmcp_compat ────────────────────────────────────────────────────


class TestProxyPassthroughArgs:
    """Test the _ProxyPassthroughArgs pydantic model for extra-field forwarding."""

    def test_accepts_arbitrary_fields(self) -> None:
        args = _ProxyPassthroughArgs(foo="bar", count=42)
        assert args.__pydantic_extra__ == {"foo": "bar", "count": 42}

    def test_model_dump_one_level_includes_extras(self) -> None:
        args = _ProxyPassthroughArgs(query="test", top_k=5)
        dumped = args.model_dump_one_level()
        assert dumped["query"] == "test"
        assert dumped["top_k"] == 5

    def test_model_dump_one_level_empty(self) -> None:
        args = _ProxyPassthroughArgs()
        dumped = args.model_dump_one_level()
        assert isinstance(dumped, dict)


class TestPassthroughMetadata:
    """Test the singleton _PASSTHROUGH_METADATA FuncMetadata."""

    def test_arg_model_set(self) -> None:
        assert _PASSTHROUGH_METADATA.arg_model is _ProxyPassthroughArgs

    def test_output_schema_is_none(self) -> None:
        assert _PASSTHROUGH_METADATA.output_schema is None

    def test_wrap_output_false(self) -> None:
        assert _PASSTHROUGH_METADATA.wrap_output is False


class TestRegisterProxyTool:
    """Test register_proxy_tool patches the tool manager correctly."""

    def test_register_sets_parameters_and_metadata(self) -> None:
        mock_server = MagicMock()
        mock_tool = MagicMock()
        mock_server._tool_manager._tools.get.return_value = mock_tool

        @dataclass
        class FakeInfo:
            prefixed_name: str = "srv__my_tool"
            description: str = "does things"
            input_schema: dict = None
            annotations: Any = None

            def __post_init__(self):
                if self.input_schema is None:
                    self.input_schema = {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                    }

        info = FakeInfo()
        handler = MagicMock()

        register_proxy_tool(mock_server, handler, info)

        mock_server.add_tool.assert_called_once_with(
            handler,
            name="srv__my_tool",
            description="[proxied] does things",
            annotations=None,
        )
        assert mock_tool.parameters == info.input_schema
        assert mock_tool.fn_metadata is _PASSTHROUGH_METADATA

    def test_register_skips_patch_when_tool_not_found(self) -> None:
        mock_server = MagicMock()
        mock_server._tool_manager._tools.get.return_value = None

        @dataclass
        class FakeInfo:
            prefixed_name: str = "missing__tool"
            description: str = "gone"
            input_schema: dict = None
            annotations: Any = None

            def __post_init__(self):
                if self.input_schema is None:
                    self.input_schema = {}

        register_proxy_tool(mock_server, MagicMock(), FakeInfo())
        # Should not raise


# ── tracing ─────────────────────────────────────────────────────────────


class TestTracing:
    """Test Langfuse tracing graceful fallbacks."""

    def test_init_disabled_config_returns_none(self) -> None:
        import memtomem_stm.observability.tracing as tracing_mod

        old = tracing_mod._langfuse_client
        try:
            config = MagicMock()
            config.enabled = False
            result = tracing_mod.init_langfuse(config)
            assert result is None
        finally:
            tracing_mod._langfuse_client = old

    def test_init_missing_langfuse_returns_none(self) -> None:
        import memtomem_stm.observability.tracing as tracing_mod

        old = tracing_mod._langfuse_client
        try:
            config = MagicMock()
            config.enabled = True
            # Temporarily make langfuse unimportable
            with patch.dict(sys.modules, {"langfuse": None}):
                result = tracing_mod.init_langfuse(config)
            assert result is None
        finally:
            tracing_mod._langfuse_client = old

    def test_init_with_langfuse_installed(self) -> None:
        import memtomem_stm.observability.tracing as tracing_mod

        old = tracing_mod._langfuse_client
        old_env = os.environ.get("OTEL_SERVICE_NAME")
        try:
            mock_langfuse_cls = MagicMock()
            mock_client = MagicMock()
            mock_langfuse_cls.return_value = mock_client
            mock_module = MagicMock()
            mock_module.Langfuse = mock_langfuse_cls

            config = MagicMock()
            config.enabled = True
            config.public_key = "pk-test"
            config.secret_key = "sk-test"
            config.host = "http://localhost:3000"

            os.environ.pop("OTEL_SERVICE_NAME", None)
            with patch.dict(sys.modules, {"langfuse": mock_module}):
                result = tracing_mod.init_langfuse(config)

            assert result is mock_client
            mock_langfuse_cls.assert_called_once_with(
                public_key="pk-test",
                secret_key="sk-test",
                host="http://localhost:3000",
            )
            assert os.environ.get("OTEL_SERVICE_NAME") == "memtomem-stm"
        finally:
            tracing_mod._langfuse_client = old
            if old_env is not None:
                os.environ["OTEL_SERVICE_NAME"] = old_env
            else:
                os.environ.pop("OTEL_SERVICE_NAME", None)

    def test_traced_returns_nullcontext_when_no_client(self) -> None:
        import memtomem_stm.observability.tracing as tracing_mod

        old = tracing_mod._langfuse_client
        try:
            tracing_mod._langfuse_client = None
            ctx = tracing_mod.traced("test-span")
            # nullcontext is usable as a context manager
            with ctx:
                pass
        finally:
            tracing_mod._langfuse_client = old

    def test_shutdown_langfuse_calls_shutdown(self) -> None:
        import memtomem_stm.observability.tracing as tracing_mod

        mock_client = MagicMock()
        tracing_mod.shutdown_langfuse(mock_client)
        mock_client.shutdown.assert_called_once()

    def test_shutdown_langfuse_none_safe(self) -> None:
        import memtomem_stm.observability.tracing as tracing_mod

        # Should not raise
        tracing_mod.shutdown_langfuse(None)

    def test_get_langfuse_returns_current(self) -> None:
        import memtomem_stm.observability.tracing as tracing_mod

        old = tracing_mod._langfuse_client
        try:
            sentinel = object()
            tracing_mod._langfuse_client = sentinel
            assert tracing_mod.get_langfuse() is sentinel
        finally:
            tracing_mod._langfuse_client = old


# ── protocols ───────────────────────────────────────────────────────────


class TestProtocols:
    """Test protocol/dataclass definitions."""

    def test_index_result_defaults(self) -> None:
        r = IndexResult()
        assert r.indexed_chunks == 0

    def test_index_result_custom(self) -> None:
        r = IndexResult(indexed_chunks=42)
        assert r.indexed_chunks == 42

    def test_file_indexer_protocol_structural(self) -> None:
        """A class with the right async method satisfies FileIndexer structurally."""

        class MyIndexer:
            async def index_file(
                self, path: Path, *, force: bool = False, namespace: str | None = None
            ) -> IndexResult:
                return IndexResult(indexed_chunks=1)

        # Runtime check: isinstance with Protocol requires runtime_checkable,
        # but structural typing means it should at least be assignable.
        indexer: FileIndexer = MyIndexer()
        assert hasattr(indexer, "index_file")


# ── metrics ─────────────────────────────────────────────────────────────


class TestCallMetrics:
    """Test CallMetrics dataclass."""

    def test_defaults(self) -> None:
        m = CallMetrics(server="srv", tool="t", original_chars=100, compressed_chars=80)
        assert m.cleaned_chars == 0
        assert m.original_tokens == 0
        assert m.trace_id is None


class TestTokenTracker:
    """Test TokenTracker aggregation logic."""

    def test_empty_summary(self, token_tracker: TokenTracker) -> None:
        s = token_tracker.get_summary()
        assert s["total_calls"] == 0
        assert s["total_savings_pct"] == 0.0
        assert s["cache_hits"] == 0
        assert s["reconnects"] == 0

    def test_record_updates_totals(self, token_tracker: TokenTracker) -> None:
        m = CallMetrics(server="s1", tool="tool_a", original_chars=1000, compressed_chars=600)
        token_tracker.record(m)
        s = token_tracker.get_summary()
        assert s["total_calls"] == 1
        assert s["total_original_chars"] == 1000
        assert s["total_compressed_chars"] == 600
        assert s["total_savings_pct"] == 40.0

    def test_record_by_server_breakdown(self, token_tracker: TokenTracker) -> None:
        token_tracker.record(
            CallMetrics(server="alpha", tool="t1", original_chars=500, compressed_chars=250)
        )
        token_tracker.record(
            CallMetrics(server="beta", tool="t2", original_chars=200, compressed_chars=200)
        )
        s = token_tracker.get_summary()
        assert "alpha" in s["by_server"]
        assert s["by_server"]["alpha"]["savings_pct"] == 50.0
        assert s["by_server"]["beta"]["savings_pct"] == 0.0

    def test_cache_hit_miss_counters(self, token_tracker: TokenTracker) -> None:
        token_tracker.record_cache_hit()
        token_tracker.record_cache_hit()
        token_tracker.record_cache_miss()
        s = token_tracker.get_summary()
        assert s["cache_hits"] == 2
        assert s["cache_misses"] == 1

    def test_reconnect_counter(self, token_tracker: TokenTracker) -> None:
        token_tracker.record_reconnect()
        assert token_tracker.get_summary()["reconnects"] == 1

    def test_persist_failure_swallowed(self) -> None:
        mock_store = MagicMock()
        mock_store.record.side_effect = RuntimeError("db locked")
        tracker = TokenTracker(metrics_store=mock_store)
        # Should not raise
        tracker.record(CallMetrics(server="s", tool="t", original_chars=10, compressed_chars=5))
        assert tracker.get_summary()["total_calls"] == 1


# ── mcp_client ──────────────────────────────────────────────────────────


class TestRemoteSearchResult:
    """Test RemoteSearchResult and its fake inner classes."""

    def test_construction(self) -> None:
        r = RemoteSearchResult(content="hello world", score=0.85, source="notes.md")
        assert r.score == 0.85
        assert r.chunk.content == "hello world"
        assert r.chunk.metadata.source_file == Path("notes.md")
        assert r.chunk.metadata.namespace == "default"

    def test_default_namespace(self) -> None:
        r = RemoteSearchResult(content="x", score=0.5)
        assert r.chunk.metadata.namespace == "default"


class TestMcpClientParseResults:
    """Test the static _parse_results method of McpClientSearchAdapter."""

    def test_empty_text(self) -> None:
        results = McpClientSearchAdapter._parse_results("")
        assert results == []

    def test_single_result(self) -> None:
        text = "--- [0.92] notes.md ---\nSome memory content here"
        results = McpClientSearchAdapter._parse_results(text)
        assert len(results) == 1
        assert results[0].score == 0.92
        assert "Some memory content" in results[0].chunk.content

    def test_multiple_results(self) -> None:
        text = (
            "--- [0.9] a.md ---\nFirst result\n"
            "--- [0.7] b.md ---\nSecond result\n"
            "--- [0.5] c.md ---\nThird result"
        )
        results = McpClientSearchAdapter._parse_results(text)
        assert len(results) == 3
        assert results[0].score == 0.9
        assert results[2].score == 0.5

    def test_source_extraction(self) -> None:
        text = "--- [0.8] /path/to/doc.md ---\nContent"
        results = McpClientSearchAdapter._parse_results(text)
        assert len(results) == 1
        assert "doc.md" in str(results[0].chunk.metadata.source_file)

    def test_content_truncated_to_500(self) -> None:
        long_content = "x" * 1000
        text = f"--- [0.5] file.md ---\n{long_content}"
        results = McpClientSearchAdapter._parse_results(text)
        assert len(results) == 1
        assert len(results[0].chunk.content) <= 500


class TestMcpClientSearchAdapter:
    """Test McpClientSearchAdapter initialization and search with mock session."""

    def test_init_stores_config(self) -> None:
        from memtomem_stm.surfacing.config import SurfacingConfig

        cfg = SurfacingConfig(ltm_mode="mcp_client", ltm_mcp_command="test-server")
        adapter = McpClientSearchAdapter(cfg)
        assert adapter._config is cfg
        assert adapter._session is None

    @pytest.mark.asyncio
    async def test_search_returns_empty_when_no_session(self) -> None:
        from memtomem_stm.surfacing.config import SurfacingConfig

        adapter = McpClientSearchAdapter(SurfacingConfig())
        results, stats = await adapter.search("test query")
        assert results == []
        assert stats is None

    @pytest.mark.asyncio
    async def test_search_calls_mem_search(self) -> None:
        from memtomem_stm.surfacing.config import SurfacingConfig

        adapter = McpClientSearchAdapter(SurfacingConfig())

        mock_content = MagicMock()
        mock_content.type = "text"
        mock_content.text = "--- [0.9] notes.md ---\nRelevant memory"

        mock_result = MagicMock()
        mock_result.content = [mock_content]

        mock_session = AsyncMock()
        mock_session.call_tool.return_value = mock_result
        adapter._session = mock_session

        results, _ = await adapter.search("what is X", top_k=5)
        mock_session.call_tool.assert_awaited_once_with(
            "mem_search", {"query": "what is X", "top_k": 5}
        )
        assert len(results) == 1
        assert results[0].score == 0.9

    @pytest.mark.asyncio
    async def test_search_handles_exception(self) -> None:
        from memtomem_stm.surfacing.config import SurfacingConfig

        adapter = McpClientSearchAdapter(SurfacingConfig())
        mock_session = AsyncMock()
        mock_session.call_tool.side_effect = ConnectionError("lost")
        adapter._session = mock_session

        results, stats = await adapter.search("query")
        assert results == []
        assert stats is None
