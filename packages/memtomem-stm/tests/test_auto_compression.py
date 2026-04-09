"""Tests for AUTO compression strategy in ProxyManager pipeline."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock


from memtomem_stm.proxy.config import (
    CompressionStrategy,
    ProxyConfig,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.manager import ProxyManager, UpstreamConnection
from memtomem_stm.proxy.metrics import TokenTracker


# ── Helpers ──────────────────────────────────────────────────────────────


def _text_content(text: str):
    return SimpleNamespace(type="text", text=text)


def _make_result(text: str):
    return SimpleNamespace(content=[_text_content(text)], isError=False)


def _make_manager(
    max_result_chars: int = 2000,
    min_retention: float = 0.0,
) -> ProxyManager:
    server_cfg = UpstreamServerConfig(
        prefix="test",
        compression=CompressionStrategy.AUTO,
        max_result_chars=max_result_chars,
        max_retries=0,
        reconnect_delay_seconds=0.0,
    )
    proxy_cfg = ProxyConfig(
        config_path=Path("/tmp/proxy.json"),
        upstream_servers={"srv": server_cfg},
        min_result_retention=min_retention,
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


# ── Passthrough: content fits within budget ──────────────────────────────


class TestAutoPassthrough:
    async def test_short_content_passthrough(self):
        """Content shorter than max_chars passes through uncompressed."""
        mgr = _make_manager(max_result_chars=5000)
        text = "Hello world, this is a short response."
        _get_session(mgr).call_tool.return_value = _make_result(text)

        result = await mgr.call_tool("srv", "tool", {})
        assert result == text

    async def test_exactly_at_budget_passthrough(self):
        """Content exactly at budget passes through."""
        mgr = _make_manager(max_result_chars=100)
        text = "x" * 100
        _get_session(mgr).call_tool.return_value = _make_result(text)

        result = await mgr.call_tool("srv", "tool", {})
        assert result == text


# ── Auto selects TRUNCATE for plain text ─────────────────────────────────


class TestAutoTruncate:
    async def test_plain_text_over_budget_truncated(self):
        """Plain text exceeding budget gets TRUNCATE."""
        mgr = _make_manager(max_result_chars=200)
        text = "Some important data. " * 100  # ~2100 chars
        _get_session(mgr).call_tool.return_value = _make_result(text)

        result = await mgr.call_tool("srv", "tool", {})
        assert len(result) < len(text)

    async def test_small_markdown_truncated(self):
        """Small markdown (few headings) gets TRUNCATE, not HYBRID."""
        mgr = _make_manager(max_result_chars=200)
        text = "# Title\n\n## Section\n\nSome content here.\n\n" * 20
        _get_session(mgr).call_tool.return_value = _make_result(text)

        result = await mgr.call_tool("srv", "tool", {})
        assert len(result) < len(text)


# ── Auto selects SCHEMA_PRUNING for large JSON arrays ────────────────────


class TestAutoSchemaPruning:
    async def test_json_large_array(self):
        """JSON with 20+ item array triggers SCHEMA_PRUNING."""
        mgr = _make_manager(max_result_chars=500)
        data = [{"id": i, "name": f"item_{i}", "value": i * 10} for i in range(30)]
        text = json.dumps(data, indent=2)
        _get_session(mgr).call_tool.return_value = _make_result(text)

        result = await mgr.call_tool("srv", "tool", {})
        assert len(result) < len(text)
        # Schema pruning should sample items, not just truncate
        assert "id" in result

    async def test_json_dict_with_large_array(self):
        """JSON dict containing a 20+ item array."""
        mgr = _make_manager(max_result_chars=500)
        data = {"results": [{"k": i} for i in range(25)], "total": 25}
        text = json.dumps(data, indent=2)
        _get_session(mgr).call_tool.return_value = _make_result(text)

        result = await mgr.call_tool("srv", "tool", {})
        assert len(result) < len(text)


# ── Auto selects SKELETON for API docs ───────────────────────────────────


class TestAutoSkeleton:
    async def test_api_docs_skeleton(self):
        """Markdown with HTTP methods in headings → SKELETON."""
        mgr = _make_manager(max_result_chars=500)
        sections = []
        for method, path in [
            ("GET", "/users"), ("POST", "/users"), ("GET", "/users/{id}"),
            ("PUT", "/users/{id}"), ("DELETE", "/users/{id}"),
        ]:
            sections.append(
                f"## {method} {path}\n\nDescription of endpoint.\n\n"
                f"### Parameters\n\n- `id`: string\n\n"
                f"### Response\n\n```json\n{{\"status\": \"ok\"}}\n```\n"
            )
        text = "# API Reference\n\n" + "\n".join(sections)
        _get_session(mgr).call_tool.return_value = _make_result(text)

        result = await mgr.call_tool("srv", "tool", {})
        assert len(result) < len(text)
        # Skeleton preserves headings
        assert "API Reference" in result


# ── Auto selects HYBRID for large structured markdown ────────────────────


class TestAutoHybrid:
    async def test_large_markdown_hybrid(self):
        """Large markdown (5+ headings, 5KB+) → HYBRID."""
        mgr = _make_manager(max_result_chars=1000)
        sections = [
            f"## Section {i}\n\n{'Detailed content about topic. ' * 50}\n"
            for i in range(10)
        ]
        text = "# Documentation\n\n" + "\n".join(sections)
        assert len(text) > 5000
        _get_session(mgr).call_tool.return_value = _make_result(text)

        result = await mgr.call_tool("srv", "tool", {})
        assert len(result) < len(text)

    async def test_code_heavy_hybrid(self):
        """Code-heavy content (6+ fences, 5KB+) → HYBRID."""
        mgr = _make_manager(max_result_chars=1000)
        blocks = [
            f"## Module {i}\n\n```python\ndef func_{i}(x, y):\n"
            f"    # Process data for module {i}\n"
            f"    result = x * y + {i}\n"
            f"    return result\n```\n\nExplanation of module {i} functionality and usage patterns.\n"
            for i in range(40)
        ]
        text = "\n".join(blocks)
        assert len(text) > 5000
        _get_session(mgr).call_tool.return_value = _make_result(text)

        result = await mgr.call_tool("srv", "tool", {})
        assert len(result) < len(text)


# ── AUTO is the new default ──────────────────────────────────────────────


class TestAutoIsDefault:
    def test_upstream_server_default(self):
        cfg = UpstreamServerConfig(prefix="test")
        assert cfg.compression == CompressionStrategy.AUTO

    def test_proxy_config_default(self):
        cfg = ProxyConfig()
        assert cfg.default_compression == CompressionStrategy.AUTO


# ── Override still works ─────────────────────────────────────────────────


class TestOverrideBypassesAuto:
    async def test_explicit_none_skips_auto(self):
        """Explicitly setting NONE bypasses auto selection."""
        server_cfg = UpstreamServerConfig(
            prefix="test",
            compression=CompressionStrategy.NONE,
            max_result_chars=100,
            max_retries=0,
            reconnect_delay_seconds=0.0,
        )
        proxy_cfg = ProxyConfig(
            config_path=Path("/tmp/proxy.json"),
            upstream_servers={"srv": server_cfg},
            min_result_retention=0.0,
        )
        mgr = ProxyManager(proxy_cfg, TokenTracker())
        session = AsyncMock()
        mgr._connections["srv"] = UpstreamConnection(
            name="srv", config=server_cfg, session=session, tools=[]
        )

        text = "x" * 5000  # way over budget, but NONE = no compression
        session.call_tool.return_value = _make_result(text)

        result = await mgr.call_tool("srv", "tool", {})
        assert result == text  # passthrough despite being over budget

    async def test_explicit_truncate_skips_auto(self):
        """Explicitly setting TRUNCATE uses truncate regardless of content."""
        server_cfg = UpstreamServerConfig(
            prefix="test",
            compression=CompressionStrategy.TRUNCATE,
            max_result_chars=200,
            max_retries=0,
            reconnect_delay_seconds=0.0,
        )
        proxy_cfg = ProxyConfig(
            config_path=Path("/tmp/proxy.json"),
            upstream_servers={"srv": server_cfg},
            min_result_retention=0.0,
        )
        mgr = ProxyManager(proxy_cfg, TokenTracker())
        session = AsyncMock()
        mgr._connections["srv"] = UpstreamConnection(
            name="srv", config=server_cfg, session=session, tools=[]
        )

        # JSON with large array — AUTO would pick SCHEMA_PRUNING, but TRUNCATE is forced
        data = [{"id": i} for i in range(50)]
        text = json.dumps(data)
        session.call_tool.return_value = _make_result(text)

        result = await mgr.call_tool("srv", "tool", {})
        assert len(result) <= 300
