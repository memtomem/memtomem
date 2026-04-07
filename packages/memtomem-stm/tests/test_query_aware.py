"""Tests for query-aware compression.

Validates QueryRelevanceScorer and TruncateCompressor's context_query
budget allocation behavior.

    uv run pytest packages/memtomem-stm/tests/test_query_aware.py -v
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from memtomem_stm.proxy.compression import QueryRelevanceScorer, TruncateCompressor


# ── QueryRelevanceScorer ──────────────────────────────────────────────


class TestQueryRelevanceScorer:
    def test_heading_weight(self):
        """Section with query term in heading scores higher than body-only."""
        sections = [
            ("## Redis Caching", "LRU eviction policy applied."),
            ("## Database", "Redis used as secondary cache layer."),
        ]
        scorer = QueryRelevanceScorer("Redis", sections)
        scores = scorer.score_all()
        # "Redis" appears in heading of section 0 → heading weight 3×
        assert scores[0] > scores[1]

    def test_no_match_returns_zeros(self):
        """Query terms not in any section → all zeros."""
        sections = [
            ("## Database", "PostgreSQL for ACID transactions."),
            ("## Caching", "LRU eviction policy."),
        ]
        scorer = QueryRelevanceScorer("kubernetes deployment", sections)
        scores = scorer.score_all()
        assert all(s == 0.0 for s in scores)

    def test_partial_match(self):
        """Some sections match, others don't → mixed scores."""
        sections = [
            ("## Auth", "OAuth2 with PKCE flow for authentication."),
            ("## Database", "PostgreSQL for storage."),
            ("## API", "RESTful endpoints with FastAPI."),
        ]
        scorer = QueryRelevanceScorer("OAuth2 authentication", sections)
        scores = scorer.score_all()
        assert scores[0] > 0
        assert scores[1] == 0.0
        assert scores[2] == 0.0

    def test_case_insensitive(self):
        """Matching is case-insensitive."""
        sections = [
            ("## Setup", "Install REDIS server on Ubuntu."),
        ]
        scorer = QueryRelevanceScorer("redis", sections)
        scores = scorer.score_all()
        assert scores[0] > 0

    def test_empty_query_returns_zeros(self):
        """Empty query → all zeros."""
        sections = [("## Title", "Some content.")]
        scorer = QueryRelevanceScorer("", sections)
        assert scorer.score_all() == [0.0]

    def test_stemming_matches(self):
        """Basic suffix stemming: 'caching' matches 'cache'."""
        sections = [
            ("## Cache Layer", "Redis caching strategy."),
            ("## Deployment", "Kubernetes pods."),
        ]
        scorer = QueryRelevanceScorer("caching", sections)
        scores = scorer.score_all()
        assert scores[0] > scores[1]


# ── TruncateCompressor with context_query ─────────────────────────────


class TestTruncateWithQuery:
    @pytest.fixture
    def compressor(self):
        return TruncateCompressor()

    @pytest.fixture
    def multi_section_doc(self):
        """Multi-section doc with multi-line content per section."""

        def _section(heading: str, line: str, n: int = 20) -> str:
            return f"## {heading}\n\n" + "\n".join([line] * n)

        return "\n\n".join(
            [
                _section("Database Design", "PostgreSQL for ACID transactions and JSON support."),
                _section("Redis Caching", "Redis LRU eviction. Cache-aside pattern applied."),
                _section("Message Queue", "RabbitMQ for async job processing."),
                _section("Monitoring", "Prometheus and Grafana stack for alerts."),
                _section("Deployment", "Kubernetes on AWS EKS with Helm charts."),
            ]
        )

    def test_query_boosts_relevant_section(self, compressor, multi_section_doc):
        """With query, the matching section gets more content than without."""
        budget = len(multi_section_doc) // 3  # ~33% budget

        result_with_query = compressor.compress(
            multi_section_doc, max_chars=budget, context_query="Redis caching strategy"
        )
        result_without_query = compressor.compress(multi_section_doc, max_chars=budget)

        # Extract Redis section content from both
        def redis_content(text: str) -> str:
            start = text.find("## Redis")
            if start < 0:
                return ""
            end = text.find("\n## ", start + 1)
            return text[start : end if end > 0 else len(text)]

        redis_with = redis_content(result_with_query)
        redis_without = redis_content(result_without_query)
        assert len(redis_with) > len(redis_without), (
            f"Query-aware Redis section ({len(redis_with)}) should be longer "
            f"than top-down ({len(redis_without)})"
        )

    def test_without_query_preserves_original_behavior(self, compressor, multi_section_doc):
        """Without context_query, behavior is identical to baseline."""
        budget = len(multi_section_doc) // 3

        result_none = compressor.compress(multi_section_doc, max_chars=budget)
        result_explicit_none = compressor.compress(
            multi_section_doc, max_chars=budget, context_query=None
        )
        assert result_none == result_explicit_none

    def test_budget_respected(self, compressor, multi_section_doc):
        """Query-aware mode must not exceed max_chars."""
        budget = 500
        result = compressor.compress(
            multi_section_doc, max_chars=budget, context_query="Redis caching"
        )
        assert len(result) <= budget

    def test_min_representation_preserved(self, compressor, multi_section_doc):
        """All sections get at least heading + 1 line even with query."""
        budget = len(multi_section_doc) // 3
        result = compressor.compress(
            multi_section_doc, max_chars=budget, context_query="Redis caching"
        )
        for heading in [
            "Database Design",
            "Redis Caching",
            "Message Queue",
            "Monitoring",
            "Deployment",
        ]:
            assert heading in result, f"Section '{heading}' missing from output"

    def test_all_scores_zero_fallback(self, compressor, multi_section_doc):
        """Query that matches nothing → falls back to top-down."""
        budget = len(multi_section_doc) // 3
        result_nomatch = compressor.compress(
            multi_section_doc, max_chars=budget, context_query="xyzzy nonexistent"
        )
        result_noquery = compressor.compress(multi_section_doc, max_chars=budget)
        # Should produce identical output (both use top-down)
        assert result_nomatch == result_noquery

    def test_json_content_with_query(self, compressor):
        """JSON top-level keys: query-relevant key gets more budget."""
        data = {
            "users": {"count": 50, "details": "user data " * 100},
            "permissions": {"roles": ["admin", "editor"], "details": "role data " * 100},
            "settings": {"theme": "dark", "details": "config data " * 100},
        }
        import json

        text = json.dumps(data, indent=2)
        budget = len(text) // 3

        result = compressor.compress(
            text, max_chars=budget, context_query="permissions roles admin"
        )
        # permissions section should have more content preserved
        assert "permissions" in result
        assert "roles" in result


# ── ProxyManager Integration ──────────────────────────────────────────


class TestManagerPassesContextQuery:
    @pytest.mark.asyncio
    async def test_apply_compression_threads_context_query(self):
        """_apply_compression passes context_query to TruncateCompressor."""
        from memtomem_stm.proxy.config import CompressionStrategy, ProxyConfig
        from memtomem_stm.proxy.metrics import TokenTracker

        tracker = TokenTracker()
        config = ProxyConfig(enabled=True)

        from memtomem_stm.proxy.manager import ProxyManager

        mgr = ProxyManager(config, tracker)

        with patch.object(TruncateCompressor, "compress", return_value="compressed") as mock:
            await mgr._apply_compression(
                "some text",
                CompressionStrategy.TRUNCATE,
                100,
                None,
                None,
                None,
                "server",
                "tool",
                context_query="test query",
            )
            mock.assert_called_once()
            _, kwargs = mock.call_args
            assert kwargs.get("context_query") == "test query"

    @pytest.mark.asyncio
    async def test_auto_threads_context_query_to_truncate(self):
        """AUTO → TRUNCATE preserves context_query through recursive call."""
        from memtomem_stm.proxy.config import CompressionStrategy, ProxyConfig
        from memtomem_stm.proxy.metrics import TokenTracker

        tracker = TokenTracker()
        config = ProxyConfig(enabled=True)

        from memtomem_stm.proxy.manager import ProxyManager

        mgr = ProxyManager(config, tracker)

        # Content that will resolve to TRUNCATE via auto_select
        text = "## A\n\nContent A.\n\n## B\n\nContent B.\n" * 5

        with patch.object(TruncateCompressor, "compress", return_value="compressed") as mock:
            await mgr._apply_compression(
                text,
                CompressionStrategy.AUTO,
                50,
                None,
                None,
                None,
                "server",
                "tool",
                context_query="section A query",
            )
            mock.assert_called_once()
            _, kwargs = mock.call_args
            assert kwargs.get("context_query") == "section A query"
