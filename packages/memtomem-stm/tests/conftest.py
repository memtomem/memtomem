"""Shared fixtures for memtomem-stm tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from memtomem_stm.proxy.cache import ProxyCache
from memtomem_stm.proxy.metrics import TokenTracker
from memtomem_stm.surfacing.config import SurfacingConfig
from memtomem_stm.surfacing.feedback_store import FeedbackStore


@pytest.fixture
def surfacing_config() -> SurfacingConfig:
    """SurfacingConfig with short timeouts, no webhooks."""
    return SurfacingConfig(
        enabled=True,
        timeout_seconds=1.0,
        fire_webhook=False,
        feedback_enabled=True,
        auto_tune_enabled=True,
        cache_ttl_seconds=5.0,
        cooldown_seconds=1.0,
    )


@pytest.fixture
def feedback_store(tmp_path: Path) -> FeedbackStore:
    db = tmp_path / "test_feedback.db"
    store = FeedbackStore(db)
    store.initialize()
    yield store
    store.close()


@pytest.fixture
def proxy_cache(tmp_path: Path) -> ProxyCache:
    db = tmp_path / "test_cache.db"
    cache = ProxyCache(db, max_entries=100)
    cache.initialize()
    yield cache
    cache.close()


@pytest.fixture
def token_tracker() -> TokenTracker:
    return TokenTracker(metrics_store=None)
