"""Tests for embedding/retry.py — _parse_retry_after and with_retry."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from memtomem.embedding.retry import _parse_retry_after, with_retry


# ── _parse_retry_after ────────────────────────────────────────────────


class TestParseRetryAfter:
    def test_numeric_string(self):
        assert _parse_retry_after("5") == 5.0

    def test_float_string(self):
        assert _parse_retry_after("2.5") == 2.5

    def test_none_returns_none(self):
        assert _parse_retry_after(None) is None

    def test_empty_string_returns_none(self):
        assert _parse_retry_after("") is None

    def test_non_numeric_non_date_returns_none(self):
        assert _parse_retry_after("not-a-number") is None

    def test_valid_http_date(self):
        from datetime import datetime, timezone
        from email.utils import format_datetime

        # A date 10 seconds in the future
        future = datetime.now(timezone.utc).replace(microsecond=0)
        from datetime import timedelta

        future = future + timedelta(seconds=10)
        result = _parse_retry_after(format_datetime(future))
        assert result is not None
        assert 0 < result <= 11  # allow 1s clock drift


# ── with_retry — validation ───────────────────────────────────────────


class TestWithRetryValidation:
    def test_max_attempts_zero_raises(self):
        with pytest.raises(ValueError, match="max_attempts"):
            with_retry(max_attempts=0)

    def test_negative_base_delay_raises(self):
        with pytest.raises(ValueError, match="base_delay"):
            with_retry(base_delay=-1)


# ── with_retry — happy path ──────────────────────────────────────────


class TestWithRetryHappyPath:
    @pytest.mark.asyncio
    async def test_succeeds_on_first_call(self):
        call_count = 0

        @with_retry(max_attempts=3)
        async def fn():
            nonlocal call_count
            call_count += 1
            return "ok"

        result = await fn()
        assert result == "ok"
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_retry_then_succeed(self):
        call_count = 0

        @with_retry(max_attempts=3, base_delay=0.0)
        async def fn():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise OSError("transient")
            return "recovered"

        with patch("memtomem.embedding.retry.asyncio.sleep", new_callable=AsyncMock):
            result = await fn()
        assert result == "recovered"
        assert call_count == 2


# ── with_retry — failure cases ───────────────────────────────────────


class TestWithRetryFailure:
    @pytest.mark.asyncio
    async def test_max_attempts_exceeded(self):
        @with_retry(max_attempts=2, base_delay=0.0)
        async def fn():
            raise OSError("always fails")

        with patch("memtomem.embedding.retry.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(OSError, match="always fails"):
                await fn()

    @pytest.mark.asyncio
    async def test_non_retryable_exception_raises_immediately(self):
        call_count = 0

        @with_retry(max_attempts=3, retryable_exceptions=(OSError,))
        async def fn():
            nonlocal call_count
            call_count += 1
            raise ValueError("not retryable")

        with pytest.raises(ValueError, match="not retryable"):
            await fn()
        assert call_count == 1


# ── with_retry — backoff behaviour ───────────────────────────────────


class TestWithRetryBackoff:
    @pytest.mark.asyncio
    async def test_exponential_delays(self):
        @with_retry(max_attempts=4, base_delay=1.0, max_delay=16.0)
        async def fn():
            raise OSError("fail")

        mock_sleep = AsyncMock()
        with patch("memtomem.embedding.retry.asyncio.sleep", mock_sleep):
            with pytest.raises(OSError):
                await fn()

        delays = [c.args[0] for c in mock_sleep.call_args_list]
        assert delays == [1.0, 2.0, 4.0]  # 3 sleeps for 4 attempts

    @pytest.mark.asyncio
    async def test_max_delay_cap(self):
        @with_retry(max_attempts=5, base_delay=4.0, max_delay=10.0)
        async def fn():
            raise OSError("fail")

        mock_sleep = AsyncMock()
        with patch("memtomem.embedding.retry.asyncio.sleep", mock_sleep):
            with pytest.raises(OSError):
                await fn()

        delays = [c.args[0] for c in mock_sleep.call_args_list]
        assert all(d <= 10.0 for d in delays)

    @pytest.mark.asyncio
    async def test_retry_after_attribute(self):
        call_count = 0

        @with_retry(max_attempts=2, base_delay=1.0)
        async def fn():
            nonlocal call_count
            call_count += 1
            exc = OSError("rate limited")
            exc.retry_after = 10  # type: ignore[attr-defined]
            raise exc

        mock_sleep = AsyncMock()
        with patch("memtomem.embedding.retry.asyncio.sleep", mock_sleep):
            with pytest.raises(OSError):
                await fn()

        # delay should be max(base_delay=1.0, retry_after=10) = 10
        assert mock_sleep.call_args_list[0].args[0] == 10.0
