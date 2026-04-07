"""Tests for observability foundation (Phase 4 of gateway improvements)."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from memtomem_stm.proxy.config import CompressionStrategy, ProxyConfig, UpstreamServerConfig
from memtomem_stm.proxy.manager import ProxyManager, UpstreamConnection
from memtomem_stm.proxy.metrics import CallMetrics, RPSTracker, TokenTracker
from memtomem_stm.proxy.metrics_store import MetricsStore


# ── RPSTracker ───────────────────────────────────────────────────────────


class TestRPSTracker:
    def test_empty_returns_zero(self):
        t = RPSTracker(window_seconds=60.0)
        assert t.rps() == 0.0

    def test_single_record(self):
        t = RPSTracker(window_seconds=60.0)
        t.record()
        assert t.rps() > 0
        assert t.rps() <= 1.0  # 1 / 60 ≈ 0.017

    def test_burst(self):
        t = RPSTracker(window_seconds=60.0)
        for _ in range(100):
            t.record()
        rps = t.rps()
        # 100 / 60 ≈ 1.67
        assert 1.0 < rps < 2.0

    def test_window_expiry(self):
        t = RPSTracker(window_seconds=1.0)
        t.record()
        assert t.rps() > 0
        # Simulate time passing
        t._timestamps[0] = time.monotonic() - 2.0
        assert t.rps() == 0.0

    def test_reset(self):
        t = RPSTracker()
        for _ in range(50):
            t.record()
        t.reset()
        assert t.rps() == 0.0

    def test_small_window(self):
        t = RPSTracker(window_seconds=1.0)
        for _ in range(10):
            t.record()
        rps = t.rps()
        assert rps == 10.0  # 10 / 1


# ── TokenTracker RPS integration ─────────────────────────────────────────


class TestTokenTrackerRPS:
    def test_summary_includes_current_rps(self):
        tracker = TokenTracker()
        s = tracker.get_summary()
        assert "current_rps" in s
        assert s["current_rps"] == 0.0

    def test_record_increments_rps(self):
        tracker = TokenTracker()
        for _ in range(5):
            tracker.record(CallMetrics(server="s", tool="t", original_chars=100, compressed_chars=50))
        s = tracker.get_summary()
        assert s["current_rps"] > 0

    def test_record_error_increments_rps(self):
        tracker = TokenTracker()
        tracker.record_error(CallMetrics(
            server="s", tool="t", original_chars=0, compressed_chars=0, is_error=True,
        ))
        s = tracker.get_summary()
        assert s["current_rps"] > 0


# ── trace_id generation and propagation ──────────────────────────────────


def _text_content(text: str):
    return SimpleNamespace(type="text", text=text)


def _make_result(text: str, is_error: bool = False):
    return SimpleNamespace(content=[_text_content(text)], isError=is_error)


def _make_manager() -> ProxyManager:
    server_cfg = UpstreamServerConfig(
        prefix="test", compression=CompressionStrategy.NONE,
        max_result_chars=50000, max_retries=0, reconnect_delay_seconds=0.0,
    )
    proxy_cfg = ProxyConfig(
        config_path=Path("/tmp/proxy.json"),
        upstream_servers={"srv": server_cfg},
    )
    mgr = ProxyManager(proxy_cfg, TokenTracker())
    session = AsyncMock()
    mgr._connections["srv"] = UpstreamConnection(
        name="srv", config=server_cfg, session=session, tools=[],
    )
    return mgr


class TestTraceIdPropagation:
    async def test_success_path_has_trace_id(self):
        mgr = _make_manager()
        mgr._connections["srv"].session.call_tool.return_value = _make_result("ok")

        recorded: list[CallMetrics] = []
        original_record = mgr.tracker.record

        def capture(m):
            recorded.append(m)
            original_record(m)

        mgr.tracker.record = capture
        await mgr.call_tool("srv", "tool", {})

        assert len(recorded) == 1
        assert recorded[0].trace_id is not None
        assert len(recorded[0].trace_id) == 16

    async def test_error_path_has_trace_id(self):
        mgr = _make_manager()
        mgr._connections["srv"].session.call_tool.side_effect = ConnectionError("down")

        recorded_errors: list[CallMetrics] = []
        original_record_error = mgr.tracker.record_error

        def capture(m):
            recorded_errors.append(m)
            original_record_error(m)

        mgr.tracker.record_error = capture
        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock):
            with pytest.raises(ConnectionError):
                await mgr.call_tool("srv", "tool", {})

        assert len(recorded_errors) == 1
        assert recorded_errors[0].trace_id is not None
        assert len(recorded_errors[0].trace_id) == 16

    async def test_trace_ids_unique_per_call(self):
        mgr = _make_manager()
        mgr._connections["srv"].session.call_tool.return_value = _make_result("ok")

        trace_ids: list[str] = []
        original_record = mgr.tracker.record

        def capture(m):
            trace_ids.append(m.trace_id)
            original_record(m)

        mgr.tracker.record = capture
        await mgr.call_tool("srv", "tool", {})
        await mgr.call_tool("srv", "tool", {})

        assert len(trace_ids) == 2
        assert trace_ids[0] != trace_ids[1]


# ── MetricsStore trace_id ────────────────────────────────────────────────


class TestMetricsStoreTraceId:
    def test_trace_id_column_exists(self, tmp_path):
        store = MetricsStore(tmp_path / "test.db")
        store.initialize()
        cols = {row[1] for row in store._db.execute("PRAGMA table_info(proxy_metrics)")}
        assert "trace_id" in cols
        store.close()

    def test_trace_id_stored(self, tmp_path):
        store = MetricsStore(tmp_path / "test.db")
        store.initialize()
        store.record(CallMetrics(
            server="srv", tool="tool", original_chars=100, compressed_chars=50,
            trace_id="abc123def456gh",
        ))
        row = store._db.execute("SELECT trace_id FROM proxy_metrics").fetchone()
        assert row[0] == "abc123def456gh"
        store.close()


# ── get_upstream_health ──────────────────────────────────────────────────


class TestUpstreamHealth:
    def test_single_connected_server(self):
        mgr = _make_manager()
        health = mgr.get_upstream_health()
        assert "srv" in health
        assert health["srv"]["connected"] is True
        assert health["srv"]["tools"] == 0

    def test_server_with_tools(self):
        mgr = _make_manager()
        fake_tools = [SimpleNamespace(name=f"t{i}") for i in range(5)]
        mgr._connections["srv"].tools = fake_tools
        health = mgr.get_upstream_health()
        assert health["srv"]["tools"] == 5

    def test_empty_connections(self):
        mgr = _make_manager()
        mgr._connections.clear()
        health = mgr.get_upstream_health()
        assert health == {}
