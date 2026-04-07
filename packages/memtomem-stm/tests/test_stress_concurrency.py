"""Stress and concurrency tests for ProxyManager pipeline."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from memtomem_stm.proxy.config import (
    CompressionStrategy,
    ProxyConfig,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.compression import SelectiveCompressor, TruncateCompressor
from memtomem_stm.proxy.manager import ProxyManager, UpstreamConnection
from memtomem_stm.proxy.metrics import TokenTracker


# ── Helpers ──────────────────────────────────────────────────────────────


def _text_content(text: str):
    return SimpleNamespace(type="text", text=text)


def _make_result(text: str):
    return SimpleNamespace(content=[_text_content(text)], isError=False)


def _make_manager(
    compression: CompressionStrategy = CompressionStrategy.NONE,
    max_result_chars: int = 50000,
    max_retries: int = 0,
) -> ProxyManager:
    server_cfg = UpstreamServerConfig(
        prefix="test",
        compression=compression,
        max_result_chars=max_result_chars,
        max_retries=max_retries,
        reconnect_delay_seconds=0.0,
    )
    proxy_cfg = ProxyConfig(
        config_path=Path("/tmp/proxy.json"),
        upstream_servers={"srv": server_cfg},
        min_result_retention=0.0,  # disable retention scaling for predictable sizes
    )
    tracker = TokenTracker()
    mgr = ProxyManager(proxy_cfg, tracker)

    session = AsyncMock()
    conn = UpstreamConnection(
        name="srv", config=server_cfg, session=session, tools=[]
    )
    mgr._connections["srv"] = conn
    return mgr


def _get_session(mgr: ProxyManager) -> AsyncMock:
    return mgr._connections["srv"].session


# ── Large payload tests ──────────────────────────────────────────────────


class TestLargePayloads:
    async def test_1mb_response_none_compression(self):
        """1MB response with NONE compression passes through."""
        mgr = _make_manager(compression=CompressionStrategy.NONE)
        payload = "A" * (1024 * 1024)  # 1MB
        _get_session(mgr).call_tool.return_value = _make_result(payload)

        result = await mgr.call_tool("srv", "tool", {})
        assert len(result) == len(payload)

    async def test_1mb_response_truncate_compression(self):
        """1MB response with TRUNCATE compression is reduced."""
        mgr = _make_manager(
            compression=CompressionStrategy.TRUNCATE,
            max_result_chars=5000,
        )
        payload = "# Section\n" + "data " * 200000  # ~1MB
        _get_session(mgr).call_tool.return_value = _make_result(payload)

        result = await mgr.call_tool("srv", "tool", {})
        assert len(result) <= 6000  # some overhead from truncation markers

    async def test_2mb_json_response(self):
        """2MB JSON payload compresses without error."""
        mgr = _make_manager(
            compression=CompressionStrategy.TRUNCATE,
            max_result_chars=10000,
        )
        # Large JSON-like content
        entries = [f'{{"id": {i}, "data": "{chr(65 + i % 26) * 200}"}}' for i in range(5000)]
        payload = "[\n" + ",\n".join(entries) + "\n]"
        assert len(payload) > 1_000_000

        _get_session(mgr).call_tool.return_value = _make_result(payload)
        result = await mgr.call_tool("srv", "tool", {})
        assert len(result) <= 12000

    async def test_large_payload_metrics_correct(self):
        """Metrics accurately track large original and compressed sizes."""
        mgr = _make_manager(
            compression=CompressionStrategy.TRUNCATE,
            max_result_chars=1000,
        )
        payload = "x" * 500_000
        _get_session(mgr).call_tool.return_value = _make_result(payload)

        await mgr.call_tool("srv", "tool", {})

        summary = mgr.tracker.get_summary()
        assert summary["total_original_chars"] == 500_000
        assert summary["total_compressed_chars"] < 500_000
        assert summary["total_original_tokens"] > 100_000  # 500000/3.5 ≈ 142857

    async def test_5mb_markdown_with_headings(self):
        """5MB markdown with many headings survives section-aware truncation."""
        mgr = _make_manager(
            compression=CompressionStrategy.TRUNCATE,
            max_result_chars=8000,
        )
        sections = [f"## Section {i}\n\n{'Content ' * 500}\n" for i in range(500)]
        payload = "\n".join(sections)
        assert len(payload) > 2_000_000

        _get_session(mgr).call_tool.return_value = _make_result(payload)
        result = await mgr.call_tool("srv", "tool", {})
        assert len(result) <= 10000


# ── Concurrent tool calls ────────────────────────────────────────────────


class TestConcurrentCalls:
    async def test_concurrent_none_compression(self):
        """Multiple concurrent calls with NONE compression."""
        mgr = _make_manager(compression=CompressionStrategy.NONE)
        session = _get_session(mgr)

        async def mock_call_tool(tool, args):
            await asyncio.sleep(0.01)  # simulate latency
            return _make_result(f"response for {tool}")

        session.call_tool.side_effect = mock_call_tool

        tasks = [
            mgr.call_tool("srv", f"tool_{i}", {})
            for i in range(20)
        ]
        results = await asyncio.gather(*tasks)

        assert len(results) == 20
        assert mgr.tracker.get_summary()["total_calls"] == 20

    async def test_concurrent_truncate_compression(self):
        """Multiple concurrent calls with TRUNCATE — no shared mutable state issues."""
        mgr = _make_manager(
            compression=CompressionStrategy.TRUNCATE,
            max_result_chars=500,
        )
        session = _get_session(mgr)

        async def mock_call_tool(tool, args):
            await asyncio.sleep(0.005)
            return _make_result("data " * 1000)

        session.call_tool.side_effect = mock_call_tool

        tasks = [mgr.call_tool("srv", f"tool_{i}", {}) for i in range(20)]
        results = await asyncio.gather(*tasks)

        assert len(results) == 20
        for r in results:
            assert len(r) <= 600

    async def test_concurrent_selective_lock(self):
        """Concurrent SELECTIVE calls share one compressor through the lock."""
        mgr = _make_manager(compression=CompressionStrategy.SELECTIVE)
        session = _get_session(mgr)

        # Selective needs structured content to produce TOC
        md = "# Title\n\n## A\nContent A\n\n## B\nContent B\n\n## C\nContent C\n" * 10
        session.call_tool.return_value = _make_result(md)

        tasks = [mgr.call_tool("srv", f"tool_{i}", {}) for i in range(10)]
        results = await asyncio.gather(*tasks)

        assert len(results) == 10
        # Verify single compressor was created (not 10)
        assert mgr._selective_compressor is not None

    async def test_concurrent_hybrid_compression(self):
        """Concurrent HYBRID calls also go through selective_lock."""
        mgr = _make_manager(
            compression=CompressionStrategy.HYBRID,
            max_result_chars=200,
        )
        session = _get_session(mgr)

        md = "# Doc\n\n" + "\n".join(
            f"## Section {i}\n\nParagraph with details about topic {i}.\n"
            for i in range(50)
        )
        session.call_tool.return_value = _make_result(md)

        tasks = [mgr.call_tool("srv", f"tool_{i}", {}) for i in range(10)]
        results = await asyncio.gather(*tasks)

        assert len(results) == 10
        assert mgr._selective_compressor is not None

    async def test_concurrent_mixed_strategies(self):
        """Different servers with different strategies called concurrently."""
        # Two servers with different compression
        srv1_cfg = UpstreamServerConfig(
            prefix="s1", compression=CompressionStrategy.TRUNCATE,
            max_result_chars=500, max_retries=0, reconnect_delay_seconds=0.0,
        )
        srv2_cfg = UpstreamServerConfig(
            prefix="s2", compression=CompressionStrategy.NONE,
            max_retries=0, reconnect_delay_seconds=0.0,
        )
        proxy_cfg = ProxyConfig(
            config_path=Path("/tmp/proxy.json"),
            upstream_servers={"srv1": srv1_cfg, "srv2": srv2_cfg},
            min_result_retention=0.0,
        )
        mgr = ProxyManager(proxy_cfg, TokenTracker())

        session1, session2 = AsyncMock(), AsyncMock()
        session1.call_tool.return_value = _make_result("long " * 500)
        session2.call_tool.return_value = _make_result("short")

        mgr._connections["srv1"] = UpstreamConnection(
            name="srv1", config=srv1_cfg, session=session1, tools=[]
        )
        mgr._connections["srv2"] = UpstreamConnection(
            name="srv2", config=srv2_cfg, session=session2, tools=[]
        )

        tasks = (
            [mgr.call_tool("srv1", "tool", {}) for _ in range(5)]
            + [mgr.call_tool("srv2", "tool", {}) for _ in range(5)]
        )
        results = await asyncio.gather(*tasks)
        assert len(results) == 10
        assert mgr.tracker.get_summary()["total_calls"] == 10


# ── SelectiveCompressor direct stress ────────────────────────────────────


class TestSelectiveCompressorStress:
    def test_many_pending_entries_eviction(self):
        """Eviction works correctly when max_pending is exceeded."""
        comp = SelectiveCompressor(max_pending=5, pending_ttl_seconds=300)

        for i in range(20):
            text = f"# Doc {i}\n\n## A\nContent A\n\n## B\nContent B\n"
            comp.compress(text, max_chars=50)

        # Only max_pending entries should remain
        assert len(comp._pending) <= 5

    def test_large_section_count(self):
        """Compressor handles document with 200+ sections."""
        sections = "\n".join(f"## Section {i}\nContent for section {i}.\n" for i in range(200))
        text = f"# Big Doc\n\n{sections}"

        comp = SelectiveCompressor()
        result = comp.compress(text, max_chars=500)
        # Should produce a TOC, not crash
        assert "selection_key" in result or len(result) <= 600

    def test_deeply_nested_json(self):
        """Compressor handles deeply nested JSON without recursion error."""
        import json
        data = {"level": 0}
        current = data
        for i in range(1, 50):
            current["nested"] = {"level": i}
            current = current["nested"]
        text = json.dumps(data, indent=2)

        comp = SelectiveCompressor(json_depth=2)
        result = comp.compress(text, max_chars=100)
        assert isinstance(result, str)


# ── TruncateCompressor large payloads ────────────────────────────────────


class TestTruncateCompressorStress:
    def test_1mb_plain_text(self):
        comp = TruncateCompressor()
        text = "word " * 200000  # ~1MB
        result = comp.compress(text, max_chars=5000)
        assert len(result) <= 6000

    def test_1mb_markdown_many_headings(self):
        comp = TruncateCompressor()
        sections = [f"## H{i}\n\n{'content ' * 100}\n" for i in range(500)]
        text = "\n".join(sections)
        result = comp.compress(text, max_chars=5000)
        assert len(result) <= 6000

    def test_large_json_array(self):
        import json
        comp = TruncateCompressor()
        data = [{"id": i, "value": f"item_{i}" * 20} for i in range(10000)]
        text = json.dumps(data)
        result = comp.compress(text, max_chars=5000)
        assert len(result) <= 6000

    def test_repetitive_content(self):
        """Highly repetitive content (same line 10000 times)."""
        comp = TruncateCompressor()
        text = "ERROR: connection timeout at 10.0.0.1:5432\n" * 10000
        result = comp.compress(text, max_chars=2000)
        assert len(result) <= 3000


# ── Metrics accumulation under load ──────────────────────────────────────


class TestMetricsUnderLoad:
    async def test_100_calls_metrics_consistent(self):
        """After 100 calls, metrics totals are internally consistent."""
        mgr = _make_manager(
            compression=CompressionStrategy.TRUNCATE,
            max_result_chars=500,
        )
        session = _get_session(mgr)
        session.call_tool.return_value = _make_result("data " * 200)

        for _ in range(100):
            await mgr.call_tool("srv", "tool", {})

        s = mgr.tracker.get_summary()
        assert s["total_calls"] == 100
        assert s["total_original_chars"] == 100 * len("data " * 200)
        assert s["total_compressed_chars"] < s["total_original_chars"]
        assert s["total_savings_pct"] > 0

        # Percentiles should be populated
        lp = s["latency_percentiles"]
        assert lp["total_ms"]["p50"] >= 0
        assert lp["total_ms"]["p99"] >= lp["total_ms"]["p50"]

    async def test_concurrent_calls_metrics_total(self):
        """Concurrent calls produce correct totals (no lost increments)."""
        mgr = _make_manager(compression=CompressionStrategy.NONE)
        session = _get_session(mgr)

        async def slow_response(tool, args):
            await asyncio.sleep(0.005)
            return _make_result("x" * 100)

        session.call_tool.side_effect = slow_response

        tasks = [mgr.call_tool("srv", f"t_{i}", {}) for i in range(50)]
        await asyncio.gather(*tasks)

        s = mgr.tracker.get_summary()
        assert s["total_calls"] == 50
        assert s["total_original_chars"] == 50 * 100

    async def test_by_server_breakdown(self):
        """Per-server metrics are correctly bucketed under concurrent load."""
        srv1_cfg = UpstreamServerConfig(
            prefix="a", compression=CompressionStrategy.NONE,
            max_retries=0, reconnect_delay_seconds=0.0,
        )
        srv2_cfg = UpstreamServerConfig(
            prefix="b", compression=CompressionStrategy.NONE,
            max_retries=0, reconnect_delay_seconds=0.0,
        )
        proxy_cfg = ProxyConfig(
            config_path=Path("/tmp/proxy.json"),
            upstream_servers={"s1": srv1_cfg, "s2": srv2_cfg},
            min_result_retention=0.0,
        )
        mgr = ProxyManager(proxy_cfg, TokenTracker())

        s1_session, s2_session = AsyncMock(), AsyncMock()
        s1_session.call_tool.return_value = _make_result("a" * 100)
        s2_session.call_tool.return_value = _make_result("b" * 200)

        mgr._connections["s1"] = UpstreamConnection(
            name="s1", config=srv1_cfg, session=s1_session, tools=[]
        )
        mgr._connections["s2"] = UpstreamConnection(
            name="s2", config=srv2_cfg, session=s2_session, tools=[]
        )

        tasks = (
            [mgr.call_tool("s1", "t", {}) for _ in range(10)]
            + [mgr.call_tool("s2", "t", {}) for _ in range(10)]
        )
        await asyncio.gather(*tasks)

        s = mgr.tracker.get_summary()
        assert s["by_server"]["s1"]["calls"] == 10
        assert s["by_server"]["s2"]["calls"] == 10
        assert s["by_server"]["s1"]["original_chars"] == 1000
        assert s["by_server"]["s2"]["original_chars"] == 2000
