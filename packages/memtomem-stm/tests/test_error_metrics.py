"""Tests for error classification metrics (Phase 1 of gateway improvements)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from memtomem_stm.proxy.config import CompressionStrategy, ProxyConfig, UpstreamServerConfig
from memtomem_stm.proxy.manager import ProxyManager, UpstreamConnection
from memtomem_stm.proxy.metrics import CallMetrics, ErrorCategory, TokenTracker
from memtomem_stm.proxy.metrics_store import MetricsStore
from memtomem_stm.utils.circuit_breaker import CircuitBreaker


# ── ErrorCategory enum ───────────────────────────────────────────────────


class TestErrorCategory:
    def test_values(self):
        assert ErrorCategory.TRANSPORT == "transport"
        assert ErrorCategory.TIMEOUT == "timeout"
        assert ErrorCategory.PROTOCOL == "protocol"
        assert ErrorCategory.UPSTREAM_ERROR == "upstream_error"
        assert ErrorCategory.PROGRAMMING == "programming"

    def test_is_strenum(self):
        assert isinstance(ErrorCategory.TRANSPORT, str)


# ── CallMetrics error fields ─────────────────────────────────────────────


class TestCallMetricsErrorFields:
    def test_defaults(self):
        m = CallMetrics(server="s", tool="t", original_chars=100, compressed_chars=50)
        assert m.is_error is False
        assert m.error_category is None
        assert m.error_code is None

    def test_error_fields(self):
        m = CallMetrics(
            server="s", tool="t", original_chars=0, compressed_chars=0,
            is_error=True, error_category=ErrorCategory.PROTOCOL, error_code=-32601,
        )
        assert m.is_error is True
        assert m.error_category == ErrorCategory.PROTOCOL
        assert m.error_code == -32601


# ── TokenTracker.record_error ────────────────────────────────────────────


class TestTokenTrackerRecordError:
    def test_record_error_increments_total(self):
        tracker = TokenTracker()
        tracker.record_error(CallMetrics(
            server="s", tool="t", original_chars=0, compressed_chars=0,
            is_error=True, error_category=ErrorCategory.TRANSPORT,
        ))
        assert tracker._total_errors == 1

    def test_record_error_by_category(self):
        tracker = TokenTracker()
        tracker.record_error(CallMetrics(
            server="s", tool="t", original_chars=0, compressed_chars=0,
            is_error=True, error_category=ErrorCategory.TRANSPORT,
        ))
        tracker.record_error(CallMetrics(
            server="s", tool="t", original_chars=0, compressed_chars=0,
            is_error=True, error_category=ErrorCategory.TRANSPORT,
        ))
        tracker.record_error(CallMetrics(
            server="s", tool="t", original_chars=0, compressed_chars=0,
            is_error=True, error_category=ErrorCategory.PROTOCOL,
        ))
        assert tracker._errors_by_category["transport"] == 2
        assert tracker._errors_by_category["protocol"] == 1

    def test_record_error_by_server(self):
        tracker = TokenTracker()
        tracker.record_error(CallMetrics(
            server="srv1", tool="t", original_chars=0, compressed_chars=0,
            is_error=True, error_category=ErrorCategory.TIMEOUT,
        ))
        tracker.record_error(CallMetrics(
            server="srv2", tool="t", original_chars=0, compressed_chars=0,
            is_error=True, error_category=ErrorCategory.TIMEOUT,
        ))
        assert tracker._errors_by_server["srv1"] == 1
        assert tracker._errors_by_server["srv2"] == 1

    def test_record_error_none_category(self):
        tracker = TokenTracker()
        tracker.record_error(CallMetrics(
            server="s", tool="t", original_chars=0, compressed_chars=0,
            is_error=True,
        ))
        assert tracker._total_errors == 1
        assert len(tracker._errors_by_category) == 0


# ── get_summary error fields ─────────────────────────────────────────────


class TestSummaryErrorFields:
    def test_no_errors(self):
        tracker = TokenTracker()
        s = tracker.get_summary()
        assert s["total_errors"] == 0
        assert s["errors_by_category"] == {}
        assert s["error_rate"] == 0.0

    def test_error_rate_calculation(self):
        tracker = TokenTracker()
        # 3 successful calls
        for _ in range(3):
            tracker.record(CallMetrics(server="s", tool="t", original_chars=100, compressed_chars=50))
        # 1 error
        tracker.record_error(CallMetrics(
            server="s", tool="t", original_chars=0, compressed_chars=0,
            is_error=True, error_category=ErrorCategory.TRANSPORT,
        ))
        s = tracker.get_summary()
        assert s["total_calls"] == 3
        assert s["total_errors"] == 1
        # error_rate = 1 / (3 + 1) * 100 = 25.0%
        assert s["error_rate"] == 25.0

    def test_all_errors(self):
        tracker = TokenTracker()
        for _ in range(5):
            tracker.record_error(CallMetrics(
                server="s", tool="t", original_chars=0, compressed_chars=0,
                is_error=True, error_category=ErrorCategory.TIMEOUT,
            ))
        s = tracker.get_summary()
        assert s["error_rate"] == 100.0
        assert s["errors_by_category"] == {"timeout": 5}


# ── MetricsStore migration ──────────────────────────────────────────────


class TestMetricsStoreMigration:
    def test_fresh_db_has_error_columns(self, tmp_path):
        store = MetricsStore(tmp_path / "test.db")
        store.initialize()
        cols = {row[1] for row in store._db.execute("PRAGMA table_info(proxy_metrics)")}
        assert "is_error" in cols
        assert "error_category" in cols
        assert "error_code" in cols
        store.close()

    def test_existing_db_migrated(self, tmp_path):
        """Pre-existing DB without error columns gets migrated."""
        import sqlite3
        db_path = tmp_path / "old.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE proxy_metrics ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "server TEXT NOT NULL, tool TEXT NOT NULL, "
            "original_chars INTEGER NOT NULL, compressed_chars INTEGER NOT NULL, "
            "cleaned_chars INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL)"
        )
        conn.commit()
        conn.close()

        store = MetricsStore(db_path)
        store.initialize()
        cols = {row[1] for row in store._db.execute("PRAGMA table_info(proxy_metrics)")}
        assert "error_category" in cols
        store.close()

    def test_record_with_error_fields(self, tmp_path):
        store = MetricsStore(tmp_path / "test.db")
        store.initialize()
        store.record(CallMetrics(
            server="srv", tool="tool", original_chars=0, compressed_chars=0,
            is_error=True, error_category=ErrorCategory.PROTOCOL, error_code=-32601,
        ))
        row = store._db.execute(
            "SELECT is_error, error_category, error_code FROM proxy_metrics"
        ).fetchone()
        assert row == (1, "protocol", -32601)
        store.close()

    def test_record_success_has_defaults(self, tmp_path):
        store = MetricsStore(tmp_path / "test.db")
        store.initialize()
        store.record(CallMetrics(
            server="srv", tool="tool", original_chars=100, compressed_chars=50,
        ))
        row = store._db.execute(
            "SELECT is_error, error_category, error_code FROM proxy_metrics"
        ).fetchone()
        assert row == (0, None, None)
        store.close()


# ── ProxyManager integration ─────────────────────────────────────────────


def _text_content(text: str):
    return SimpleNamespace(type="text", text=text)


def _make_result(text: str, is_error: bool = False):
    return SimpleNamespace(content=[_text_content(text)], isError=is_error)


def _make_manager(max_retries: int = 0) -> ProxyManager:
    server_cfg = UpstreamServerConfig(
        prefix="test", compression=CompressionStrategy.NONE,
        max_result_chars=50000, max_retries=max_retries,
        reconnect_delay_seconds=0.0,
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


class TestManagerErrorRecording:
    async def test_programming_error_records_metric(self):
        mgr = _make_manager()
        mgr._connections["srv"].session.call_tool.side_effect = TypeError("bad")
        with pytest.raises(TypeError):
            await mgr.call_tool("srv", "tool", {})
        s = mgr.tracker.get_summary()
        assert s["total_errors"] == 1
        assert s["errors_by_category"]["programming"] == 1

    async def test_protocol_error_records_metric(self):
        mgr = _make_manager()
        exc = Exception("bad params")
        exc.error = SimpleNamespace(code=-32602)
        mgr._connections["srv"].session.call_tool.side_effect = exc
        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock):
            with pytest.raises(Exception):
                await mgr.call_tool("srv", "tool", {})
        s = mgr.tracker.get_summary()
        assert s["errors_by_category"]["protocol"] == 1

    async def test_transport_error_records_metric(self):
        mgr = _make_manager(max_retries=0)
        mgr._connections["srv"].session.call_tool.side_effect = ConnectionError("down")
        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock):
            with pytest.raises(ConnectionError):
                await mgr.call_tool("srv", "tool", {})
        s = mgr.tracker.get_summary()
        assert s["errors_by_category"]["transport"] == 1

    async def test_timeout_error_records_timeout_category(self):
        mgr = _make_manager(max_retries=0)
        mgr._connections["srv"].session.call_tool.side_effect = asyncio.TimeoutError()
        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock):
            with pytest.raises(asyncio.TimeoutError):
                await mgr.call_tool("srv", "tool", {})
        s = mgr.tracker.get_summary()
        assert s["errors_by_category"]["timeout"] == 1

    async def test_upstream_error_records_metric(self):
        mgr = _make_manager()
        mgr._connections["srv"].session.call_tool.return_value = _make_result(
            "Error: not found", is_error=True
        )
        result = await mgr.call_tool("srv", "tool", {})
        assert "not found" in result
        s = mgr.tracker.get_summary()
        assert s["errors_by_category"]["upstream_error"] == 1

    async def test_success_does_not_record_error(self):
        mgr = _make_manager()
        mgr._connections["srv"].session.call_tool.return_value = _make_result("ok")
        await mgr.call_tool("srv", "tool", {})
        s = mgr.tracker.get_summary()
        assert s["total_errors"] == 0
        assert s["total_calls"] == 1


# ── CircuitBreaker properties ────────────────────────────────────────────


class TestCircuitBreakerProperties:
    def test_initial_state(self):
        cb = CircuitBreaker(max_failures=3, reset_timeout=60.0)
        assert cb.state == "closed"
        assert cb.failure_count == 0
        assert cb.time_until_reset is None

    def test_state_after_failures(self):
        cb = CircuitBreaker(max_failures=2, reset_timeout=60.0)
        cb.record_failure()
        assert cb.state == "closed"
        assert cb.failure_count == 1
        cb.record_failure()
        assert cb.state == "open"
        assert cb.failure_count == 2
        assert cb.time_until_reset is not None
        assert cb.time_until_reset > 0

    def test_state_after_success(self):
        cb = CircuitBreaker(max_failures=2, reset_timeout=60.0)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == "open"
        # Simulate timeout elapsed
        cb._opened_at = time.monotonic() - 61.0
        assert cb.state == "half-open"
        cb.record_success()
        assert cb.state == "closed"
        assert cb.failure_count == 0

    def test_time_until_reset_decreases(self):
        cb = CircuitBreaker(max_failures=1, reset_timeout=10.0)
        cb.record_failure()
        t1 = cb.time_until_reset
        assert t1 is not None
        assert t1 <= 10.0
        cb._opened_at = time.monotonic() - 8.0
        t2 = cb.time_until_reset
        assert t2 is not None
        assert t2 <= 2.5
