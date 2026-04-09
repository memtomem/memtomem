"""Tests for STM config loading, metrics persistence, token tracking, and privacy detection."""

from __future__ import annotations

import json
import time


from memtomem_stm.proxy.config import ProxyConfig, ProxyConfigLoader
from memtomem_stm.proxy.metrics import CallMetrics, TokenTracker
from memtomem_stm.proxy.metrics_store import MetricsStore
from memtomem_stm.proxy.privacy import contains_sensitive_content


# ── ProxyConfigLoader hot-reload ─────────────────────────────────────────


class TestProxyConfigLoader:
    def test_load_from_file(self, tmp_path):
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({
            "enabled": True,
            "upstream_servers": {
                "gh": {"prefix": "gh", "command": "gh-server"}
            },
        }))
        loader = ProxyConfigLoader(cfg_file)
        config = loader.get()
        assert config.enabled is True
        assert "gh" in config.upstream_servers

    def test_hot_reload_on_change(self, tmp_path):
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({"enabled": False}))
        loader = ProxyConfigLoader(cfg_file)
        loader.seed(ProxyConfig.load_from_file(cfg_file))

        c1 = loader.get()
        assert c1.enabled is False

        # Modify file (force mtime change)
        time.sleep(0.05)
        cfg_file.write_text(json.dumps({"enabled": True}))

        c2 = loader.get()
        assert c2.enabled is True

    def test_missing_file_returns_default(self, tmp_path):
        loader = ProxyConfigLoader(tmp_path / "nonexistent.json")
        config = loader.get()
        assert config.enabled is False  # default
        assert config.upstream_servers == {}

    def test_seed_uses_cached(self, tmp_path):
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({"enabled": True}))
        loader = ProxyConfigLoader(cfg_file)
        seeded = ProxyConfig(enabled=True)
        loader.seed(seeded)
        assert loader.get() is seeded  # same object, not re-parsed


# ── MetricsStore persistence ─────────────────────────────────────────────


class TestMetricsStore:
    def test_record_and_query(self, tmp_path):
        store = MetricsStore(tmp_path / "metrics.db")
        store.initialize()
        store.record(CallMetrics(server="gh", tool="list", original_chars=1000, compressed_chars=500))
        store.record(CallMetrics(server="gh", tool="search", original_chars=2000, compressed_chars=800))

        history = store.get_history(limit=10)
        assert len(history) == 2
        assert history[0]["tool"] == "search"  # newest first
        store.close()

    def test_trim_excess(self, tmp_path):
        store = MetricsStore(tmp_path / "metrics.db", max_history=5)
        store.initialize()
        for i in range(10):
            store.record(CallMetrics(server="s", tool=f"t{i}", original_chars=100, compressed_chars=50))

        history = store.get_history(limit=100)
        assert len(history) == 5  # trimmed to max_history
        store.close()

    def test_empty_history(self, tmp_path):
        store = MetricsStore(tmp_path / "metrics.db")
        store.initialize()
        assert store.get_history() == []
        store.close()


# ── TokenTracker aggregation ─────────────────────────────────────────────


class TestTokenTracker:
    def test_basic_recording(self):
        tracker = TokenTracker()
        tracker.record(CallMetrics(server="gh", tool="list", original_chars=1000, compressed_chars=500))
        tracker.record(CallMetrics(server="gh", tool="search", original_chars=2000, compressed_chars=800))

        summary = tracker.get_summary()
        assert summary["total_calls"] == 2
        assert summary["total_original_chars"] == 3000
        assert summary["total_compressed_chars"] == 1300
        assert summary["total_savings_pct"] > 50

    def test_by_server_aggregation(self):
        tracker = TokenTracker()
        tracker.record(CallMetrics(server="gh", tool="t1", original_chars=100, compressed_chars=50))
        tracker.record(CallMetrics(server="fs", tool="t2", original_chars=200, compressed_chars=100))

        summary = tracker.get_summary()
        assert "gh" in summary["by_server"]
        assert "fs" in summary["by_server"]
        assert summary["by_server"]["gh"]["calls"] == 1

    def test_cache_counters(self):
        tracker = TokenTracker()
        tracker.record_cache_hit()
        tracker.record_cache_hit()
        tracker.record_cache_miss()

        summary = tracker.get_summary()
        assert summary["cache_hits"] == 2
        assert summary["cache_misses"] == 1

    def test_empty_tracker(self):
        tracker = TokenTracker()
        summary = tracker.get_summary()
        assert summary["total_calls"] == 0
        assert summary["total_savings_pct"] == 0.0

    def test_with_persistent_store(self, tmp_path):
        store = MetricsStore(tmp_path / "metrics.db")
        store.initialize()
        tracker = TokenTracker(metrics_store=store)
        tracker.record(CallMetrics(server="gh", tool="list", original_chars=100, compressed_chars=50))

        # Check both in-memory and persistent
        assert tracker.get_summary()["total_calls"] == 1
        assert len(store.get_history()) == 1
        store.close()


# ── Privacy detection ────────────────────────────────────────────────────


class TestPrivacyDetection:
    def test_api_key_detected(self):
        assert contains_sensitive_content("api_key=sk-1234567890abcdefghijklmn")

    def test_password_detected(self):
        assert contains_sensitive_content("password: hunter2")

    def test_email_detected(self):
        assert contains_sensitive_content("Contact: user@example.com for info")

    def test_github_token_detected(self):
        assert contains_sensitive_content("token: ghp_" + "a" * 36)

    def test_private_key_detected(self):
        assert contains_sensitive_content("-----BEGIN RSA PRIVATE KEY-----")

    def test_clean_content_not_flagged(self):
        assert not contains_sensitive_content("This is a normal markdown document about APIs.")

    def test_empty_string_not_flagged(self):
        assert not contains_sensitive_content("")
