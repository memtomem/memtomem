"""Tests for ProxyManager — ToolConfig resolution and pipeline logic."""

from __future__ import annotations


import pytest

from memtomem_stm.proxy.config import (
    CleaningConfig,
    CompressionStrategy,
    SelectiveConfig,
    ToolOverrideConfig,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.manager import ToolConfig


# ── ToolConfig resolution tests ──────────────────────────────────────────
# These test the _resolve_tool_config logic by constructing ToolConfig manually
# from the same cascading rules the ProxyManager uses.


def _resolve(server_cfg: UpstreamServerConfig, tool: str, global_auto_index: bool = False):
    """Replicate _resolve_tool_config logic without needing a full ProxyManager."""
    compression = server_cfg.compression
    max_chars = server_cfg.max_result_chars
    llm_cfg = server_cfg.llm
    sel_cfg = server_cfg.selective
    hybrid_cfg = server_cfg.hybrid
    cleaning_cfg = server_cfg.cleaning or CleaningConfig()
    auto_index_enabled = global_auto_index
    if server_cfg.auto_index is not None:
        auto_index_enabled = server_cfg.auto_index

    override = server_cfg.tool_overrides.get(tool)
    if override is not None:
        if override.compression is not None:
            compression = override.compression
        if override.max_result_chars is not None:
            max_chars = override.max_result_chars
        if override.llm is not None:
            llm_cfg = override.llm
        if override.selective is not None:
            sel_cfg = override.selective
        if override.hybrid is not None:
            hybrid_cfg = override.hybrid
        if override.cleaning is not None:
            cleaning_cfg = override.cleaning
        if override.auto_index is not None:
            auto_index_enabled = override.auto_index

    return ToolConfig(
        compression=compression,
        max_chars=max_chars,
        llm=llm_cfg,
        auto_index_enabled=auto_index_enabled,
        selective=sel_cfg,
        cleaning=cleaning_cfg,
        hybrid=hybrid_cfg,
    )


class TestToolConfigResolution:
    def test_server_defaults(self):
        cfg = UpstreamServerConfig(prefix="gh", compression=CompressionStrategy.HYBRID)
        tc = _resolve(cfg, "list_repos")
        assert tc.compression == CompressionStrategy.HYBRID
        assert tc.max_chars == 8000  # UpstreamServerConfig default
        assert tc.cleaning.enabled is True

    def test_tool_override_compression(self):
        cfg = UpstreamServerConfig(
            prefix="gh",
            compression=CompressionStrategy.HYBRID,
            tool_overrides={
                "search_code": ToolOverrideConfig(compression=CompressionStrategy.TRUNCATE)
            },
        )
        tc_default = _resolve(cfg, "list_repos")
        tc_override = _resolve(cfg, "search_code")
        assert tc_default.compression == CompressionStrategy.HYBRID
        assert tc_override.compression == CompressionStrategy.TRUNCATE

    def test_tool_override_max_chars(self):
        cfg = UpstreamServerConfig(
            prefix="gh",
            max_result_chars=2000,
            tool_overrides={
                "get_file": ToolOverrideConfig(max_result_chars=50000)
            },
        )
        tc = _resolve(cfg, "get_file")
        assert tc.max_chars == 50000

    def test_tool_override_cleaning(self):
        cfg = UpstreamServerConfig(
            prefix="gh",
            tool_overrides={
                "raw_output": ToolOverrideConfig(cleaning=CleaningConfig(strip_html=False))
            },
        )
        tc = _resolve(cfg, "raw_output")
        assert tc.cleaning.strip_html is False

    def test_tool_override_auto_index(self):
        cfg = UpstreamServerConfig(
            prefix="gh",
            auto_index=False,
            tool_overrides={
                "search_code": ToolOverrideConfig(auto_index=True)
            },
        )
        tc_default = _resolve(cfg, "list_repos")
        tc_override = _resolve(cfg, "search_code")
        assert tc_default.auto_index_enabled is False
        assert tc_override.auto_index_enabled is True

    def test_global_auto_index_default(self):
        cfg = UpstreamServerConfig(prefix="gh")
        tc = _resolve(cfg, "tool", global_auto_index=True)
        assert tc.auto_index_enabled is True

    def test_server_auto_index_overrides_global(self):
        cfg = UpstreamServerConfig(prefix="gh", auto_index=False)
        tc = _resolve(cfg, "tool", global_auto_index=True)
        assert tc.auto_index_enabled is False

    def test_no_override_preserves_server_config(self):
        sel = SelectiveConfig(max_pending=50)
        cfg = UpstreamServerConfig(
            prefix="gh",
            selective=sel,
            compression=CompressionStrategy.SELECTIVE,
        )
        tc = _resolve(cfg, "any_tool")
        assert tc.selective is sel
        assert tc.selective.max_pending == 50


class TestToolConfigFrozen:
    def test_toolconfig_is_immutable(self):
        tc = ToolConfig(
            compression=CompressionStrategy.NONE,
            max_chars=1000,
            llm=None,
            auto_index_enabled=False,
            selective=None,
            cleaning=CleaningConfig(),
            hybrid=None,
        )
        with pytest.raises(AttributeError):
            tc.compression = CompressionStrategy.TRUNCATE
