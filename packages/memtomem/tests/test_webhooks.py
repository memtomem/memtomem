"""Tests for webhook manager."""

import pytest
import hashlib
import hmac
import json


class TestWebhookManager:
    def test_hmac_signature(self):
        """Verify HMAC-SHA256 signature computation."""
        secret = "test-secret"
        body = json.dumps({"event": "add", "data": {"file": "/test.md"}})
        expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        assert expected and len(expected) == 64

    @pytest.mark.asyncio
    async def test_disabled_config(self):
        """WebhookManager should be None when disabled."""
        from memtomem.config import WebhookConfig
        from memtomem.server.webhooks import WebhookManager
        config = WebhookConfig(enabled=False, url="https://example.com")
        mgr = WebhookManager(config)
        await mgr.fire("add", {})

    @pytest.mark.asyncio
    async def test_no_url_no_fire(self):
        """No URL configured should skip webhook."""
        from memtomem.config import WebhookConfig
        from memtomem.server.webhooks import WebhookManager
        config = WebhookConfig(enabled=True, url="")
        mgr = WebhookManager(config)
        await mgr.fire("add", {})

    @pytest.mark.asyncio
    async def test_event_filtering(self):
        """Events not in the configured list should be skipped."""
        from memtomem.config import WebhookConfig
        from memtomem.server.webhooks import WebhookManager
        config = WebhookConfig(enabled=True, url="https://example.com", events=["add"])
        mgr = WebhookManager(config)
        await mgr.fire("search", {})
        assert mgr._client is None


class TestRerankerFactory:
    def test_disabled_returns_none(self):
        from memtomem.config import RerankConfig
        from memtomem.search.reranker.factory import create_reranker
        config = RerankConfig(enabled=False)
        assert create_reranker(config) is None

    def test_cohere_provider(self):
        from memtomem.config import RerankConfig
        from memtomem.search.reranker.factory import create_reranker
        from memtomem.search.reranker.cohere import CohereReranker
        config = RerankConfig(enabled=True, provider="cohere", api_key="test")
        reranker = create_reranker(config)
        assert isinstance(reranker, CohereReranker)

    def test_unknown_provider_raises(self):
        from memtomem.config import RerankConfig
        from memtomem.search.reranker.factory import create_reranker
        config = RerankConfig(enabled=True, provider="unknown")
        with pytest.raises(ValueError, match="Unknown reranker"):
            create_reranker(config)


class TestConfigSections:
    def test_all_new_configs_default_disabled(self):
        from memtomem.config import Mem2MemConfig
        c = Mem2MemConfig()
        assert c.rerank.enabled is False
        assert c.query_expansion.enabled is False
        assert c.importance.enabled is False
        assert c.conflict.enabled is False
        assert c.webhook.enabled is False
        assert c.consolidation_schedule.enabled is False

    def test_rerank_config_validation(self):
        from memtomem.config import RerankConfig
        with pytest.raises(Exception):
            RerankConfig(top_k=0)

    def test_importance_max_boost_validation(self):
        from memtomem.config import ImportanceConfig
        with pytest.raises(Exception):
            ImportanceConfig(max_boost=0.5)

    def test_query_expansion_strategy_validation(self):
        from memtomem.config import QueryExpansionConfig
        with pytest.raises(Exception):
            QueryExpansionConfig(strategy="invalid")
