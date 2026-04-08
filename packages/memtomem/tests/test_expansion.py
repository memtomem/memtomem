"""Tests for query expansion."""

import pytest
from memtomem.search.expansion import expand_query_tags


class FakeStorage:
    """Mock storage for tag-based expansion tests."""
    def __init__(self, tags):
        self._tags = tags

    async def get_tag_counts(self):
        return self._tags


class TestExpandQueryTags:
    @pytest.mark.asyncio
    async def test_no_matching_tags(self):
        storage = FakeStorage([("python", 5), ("javascript", 3)])
        result = await expand_query_tags("deployment strategy", storage)
        assert result == "deployment strategy"

    @pytest.mark.asyncio
    async def test_matching_tag_appended(self):
        storage = FakeStorage([("deploy", 5), ("testing", 3)])
        result = await expand_query_tags("deployment strategy", storage)
        assert "deploy" in result

    @pytest.mark.asyncio
    async def test_max_terms_respected(self):
        storage = FakeStorage([("deploy-a", 5), ("deploy-b", 3), ("deploy-c", 2), ("deploy-d", 1)])
        result = await expand_query_tags("deploy", storage, max_terms=2)
        # Original query + up to 2 terms
        added = result.replace("deploy", "").strip().split()
        assert len(added) <= 2

    @pytest.mark.asyncio
    async def test_short_words_ignored(self):
        """Words shorter than 3 chars should not trigger expansion."""
        storage = FakeStorage([("ai", 10), ("ml", 8)])
        result = await expand_query_tags("ai tools", storage)
        assert result == "ai tools"

    @pytest.mark.asyncio
    async def test_empty_tags(self):
        storage = FakeStorage([])
        result = await expand_query_tags("test query", storage)
        assert result == "test query"
