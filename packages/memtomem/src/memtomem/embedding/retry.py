"""Exponential backoff retry for async functions."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from functools import wraps

logger = logging.getLogger(__name__)


class RateLimitError(Exception):
    """Raised on a transient HTTP status (429, 5xx) to trigger retry via
    the with_retry decorator.

    The optional retry_after attribute is honored by with_retry when set:
    the loop sleeps for max(exponential_delay, retry_after). ``message``
    lets a provider describe the actual condition (e.g. "Ollama returned
    HTTP 503") instead of the default rate-limit wording, so retry logs
    and the final wrapped error don't mislabel a server reload as
    rate limiting.
    """

    def __init__(self, retry_after: float | None = None, message: str | None = None) -> None:
        self.retry_after = retry_after
        super().__init__(message or f"Rate limited (retry_after={retry_after})")


def parse_retry_after(value: str | None) -> float | None:
    """Parse a Retry-After header value (seconds or RFC 7231 HTTP-date).

    Returns the delay in seconds, or None if unparseable.
    """
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(value)
        delta = (dt - datetime.now(timezone.utc)).total_seconds()
        return max(delta, 0.0)
    except Exception:
        return None


def with_retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 16.0,
    retryable_exceptions: tuple[type[BaseException], ...] = (OSError, TimeoutError),
):
    """Decorator for async functions with exponential backoff retry.

    Retries on retryable_exceptions up to max_attempts times.
    Delay doubles each attempt: base_delay, base_delay*2, base_delay*4, ...
    """
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")
    if base_delay < 0:
        raise ValueError(f"base_delay must be >= 0, got {base_delay}")
    if max_delay < 0:
        raise ValueError(f"max_delay must be >= 0, got {max_delay}")

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_attempts):
                try:
                    return await func(*args, **kwargs)
                except retryable_exceptions as exc:
                    last_exc = exc
                    if attempt < max_attempts - 1:
                        delay = min(base_delay * (2**attempt), max_delay)
                        # Honour Retry-After from rate-limit responses. The
                        # attribute may be numeric (seconds) or a string (a raw
                        # Retry-After header: seconds or an RFC 7231 HTTP-date);
                        # strings go through parse_retry_after, which returns
                        # None for unparseable values so we fall back to backoff.
                        ra = getattr(exc, "retry_after", None)
                        ra_delay: float | None = None
                        if isinstance(ra, (int, float)):
                            ra_delay = float(ra)
                        elif isinstance(ra, str):
                            ra_delay = parse_retry_after(ra)
                        if ra_delay is not None:
                            delay = max(delay, ra_delay)
                        logger.warning(
                            "Retry %d/%d for %s after %.1fs: %s",
                            attempt + 1,
                            max_attempts,
                            func.__name__,
                            delay,
                            exc,
                        )
                        await asyncio.sleep(delay)
            assert last_exc is not None  # max_attempts >= 1 guarantees the loop ran
            raise last_exc

        return wrapper

    return decorator
