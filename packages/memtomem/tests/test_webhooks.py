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


class TestValidateWebhookUrl:
    """Pin the URL safety checks in ``_validate_webhook_url`` (#1030).

    Assertions stay at the rejection-reason level (scheme vs IP) rather than
    pinning the exact message, so wording can change without churning tests.
    """

    def test_accepts_https_url(self):
        from memtomem.server.webhooks import _validate_webhook_url

        assert _validate_webhook_url("https://example.com/hook") is None

    def test_accepts_http_dns_host(self):
        from memtomem.server.webhooks import _validate_webhook_url

        # A public DNS name over http is allowed — only IP literals are checked.
        assert _validate_webhook_url("http://example.com/hook") is None

    def test_rejects_unsupported_scheme(self):
        from memtomem.server.webhooks import _validate_webhook_url

        err = _validate_webhook_url("file:///etc/passwd")
        assert err is not None and "scheme" in err

    def test_rejects_malformed_url(self):
        from memtomem.server.webhooks import _validate_webhook_url

        # An unclosed IPv6 bracket makes urlparse() raise ValueError; the helper
        # catches it and returns the malformed-URL reason (the try/except branch).
        err = _validate_webhook_url("http://[::1")
        assert err is not None and "malformed" in err

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1/hook",  # loopback
            "http://10.0.0.1/hook",  # private
            "http://[::1]/hook",  # loopback (IPv6)
        ],
    )
    def test_rejects_private_or_reserved_ip(self, url):
        from memtomem.server.webhooks import _validate_webhook_url

        err = _validate_webhook_url(url)
        assert err is not None and "private/reserved" in err

    def test_manager_disables_on_rejected_url(self):
        """A rejected URL flips the manager's effective config to disabled."""
        from memtomem.config import WebhookConfig
        from memtomem.server.webhooks import WebhookManager

        mgr = WebhookManager(WebhookConfig(enabled=True, url="http://127.0.0.1/hook"))
        assert mgr._config.enabled is False

    @pytest.mark.asyncio
    async def test_manager_does_not_fire_on_rejected_url(self):
        """Self-disabled manager is inert even for a configured event."""
        from memtomem.config import WebhookConfig
        from memtomem.server.webhooks import WebhookManager

        mgr = WebhookManager(
            WebhookConfig(enabled=True, url="http://10.0.0.1/hook", events=["add"])
        )
        await mgr.fire("add", {})
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
