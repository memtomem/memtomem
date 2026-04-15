"""Tests that MCP tool numeric-range validation errors echo the actual input.

Contract tested: when a tool rejects a numeric value as out of range, the
returned "Error: ..." string includes the offending value so the caller can
see what they actually passed. Closes #47.

Each test hits the validation path directly (no MCP ctx / app needed —
the bad-value checks run before any ctx access)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


class TestSearchValidation:
    async def test_top_k_out_of_range_includes_value(self):
        from memtomem.server.tools.search import mem_search

        result = await mem_search(query="hello", top_k=0)
        assert "got 0" in result
        assert "top_k must be between 1 and 100" in result

    async def test_query_too_long_includes_length(self):
        from memtomem.server.tools.search import mem_search

        long_query = "x" * 10_001
        result = await mem_search(query=long_query)
        assert "got 10001" in result
        assert "query too long" in result


class TestAskValidation:
    async def test_top_k_out_of_range_includes_value(self):
        from memtomem.server.tools.ask import mem_ask

        result = await mem_ask(question="hi", top_k=99)
        assert "got 99" in result
        assert "top_k must be between 1 and 20" in result


class TestConsolidateValidation:
    async def test_max_groups_out_of_range_includes_value(self):
        from memtomem.server.tools.consolidation import mem_consolidate

        result = await mem_consolidate(max_groups=99)
        assert "got 99" in result
        assert "max_groups must be between 1 and 50" in result

    async def test_min_group_size_too_small_includes_value(self):
        from memtomem.server.tools.consolidation import mem_consolidate

        result = await mem_consolidate(min_group_size=1)
        assert "got 1" in result
        assert "min_group_size must be at least 2" in result


class TestDedupDecayValidation:
    async def test_threshold_out_of_range_includes_value(self):
        from memtomem.server.tools.dedup_decay import mem_dedup_scan

        result = await mem_dedup_scan(threshold=1.5)
        assert "got 1.5" in result
        assert "threshold must be between 0 and 1" in result

    async def test_max_age_days_non_positive_includes_value(self):
        from memtomem.server.tools.dedup_decay import mem_decay_scan

        result = await mem_decay_scan(max_age_days=0)
        assert "got 0" in result
        assert "max_age_days must be positive" in result


class TestScratchValidation:
    async def test_ttl_minutes_non_positive_includes_value(self):
        from memtomem.server.tools.scratch import mem_scratch_set

        result = await mem_scratch_set(key="k", value="v", ttl_minutes=-5)
        assert "got -5" in result
        assert "ttl_minutes must be a positive number" in result


class TestRecallValidation:
    async def test_limit_out_of_range_includes_value(self):
        from memtomem.server.tools.recall import mem_recall

        result = await mem_recall(limit=9999)
        assert "got 9999" in result
        assert "limit must be between 1 and 500" in result


class TestMemoryCrudBatchValidation:
    async def test_batch_too_large_includes_length(self):
        from memtomem.server.tools.memory_crud import mem_batch_add

        entries = [{"key": str(i), "value": "x"} for i in range(501)]
        result = await mem_batch_add(entries=entries)
        assert "got 501" in result
        assert "batch too large" in result
