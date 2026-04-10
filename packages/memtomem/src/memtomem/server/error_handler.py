"""Shared error-handling decorator for MCP tool functions."""

from __future__ import annotations

import functools
import logging

from memtomem.errors import (
    ChunkingError,
    ConfigError,
    EmbeddingError,
    IndexingError,
    PermanentError,
    RetryableError,
    StorageError,
)

logger = logging.getLogger(__name__)

# Exceptions whose message is clear enough to surface directly.
_KNOWN_EXCEPTIONS = (
    ValueError,
    StorageError,
    EmbeddingError,
    IndexingError,
    ChunkingError,
    ConfigError,
    KeyError,
    FileNotFoundError,
    TypeError,
    ConnectionError,
    TimeoutError,
    PermissionError,
)


def tool_handler(fn):
    """Wrap an async tool function with standardised error handling.

    Catches any ``Exception``, logs it, and returns ``"Error: …"`` so the
    MCP client always receives a well-formed string response.
    """

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs) -> str:
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:
            logger.error("%s failed: %s", fn.__name__, exc, exc_info=True)
            if isinstance(exc, RetryableError):
                return f"Error (retryable): {exc}"
            if isinstance(exc, PermanentError):
                return f"Error (permanent): {exc}"
            if isinstance(exc, _KNOWN_EXCEPTIONS):
                return f"Error: {exc}"
            return f"Error: internal error ({type(exc).__name__}: {exc})"

    return wrapper
