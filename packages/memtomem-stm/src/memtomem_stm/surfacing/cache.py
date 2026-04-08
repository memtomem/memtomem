"""Surfacing result cache — avoid redundant LTM searches."""

from __future__ import annotations

import hashlib
import time
from typing import Any


class SurfacingCache:
    """In-memory TTL cache for surfacing results.

    Keyed by query hash. Avoids repeated LTM searches when the same
    tool is called with similar arguments in quick succession.
    """

    def __init__(self, ttl: float = 60.0, max_entries: int = 200) -> None:
        self._ttl = ttl
        self._max_entries = max_entries
        self._cache: dict[str, tuple[float, list[Any]]] = {}

    def get(self, query: str) -> list[Any] | None:
        key = self._hash(query)
        entry = self._cache.get(key)
        if entry is None:
            return None
        ts, results = entry
        if time.monotonic() - ts > self._ttl:
            del self._cache[key]
            return None
        return results

    def set(self, query: str, results: list[Any]) -> None:
        if len(self._cache) >= self._max_entries:
            self._evict()
        self._cache[self._hash(query)] = (time.monotonic(), results)

    def clear(self) -> None:
        self._cache.clear()

    def _evict(self) -> None:
        now = time.monotonic()
        expired = [k for k, (ts, _) in self._cache.items() if now - ts > self._ttl]
        for k in expired:
            del self._cache[k]
        # If still over limit, remove oldest
        while len(self._cache) >= self._max_entries:
            oldest = min(self._cache, key=lambda k: self._cache[k][0])
            del self._cache[oldest]

    @staticmethod
    def _hash(query: str) -> str:
        return hashlib.md5(query.encode()).hexdigest()
