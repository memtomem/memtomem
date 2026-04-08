"""Tests for ProxyCache — SQLite response cache."""

from __future__ import annotations

import time

from memtomem_stm.proxy.cache import ProxyCache, _make_key


class TestProxyCacheBasic:
    def test_set_then_get(self, proxy_cache: ProxyCache):
        proxy_cache.set("s", "t", {"a": 1}, "result", ttl_seconds=60.0)
        assert proxy_cache.get("s", "t", {"a": 1}) == "result"

    def test_cache_miss(self, proxy_cache: ProxyCache):
        assert proxy_cache.get("s", "t", {"a": 1}) is None

    def test_different_args_different_entries(self, proxy_cache: ProxyCache):
        proxy_cache.set("s", "t", {"a": 1}, "r1", ttl_seconds=60.0)
        proxy_cache.set("s", "t", {"a": 2}, "r2", ttl_seconds=60.0)
        assert proxy_cache.get("s", "t", {"a": 1}) == "r1"
        assert proxy_cache.get("s", "t", {"a": 2}) == "r2"

    def test_update_existing_entry(self, proxy_cache: ProxyCache):
        proxy_cache.set("s", "t", {"a": 1}, "old", ttl_seconds=60.0)
        proxy_cache.set("s", "t", {"a": 1}, "new", ttl_seconds=60.0)
        assert proxy_cache.get("s", "t", {"a": 1}) == "new"


class TestProxyCacheTTL:
    def test_expired_entry_returns_none(self, proxy_cache: ProxyCache):
        proxy_cache.set("s", "t", {"a": 1}, "result", ttl_seconds=0.001)
        time.sleep(0.01)
        assert proxy_cache.get("s", "t", {"a": 1}) is None

    def test_no_ttl_never_expires(self, proxy_cache: ProxyCache):
        proxy_cache.set("s", "t", {"a": 1}, "result", ttl_seconds=None)
        assert proxy_cache.get("s", "t", {"a": 1}) == "result"


class TestProxyCacheClear:
    def test_clear_all(self, proxy_cache: ProxyCache):
        proxy_cache.set("s1", "t1", {}, "r1", ttl_seconds=60.0)
        proxy_cache.set("s2", "t2", {}, "r2", ttl_seconds=60.0)
        removed = proxy_cache.clear()
        assert removed == 2
        assert proxy_cache.get("s1", "t1", {}) is None

    def test_clear_by_server(self, proxy_cache: ProxyCache):
        proxy_cache.set("s1", "t1", {}, "r1", ttl_seconds=60.0)
        proxy_cache.set("s2", "t2", {}, "r2", ttl_seconds=60.0)
        removed = proxy_cache.clear(server="s1")
        assert removed == 1
        assert proxy_cache.get("s2", "t2", {}) == "r2"

    def test_clear_by_server_and_tool(self, proxy_cache: ProxyCache):
        proxy_cache.set("s1", "t1", {}, "r1", ttl_seconds=60.0)
        proxy_cache.set("s1", "t2", {}, "r2", ttl_seconds=60.0)
        removed = proxy_cache.clear(server="s1", tool="t1")
        assert removed == 1
        assert proxy_cache.get("s1", "t2", {}) == "r2"


class TestProxyCacheEviction:
    def test_trim_evicts_oldest(self, tmp_path):
        cache = ProxyCache(tmp_path / "cache.db", max_entries=3)
        cache.initialize()
        try:
            for i in range(5):
                cache.set("s", "t", {"i": i}, f"r{i}", ttl_seconds=60.0)
            stats = cache.stats()
            assert stats["total_entries"] <= 3
        finally:
            cache.close()


class TestProxyCacheStats:
    def test_stats_counts(self, proxy_cache: ProxyCache):
        proxy_cache.set("s", "t", {"a": 1}, "r", ttl_seconds=60.0)
        stats = proxy_cache.stats()
        assert stats["total_entries"] == 1
        assert stats["expired_entries"] == 0


class TestMakeKey:
    def test_deterministic(self):
        k1 = _make_key("s", "t", {"a": 1, "b": 2})
        k2 = _make_key("s", "t", {"a": 1, "b": 2})
        assert k1 == k2

    def test_arg_order_independent(self):
        k1 = _make_key("s", "t", {"a": 1, "b": 2})
        k2 = _make_key("s", "t", {"b": 2, "a": 1})
        assert k1 == k2

    def test_different_args_different_keys(self):
        k1 = _make_key("s", "t", {"a": 1})
        k2 = _make_key("s", "t", {"a": 2})
        assert k1 != k2
