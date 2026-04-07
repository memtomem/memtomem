"""Tests for tool metadata optimization (Phase 2 of gateway improvements)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from memtomem_stm.proxy.config import (
    ProxyConfig,
    ToolOverrideConfig,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.manager import ProxyManager, UpstreamConnection
from memtomem_stm.proxy.metrics import TokenTracker


# ── _truncate_description ────────────────────────────────────────────────


class TestTruncateDescription:
    def test_empty_string(self):
        assert ProxyManager._truncate_description("", 100) == ""

    def test_under_limit(self):
        assert ProxyManager._truncate_description("Short desc.", 100) == "Short desc."

    def test_at_limit(self):
        text = "x" * 200
        assert ProxyManager._truncate_description(text, 200) == text

    def test_sentence_boundary(self):
        text = "First sentence. Second sentence. Third sentence that is long."
        result = ProxyManager._truncate_description(text, 35)
        assert result == "First sentence. Second sentence."

    def test_word_boundary_fallback(self):
        text = "one two three four five six seven eight nine ten"
        result = ProxyManager._truncate_description(text, 25)
        assert result.endswith("...")
        assert len(result) <= 28  # 25 + "..."

    def test_no_space_fallback(self):
        text = "a" * 300
        result = ProxyManager._truncate_description(text, 100)
        assert result == "a" * 100 + "..."

    def test_question_mark_boundary(self):
        text = "What is this? This is a tool. It does things."
        result = ProxyManager._truncate_description(text, 20)
        assert result == "What is this?"


# ── _distill_schema ──────────────────────────────────────────────────────


class TestDistillSchema:
    def test_no_strip(self):
        schema = {"type": "object", "description": "A schema"}
        result = ProxyManager._distill_schema(schema, strip_descriptions=False)
        assert result == schema  # unchanged

    def test_strip_top_level_description(self):
        schema = {"type": "object", "description": "Remove me"}
        result = ProxyManager._distill_schema(schema, strip_descriptions=True)
        assert "description" not in result
        assert result["type"] == "object"

    def test_strip_nested_descriptions(self):
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The name"},
                "age": {"type": "integer", "description": "The age"},
            },
        }
        result = ProxyManager._distill_schema(schema, strip_descriptions=True)
        assert "description" not in result["properties"]["name"]
        assert "description" not in result["properties"]["age"]
        assert result["properties"]["name"]["type"] == "string"

    def test_strip_examples(self):
        schema = {
            "type": "object",
            "examples": [{"name": "foo"}],
            "properties": {"x": {"type": "string", "examples": ["a", "b"]}},
        }
        result = ProxyManager._distill_schema(schema, strip_descriptions=True)
        assert "examples" not in result
        assert "examples" not in result["properties"]["x"]

    def test_preserves_non_description_keys(self):
        schema = {
            "type": "object",
            "required": ["name"],
            "properties": {
                "name": {"type": "string", "enum": ["a", "b"]},
            },
        }
        result = ProxyManager._distill_schema(schema, strip_descriptions=True)
        assert result["required"] == ["name"]
        assert result["properties"]["name"]["enum"] == ["a", "b"]

    def test_list_of_schemas(self):
        schema = {
            "oneOf": [
                {"type": "string", "description": "A string"},
                {"type": "integer", "description": "An int"},
            ],
        }
        result = ProxyManager._distill_schema(schema, strip_descriptions=True)
        assert len(result["oneOf"]) == 2
        assert "description" not in result["oneOf"][0]
        assert "description" not in result["oneOf"][1]


# ── get_proxy_tools integration ──────────────────────────────────────────


def _fake_tool(name: str, description: str = "", schema: dict | None = None):
    return SimpleNamespace(
        name=name,
        description=description,
        inputSchema=schema or {"type": "object"},
        annotations=None,
    )


def _make_manager_with_tools(
    tools: list,
    tool_overrides: dict | None = None,
    max_description_chars: int = 200,
    strip_schema_descriptions: bool = False,
    server_max_desc: int = 200,
    server_strip: bool = False,
) -> ProxyManager:
    server_cfg = UpstreamServerConfig(
        prefix="test",
        tool_overrides=tool_overrides or {},
        max_description_chars=server_max_desc,
        strip_schema_descriptions=server_strip,
    )
    proxy_cfg = ProxyConfig(
        config_path=Path("/tmp/proxy.json"),
        upstream_servers={"srv": server_cfg},
        max_description_chars=max_description_chars,
        strip_schema_descriptions=strip_schema_descriptions,
    )
    mgr = ProxyManager(proxy_cfg, TokenTracker())
    conn = UpstreamConnection(
        name="srv", config=server_cfg, session=AsyncMock(), tools=tools,
    )
    mgr._connections["srv"] = conn
    return mgr


class TestGetProxyToolsFiltering:
    def test_hidden_tool_excluded(self):
        tools = [_fake_tool("visible"), _fake_tool("secret")]
        mgr = _make_manager_with_tools(
            tools,
            tool_overrides={"secret": ToolOverrideConfig(hidden=True)},
        )
        proxy_tools = mgr.get_proxy_tools()
        names = [t.original_name for t in proxy_tools]
        assert "visible" in names
        assert "secret" not in names

    def test_all_tools_visible_by_default(self):
        tools = [_fake_tool("a"), _fake_tool("b"), _fake_tool("c")]
        mgr = _make_manager_with_tools(tools)
        assert len(mgr.get_proxy_tools()) == 3


class TestGetProxyToolsDescription:
    def test_description_truncated(self):
        long_desc = "This is a very detailed description. " * 20
        tools = [_fake_tool("tool", description=long_desc)]
        mgr = _make_manager_with_tools(tools, max_description_chars=50)
        proxy_tools = mgr.get_proxy_tools()
        assert len(proxy_tools[0].description) <= 55  # 50 + "..."

    def test_description_override(self):
        tools = [_fake_tool("tool", description="Original long description.")]
        mgr = _make_manager_with_tools(
            tools,
            tool_overrides={"tool": ToolOverrideConfig(description_override="Custom desc.")},
        )
        proxy_tools = mgr.get_proxy_tools()
        assert proxy_tools[0].description == "Custom desc."

    def test_server_max_desc_used(self):
        long_desc = "x" * 500
        tools = [_fake_tool("tool", description=long_desc)]
        mgr = _make_manager_with_tools(tools, server_max_desc=100, max_description_chars=300)
        proxy_tools = mgr.get_proxy_tools()
        # min(server=100, global=300) = 100
        assert len(proxy_tools[0].description) <= 105


class TestGetProxyToolsSchema:
    def test_schema_distilled_global(self):
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The name"},
            },
        }
        tools = [_fake_tool("tool", schema=schema)]
        mgr = _make_manager_with_tools(tools, strip_schema_descriptions=True)
        proxy_tools = mgr.get_proxy_tools()
        assert "description" not in proxy_tools[0].input_schema["properties"]["name"]

    def test_schema_distilled_server(self):
        schema = {
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "Query"},
            },
        }
        tools = [_fake_tool("tool", schema=schema)]
        mgr = _make_manager_with_tools(tools, server_strip=True)
        proxy_tools = mgr.get_proxy_tools()
        assert "description" not in proxy_tools[0].input_schema["properties"]["q"]

    def test_schema_not_distilled_by_default(self):
        schema = {
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "Query"},
            },
        }
        tools = [_fake_tool("tool", schema=schema)]
        mgr = _make_manager_with_tools(tools)
        proxy_tools = mgr.get_proxy_tools()
        assert proxy_tools[0].input_schema["properties"]["q"]["description"] == "Query"


class TestTokenSavings:
    def test_50_tools_description_savings(self):
        """50 tools with 500-char descriptions → truncated to 200 saves >50%."""
        tools = [_fake_tool(f"tool_{i}", description="d" * 500) for i in range(50)]
        mgr = _make_manager_with_tools(tools, max_description_chars=200)
        proxy_tools = mgr.get_proxy_tools()
        total_chars = sum(len(t.description) for t in proxy_tools)
        original_chars = 50 * 500
        savings_pct = (1 - total_chars / original_chars) * 100
        assert savings_pct > 50
