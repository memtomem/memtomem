"""Tests for ProxyManager error paths — transport failure, protocol error, reconnect, timeout."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from memtomem_stm.proxy.config import (
    CompressionStrategy,
    ProxyConfig,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.manager import ProxyManager, UpstreamConnection
from memtomem_stm.proxy.metrics import TokenTracker


# ── Helpers ──────────────────────────────────────────────────────────────


def _text_content(text: str):
    """Create a mock MCP TextContent."""
    return SimpleNamespace(type="text", text=text)


def _make_result(text: str, is_error: bool = False):
    """Create a mock call_tool result."""
    return SimpleNamespace(content=[_text_content(text)], isError=is_error)


def _make_manager(
    max_retries: int = 3,
    reconnect_delay: float = 0.0,
    max_reconnect_delay: float = 0.0,
    compression: CompressionStrategy = CompressionStrategy.NONE,
    max_result_chars: int = 50000,
    tmp_path: Path | None = None,
) -> ProxyManager:
    """Create a ProxyManager with a mocked upstream connection."""
    server_cfg = UpstreamServerConfig(
        prefix="test",
        compression=compression,
        max_result_chars=max_result_chars,
        max_retries=max_retries,
        reconnect_delay_seconds=reconnect_delay,
        max_reconnect_delay_seconds=max_reconnect_delay,
    )
    config_path = (tmp_path / "proxy.json") if tmp_path else Path("/tmp/proxy.json")
    proxy_cfg = ProxyConfig(
        config_path=config_path,
        upstream_servers={"srv": server_cfg},
    )
    tracker = TokenTracker()
    mgr = ProxyManager(proxy_cfg, tracker)

    # Inject a mocked connection
    session = AsyncMock()
    conn = UpstreamConnection(
        name="srv",
        config=server_cfg,
        session=session,
        tools=[],
    )
    mgr._connections["srv"] = conn
    return mgr


def _get_session(mgr: ProxyManager) -> AsyncMock:
    return mgr._connections["srv"].session


# ── Transport failure: retry + reconnect ─────────────────────────────────


class TestTransportFailureRetry:
    """OSError, ConnectionError, TimeoutError, EOFError → retry with reconnect."""

    @pytest.mark.parametrize(
        "exc_type",
        [OSError, ConnectionError, asyncio.TimeoutError, EOFError],
        ids=["OSError", "ConnectionError", "TimeoutError", "EOFError"],
    )
    async def test_retryable_error_succeeds_on_second_attempt(self, exc_type):
        mgr = _make_manager(max_retries=3)
        session = _get_session(mgr)
        session.call_tool.side_effect = [exc_type("fail"), _make_result("ok")]

        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock) as mock_reconnect:
            result = await mgr.call_tool("srv", "tool", {})

        assert result == "ok"
        assert session.call_tool.call_count == 2
        mock_reconnect.assert_awaited_once_with("srv")

    async def test_retries_exhaust_then_raises(self):
        mgr = _make_manager(max_retries=2)
        session = _get_session(mgr)
        session.call_tool.side_effect = ConnectionError("down")

        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock):
            with pytest.raises(ConnectionError, match="down"):
                await mgr.call_tool("srv", "tool", {})

        # initial + 2 retries = 3 attempts
        assert session.call_tool.call_count == 3

    async def test_reconnect_called_on_each_retry(self):
        mgr = _make_manager(max_retries=2)
        session = _get_session(mgr)
        session.call_tool.side_effect = [
            OSError("fail1"),
            OSError("fail2"),
            _make_result("ok"),
        ]

        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock) as mock_reconnect:
            result = await mgr.call_tool("srv", "tool", {})

        assert result == "ok"
        # 2 failures → 2 reconnects during retry loop
        assert mock_reconnect.await_count == 2

    async def test_tracker_records_reconnects(self):
        mgr = _make_manager(max_retries=3)
        session = _get_session(mgr)
        session.call_tool.side_effect = [
            ConnectionError("fail1"),
            ConnectionError("fail2"),
            _make_result("ok"),
        ]

        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock):
            await mgr.call_tool("srv", "tool", {})

        summary = mgr.tracker.get_summary()
        assert summary["reconnects"] == 2

    async def test_exponential_backoff_delay(self):
        mgr = _make_manager(max_retries=3, reconnect_delay=1.0, max_reconnect_delay=10.0)
        session = _get_session(mgr)
        session.call_tool.side_effect = [
            OSError("1"),
            OSError("2"),
            OSError("3"),
            _make_result("ok"),
        ]

        sleep_delays: list[float] = []

        async def capture_sleep(seconds):
            sleep_delays.append(seconds)

        with (
            patch.object(mgr, "_reconnect_server", new_callable=AsyncMock),
            patch("memtomem_stm.proxy.manager.asyncio.sleep", side_effect=capture_sleep),
        ):
            await mgr.call_tool("srv", "tool", {})

        # delay doubles: 1.0 → 2.0 → 4.0
        assert sleep_delays == [1.0, 2.0, 4.0]

    async def test_backoff_capped_at_max(self):
        mgr = _make_manager(max_retries=3, reconnect_delay=5.0, max_reconnect_delay=8.0)
        session = _get_session(mgr)
        session.call_tool.side_effect = [
            OSError("1"),
            OSError("2"),
            OSError("3"),
            _make_result("ok"),
        ]

        sleep_delays: list[float] = []

        async def capture_sleep(seconds):
            sleep_delays.append(seconds)

        with (
            patch.object(mgr, "_reconnect_server", new_callable=AsyncMock),
            patch("memtomem_stm.proxy.manager.asyncio.sleep", side_effect=capture_sleep),
        ):
            await mgr.call_tool("srv", "tool", {})

        # 5.0 → min(10.0, 8.0) = 8.0 → min(16.0, 8.0) = 8.0
        assert sleep_delays == [5.0, 8.0, 8.0]

    async def test_post_exhaustion_reconnect_attempted(self):
        """After all retries fail, a final reconnect is attempted before raising."""
        mgr = _make_manager(max_retries=1)
        session = _get_session(mgr)
        session.call_tool.side_effect = OSError("persistent")

        reconnect_calls: list[str] = []
        original_reconnect = AsyncMock()

        async def track_reconnect(name):
            reconnect_calls.append(name)

        with patch.object(mgr, "_reconnect_server", side_effect=track_reconnect):
            with pytest.raises(OSError):
                await mgr.call_tool("srv", "tool", {})

        # 1 reconnect during retry + 1 post-exhaustion reconnect
        assert len(reconnect_calls) == 2


# ── Protocol error: no retry ────────────────────────────────────────────


class TestProtocolError:
    """JSON-RPC errors with _NO_RETRY_CODES raise immediately, no retry."""

    @pytest.mark.parametrize(
        "code",
        [-32600, -32601, -32602, -32603],
        ids=["INVALID_REQUEST", "METHOD_NOT_FOUND", "INVALID_PARAMS", "INTERNAL_ERROR"],
    )
    async def test_protocol_error_no_retry(self, code):
        mgr = _make_manager(max_retries=3)
        session = _get_session(mgr)

        exc = Exception("protocol error")
        exc.error = SimpleNamespace(code=code)
        session.call_tool.side_effect = exc

        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock) as mock_reconnect:
            with pytest.raises(Exception, match="protocol error"):
                await mgr.call_tool("srv", "tool", {})

        # Only 1 attempt — no retries
        assert session.call_tool.call_count == 1
        # But reconnect IS called to keep connection healthy
        mock_reconnect.assert_awaited_once_with("srv")

    async def test_protocol_error_reconnect_failure_still_raises_original(self):
        """If reconnect fails after protocol error, the original error propagates."""
        mgr = _make_manager(max_retries=3)
        session = _get_session(mgr)

        exc = Exception("bad params")
        exc.error = SimpleNamespace(code=-32602)
        session.call_tool.side_effect = exc

        with patch.object(
            mgr, "_reconnect_server", new_callable=AsyncMock, side_effect=OSError("reconnect fail")
        ):
            with pytest.raises(Exception, match="bad params"):
                await mgr.call_tool("srv", "tool", {})


# ── Non-retryable errors: immediate propagation ─────────────────────────


class TestNonRetryableErrors:
    """TypeError, AttributeError, ValueError etc. propagate immediately."""

    @pytest.mark.parametrize(
        "exc_type,msg",
        [
            (TypeError, "wrong type"),
            (AttributeError, "no attr"),
            (ValueError, "bad value"),
            (KeyError, "missing key"),
        ],
        ids=["TypeError", "AttributeError", "ValueError", "KeyError"],
    )
    async def test_programming_error_no_retry(self, exc_type, msg):
        mgr = _make_manager(max_retries=3)
        session = _get_session(mgr)
        session.call_tool.side_effect = exc_type(msg)

        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock) as mock_reconnect:
            with pytest.raises(exc_type):
                await mgr.call_tool("srv", "tool", {})

        assert session.call_tool.call_count == 1
        mock_reconnect.assert_not_awaited()


# ── Reconnect failure scenarios ──────────────────────────────────────────


class TestReconnectFailure:
    """When _reconnect_server itself fails during retry loop."""

    async def test_reconnect_failure_during_retry_raises(self):
        mgr = _make_manager(max_retries=2)
        session = _get_session(mgr)
        session.call_tool.side_effect = ConnectionError("transport down")

        with patch.object(
            mgr,
            "_reconnect_server",
            new_callable=AsyncMock,
            side_effect=OSError("cannot reconnect"),
        ):
            with pytest.raises(OSError, match="cannot reconnect"):
                await mgr.call_tool("srv", "tool", {})

        # Only 1 attempt — reconnect failed on first retry
        assert session.call_tool.call_count == 1

    async def test_reconnect_succeeds_then_fails(self):
        """First reconnect works, second fails."""
        mgr = _make_manager(max_retries=3)
        session = _get_session(mgr)
        session.call_tool.side_effect = ConnectionError("fail")

        reconnect_count = 0

        async def flaky_reconnect(name):
            nonlocal reconnect_count
            reconnect_count += 1
            if reconnect_count >= 2:
                raise OSError("reconnect died")

        with patch.object(mgr, "_reconnect_server", side_effect=flaky_reconnect):
            with pytest.raises(OSError, match="reconnect died"):
                await mgr.call_tool("srv", "tool", {})

        # 2 call_tool attempts: initial + 1 successful reconnect
        assert session.call_tool.call_count == 2


# ── Zero retries configuration ──────────────────────────────────────────


class TestZeroRetries:
    async def test_no_retries_raises_immediately(self):
        mgr = _make_manager(max_retries=0)
        session = _get_session(mgr)
        session.call_tool.side_effect = ConnectionError("down")

        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock) as mock_reconnect:
            with pytest.raises(ConnectionError):
                await mgr.call_tool("srv", "tool", {})

        assert session.call_tool.call_count == 1
        # Post-failure reconnect still attempted
        mock_reconnect.assert_awaited_once()

    async def test_no_retries_success(self):
        mgr = _make_manager(max_retries=0)
        session = _get_session(mgr)
        session.call_tool.return_value = _make_result("works")

        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock) as mock_reconnect:
            result = await mgr.call_tool("srv", "tool", {})

        assert result == "works"
        mock_reconnect.assert_not_awaited()


# ── Unknown server ───────────────────────────────────────────────────────


class TestUnknownServer:
    async def test_unknown_server_raises_key_error(self):
        mgr = _make_manager()
        with pytest.raises(KeyError, match="Unknown upstream server"):
            await mgr.call_tool("nonexistent", "tool", {})


# ── Error result from upstream (isError=True) ────────────────────────────


class TestErrorResult:
    async def test_error_result_returned_as_is(self):
        """When upstream returns isError=True, raw text is returned (no compression)."""
        mgr = _make_manager(compression=CompressionStrategy.TRUNCATE, max_result_chars=10)
        session = _get_session(mgr)
        long_error = "Error: " + "x" * 500
        session.call_tool.return_value = _make_result(long_error, is_error=True)

        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock):
            result = await mgr.call_tool("srv", "tool", {})

        # Error result passes through uncompressed
        assert result == long_error


# ── Empty/non-text response ──────────────────────────────────────────────


class TestEdgeResponses:
    async def test_empty_response(self):
        mgr = _make_manager()
        session = _get_session(mgr)
        session.call_tool.return_value = SimpleNamespace(content=[], isError=False)

        result = await mgr.call_tool("srv", "tool", {})
        assert result == "[empty response]"

    async def test_non_text_content_passthrough(self):
        mgr = _make_manager()
        session = _get_session(mgr)
        img = SimpleNamespace(type="image", data="base64data")
        session.call_tool.return_value = SimpleNamespace(content=[img], isError=False)

        result = await mgr.call_tool("srv", "tool", {})
        assert isinstance(result, list)
        assert result[0].type == "image"

    async def test_mixed_text_and_non_text(self):
        mgr = _make_manager()
        session = _get_session(mgr)
        text = _text_content("hello world")
        img = SimpleNamespace(type="image", data="png")
        session.call_tool.return_value = SimpleNamespace(
            content=[text, img], isError=False
        )

        result = await mgr.call_tool("srv", "tool", {})
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0].type == "text"
        assert result[1].type == "image"


# ── Surfacing failure: graceful degradation ──────────────────────────────


class TestSurfacingFailure:
    async def test_surfacing_exception_returns_compressed(self):
        """If surfacing engine raises, compressed text is returned unchanged."""
        mgr = _make_manager()
        session = _get_session(mgr)
        session.call_tool.return_value = _make_result("hello world")

        engine = AsyncMock()
        engine.surface.side_effect = RuntimeError("LTM down")
        mgr._surfacing_engine = engine

        result = await mgr.call_tool("srv", "tool", {})
        # Should get the text back, not an exception
        assert "hello world" in result


# ── Context query stripping ──────────────────────────────────────────────


class TestContextQueryStripping:
    async def test_context_query_stripped_from_upstream_args(self):
        mgr = _make_manager()
        session = _get_session(mgr)
        session.call_tool.return_value = _make_result("result")

        await mgr.call_tool("srv", "tool", {"q": "test", "_context_query": "search query"})

        # _context_query should NOT be forwarded to upstream
        call_args = session.call_tool.call_args
        forwarded_args = call_args[0][1]  # second positional arg
        assert "_context_query" not in forwarded_args
        assert forwarded_args == {"q": "test"}


# ── Cache interaction with errors ────────────────────────────────────────


class TestCacheWithErrors:
    async def test_cache_miss_then_transport_error(self):
        """Cache miss followed by transport error still raises."""
        mgr = _make_manager(max_retries=0)
        session = _get_session(mgr)
        session.call_tool.side_effect = ConnectionError("down")

        cache = MagicMock()
        cache.get.return_value = None  # cache miss
        mgr._cache = cache

        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock):
            with pytest.raises(ConnectionError):
                await mgr.call_tool("srv", "tool", {})

        cache.get.assert_called_once()
        mgr.tracker.get_summary()  # should record cache miss
        assert mgr.tracker.get_summary()["cache_misses"] == 1

    async def test_cache_hit_bypasses_upstream(self):
        """Cache hit skips upstream call entirely."""
        mgr = _make_manager()
        session = _get_session(mgr)
        session.call_tool.side_effect = AssertionError("should not be called")

        cache = MagicMock()
        cache.get.return_value = "cached result"
        mgr._cache = cache

        result = await mgr.call_tool("srv", "tool", {})
        assert result == "cached result"
        session.call_tool.assert_not_called()
        assert mgr.tracker.get_summary()["cache_hits"] == 1
