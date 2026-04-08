"""Tests for SurfacingCache — in-memory TTL cache for LTM search results."""

from __future__ import annotations

import time

from memtomem_stm.surfacing.cache import SurfacingCache


class TestSurfacingCacheBasic:
    def test_set_then_get(self):
        cache = SurfacingCache(ttl=60.0)
        cache.set("query1", ["result1", "result2"])
        assert cache.get("query1") == ["result1", "result2"]

    def test_cache_miss(self):
        cache = SurfacingCache(ttl=60.0)
        assert cache.get("nonexistent") is None

    def test_empty_list_cached(self):
        """Empty list should be cached (represents 'no results found')."""
        cache = SurfacingCache(ttl=60.0)
        cache.set("query", [])
        result = cache.get("query")
        assert result is not None
        assert result == []


class TestSurfacingCacheTTL:
    def test_expired_entry_returns_none(self):
        cache = SurfacingCache(ttl=0.01)
        cache.set("query", ["result"])
        time.sleep(0.02)
        assert cache.get("query") is None


class TestSurfacingCacheEviction:
    def test_max_entries_eviction(self):
        cache = SurfacingCache(ttl=60.0, max_entries=3)
        for i in range(5):
            cache.set(f"query_{i}", [f"result_{i}"])
        # Should have evicted oldest entries
        assert len(cache._cache) <= 3

    def test_expired_entries_evicted_first(self):
        cache = SurfacingCache(ttl=0.01, max_entries=3)
        cache.set("old1", ["r1"])
        cache.set("old2", ["r2"])
        time.sleep(0.02)
        # These should evict expired entries
        cache.set("new1", ["r3"])
        cache.set("new2", ["r4"])
        assert cache.get("new1") == ["r3"]


class TestSurfacingCacheClear:
    def test_clear_removes_all(self):
        cache = SurfacingCache(ttl=60.0)
        cache.set("q1", ["r1"])
        cache.set("q2", ["r2"])
        cache.clear()
        assert cache.get("q1") is None
        assert cache.get("q2") is None


class TestSurfacingCacheHashDeterminism:
    def test_same_query_same_hash(self):
        assert SurfacingCache._hash("hello") == SurfacingCache._hash("hello")

    def test_different_query_different_hash(self):
        assert SurfacingCache._hash("hello") != SurfacingCache._hash("world")
