"""Tests for context-window-aware compression (Phase 3 of gateway improvements)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from memtomem_stm.proxy.config import (
    MODEL_CONTEXT_WINDOWS,
    ProxyConfig,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.manager import ProxyManager, UpstreamConnection
from memtomem_stm.proxy.metrics import TokenTracker


# ── MODEL_CONTEXT_WINDOWS lookup ─────────────────────────────────────────


class TestModelContextWindows:
    def test_known_models(self):
        assert MODEL_CONTEXT_WINDOWS["claude-sonnet-4"] == 200000
        assert MODEL_CONTEXT_WINDOWS["gpt-4o"] == 128000
        assert MODEL_CONTEXT_WINDOWS["gpt-4"] == 8192

    def test_unknown_model(self):
        assert "unknown-model" not in MODEL_CONTEXT_WINDOWS


# ── effective_max_result_chars ───────────────────────────────────────────


class TestEffectiveMaxResultChars:
    def test_no_model_returns_default(self):
        cfg = ProxyConfig(consumer_model="")
        assert cfg.effective_max_result_chars() == 16000

    def test_large_context_model_capped(self):
        """claude-sonnet-4: 200K * 0.05 * 3.5 = 35000 → capped at 16000."""
        cfg = ProxyConfig(consumer_model="claude-sonnet-4", context_budget_ratio=0.05)
        assert cfg.effective_max_result_chars() == 16000

    def test_small_context_model(self):
        """gpt-4: 8192 * 0.05 * 3.5 = 1433."""
        cfg = ProxyConfig(consumer_model="gpt-4", context_budget_ratio=0.05)
        result = cfg.effective_max_result_chars()
        assert result == int(8192 * 0.05 * 3.5)
        assert result == 1433

    def test_custom_ratio(self):
        """gpt-4: 8192 * 0.1 * 3.5 = 2867."""
        cfg = ProxyConfig(consumer_model="gpt-4", context_budget_ratio=0.1)
        assert cfg.effective_max_result_chars() == int(8192 * 0.1 * 3.5)

    def test_unknown_model_returns_default(self):
        cfg = ProxyConfig(consumer_model="llama-3-70b", default_max_result_chars=10000)
        assert cfg.effective_max_result_chars() == 10000

    def test_prefix_matching(self):
        """'claude-sonnet-4-20250514' matches 'claude-sonnet-4'."""
        cfg = ProxyConfig(consumer_model="claude-sonnet-4-20250514")
        # Should match and compute normally (capped at default)
        assert cfg.effective_max_result_chars() == 16000

    def test_explicit_cap(self):
        """Model budget exceeds explicit cap → cap wins."""
        cfg = ProxyConfig(
            consumer_model="claude-sonnet-4",
            default_max_result_chars=5000,
            context_budget_ratio=0.05,
        )
        # 200K * 0.05 * 3.5 = 35000 → capped at 5000
        assert cfg.effective_max_result_chars() == 5000

    def test_zero_ratio(self):
        cfg = ProxyConfig(consumer_model="gpt-4", context_budget_ratio=0.0)
        assert cfg.effective_max_result_chars() == 0

    def test_gpt4o_budget(self):
        """gpt-4o: 128K * 0.05 * 3.5 = 22400 → capped at 16000."""
        cfg = ProxyConfig(consumer_model="gpt-4o")
        assert cfg.effective_max_result_chars() == 16000


# ── _resolve_tool_config integration ─────────────────────────────────────


class TestResolveToolConfigModelAware:
    def _make_manager(
        self,
        server_max_chars: int = 2000,  # default
        consumer_model: str = "",
        context_budget_ratio: float = 0.05,
    ) -> ProxyManager:
        server_cfg = UpstreamServerConfig(
            prefix="test",
            max_result_chars=server_max_chars,
        )
        proxy_cfg = ProxyConfig(
            config_path=Path("/tmp/proxy.json"),
            upstream_servers={"srv": server_cfg},
            consumer_model=consumer_model,
            context_budget_ratio=context_budget_ratio,
        )
        mgr = ProxyManager(proxy_cfg, TokenTracker())
        conn = UpstreamConnection(
            name="srv", config=server_cfg, session=AsyncMock(), tools=[],
        )
        mgr._connections["srv"] = conn
        return mgr

    def test_default_server_uses_model_budget(self):
        """Server at default max_result_chars picks up model-aware budget."""
        mgr = self._make_manager(consumer_model="gpt-4", context_budget_ratio=0.05)
        tc = mgr._resolve_tool_config("srv", "any_tool")
        assert tc.max_chars == 1433  # gpt-4 budget

    def test_explicit_server_overrides_model(self):
        """Server with explicit max_result_chars=5000 ignores model budget."""
        mgr = self._make_manager(
            server_max_chars=5000,
            consumer_model="gpt-4",
        )
        tc = mgr._resolve_tool_config("srv", "any_tool")
        assert tc.max_chars == 5000

    def test_no_model_uses_default(self):
        """No consumer_model → server default used."""
        mgr = self._make_manager(consumer_model="")
        tc = mgr._resolve_tool_config("srv", "any_tool")
        # effective_max_result_chars returns 16000, which is > server default 2000
        # But server at default → uses effective, which is 16000
        assert tc.max_chars == 16000
