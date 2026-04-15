"""Tests for analytics storage mixin methods."""

import logging

import pytest
from pathlib import Path
from memtomem.models import Chunk, ChunkMetadata

from helpers import make_chunk


def _make_chunk(components, content="test", tags=(), namespace="default"):
    dim = components.config.embedding.dimension
    return make_chunk(content=content, tags=tags, namespace=namespace, embedding=[0.0] * dim)


class TestHealthReport:
    @pytest.mark.asyncio
    async def test_empty_db(self, storage):
        report = await storage.get_health_report()
        assert report["total_chunks"] == 0
        assert report["access_coverage"]["pct"] == 0
        assert report["tag_coverage"]["pct"] == 0
        assert report["sessions"]["total"] == 0

    @pytest.mark.asyncio
    async def test_with_data(self, storage, components):
        chunk = _make_chunk(components, tags=("test",))
        await storage.upsert_chunks([chunk])
        # Increment access
        await storage.increment_access([chunk.id])

        report = await storage.get_health_report()
        assert report["total_chunks"] == 1
        assert report["access_coverage"]["accessed"] == 1
        assert report["access_coverage"]["pct"] == 100.0


class TestFrequentlyAccessed:
    @pytest.mark.asyncio
    async def test_empty(self, storage):
        result = await storage.get_frequently_accessed()
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_accessed_chunks(self, storage, components):
        chunk = _make_chunk(components)
        await storage.upsert_chunks([chunk])
        await storage.increment_access([chunk.id])
        await storage.increment_access([chunk.id])

        result = await storage.get_frequently_accessed(limit=5)
        assert len(result) == 1
        assert result[0]["total_access"] == 2

    @pytest.mark.asyncio
    async def test_namespace_filter(self, storage, components):
        c1 = _make_chunk(components, content="a", namespace="ns1")
        c2 = _make_chunk(components, content="b", namespace="ns2")
        await storage.upsert_chunks([c1, c2])
        await storage.increment_access([c1.id])
        await storage.increment_access([c2.id])

        result = await storage.get_frequently_accessed(namespace="ns1")
        assert len(result) == 1


class TestAgentSessions:
    @pytest.mark.asyncio
    async def test_empty(self, storage):
        result = await storage.get_agent_sessions()
        assert result == []

    @pytest.mark.asyncio
    async def test_with_sessions(self, storage):
        await storage.create_session("s1", "agent-a", "default")
        await storage.create_session("s2", "agent-a", "default")
        await storage.create_session("s3", "agent-b", "default")

        result = await storage.get_agent_sessions()
        assert len(result) == 2
        agent_a = next(r for r in result if r["agent_id"] == "agent-a")
        assert agent_a["session_count"] == 2


class TestKnowledgeGaps:
    @pytest.mark.asyncio
    async def test_empty(self, storage):
        result = await storage.get_knowledge_gaps()
        assert result == []

    @pytest.mark.asyncio
    async def test_queries_with_no_results(self, storage):
        await storage.save_query_history("missing topic", [], [], [])
        await storage.save_query_history("missing topic", [], [], [])
        await storage.save_query_history("found topic", [], ["id1"], [0.9])

        gaps = await storage.get_knowledge_gaps()
        assert len(gaps) == 1
        assert gaps[0]["query"] == "missing topic"
        assert gaps[0]["count"] == 2

    @pytest.mark.asyncio
    async def test_db_error_logs_debug(self, storage, caplog):
        # Drop the query_history table to force the query to fail — we want
        # to verify the failure is logged (debug) rather than silently swallowed.
        storage._get_db().execute("DROP TABLE IF EXISTS query_history")

        with caplog.at_level(logging.DEBUG, logger="memtomem.storage.mixins.analytics"):
            result = await storage.get_knowledge_gaps()

        assert result == []
        assert any(
            rec.levelno == logging.DEBUG and "get_knowledge_gaps query failed" in rec.message
            for rec in caplog.records
        ), "Expected DEBUG log when get_knowledge_gaps query fails (not fully silent)"


class TestMostConnected:
    @pytest.mark.asyncio
    async def test_empty(self, storage):
        result = await storage.get_most_connected()
        assert result == []

    @pytest.mark.asyncio
    async def test_with_relations(self, storage, components):
        c1 = _make_chunk(components, content="hub")
        c2 = _make_chunk(components, content="spoke1")
        c3 = _make_chunk(components, content="spoke2")
        await storage.upsert_chunks([c1, c2, c3])
        await storage.add_relation(c1.id, c2.id)
        await storage.add_relation(c1.id, c3.id)

        result = await storage.get_most_connected(limit=2)
        assert len(result) >= 1
        assert result[0]["link_count"] >= 2


class TestChunkFactors:
    @pytest.mark.asyncio
    async def test_returns_factors(self, storage, components):
        chunk = _make_chunk(components, tags=("a", "b"))
        await storage.upsert_chunks([chunk])
        await storage.increment_access([chunk.id])

        factors = await storage.get_chunk_factors()
        assert len(factors) == 1
        assert factors[0]["access_count"] == 1
        assert factors[0]["updated_at"] is not None


class TestConsolidationGroups:
    @pytest.mark.asyncio
    async def test_empty(self, storage):
        result = await storage.get_consolidation_groups()
        assert result == []

    @pytest.mark.asyncio
    async def test_groups_by_source(self, storage, components):
        # Create 4 chunks from same source
        for i in range(4):
            c = Chunk(
                content=f"content {i}",
                metadata=ChunkMetadata(source_file=Path("/tmp/big.md")),
                embedding=[0.0] * components.config.embedding.dimension,
            )
            await storage.upsert_chunks([c])

        result = await storage.get_consolidation_groups(min_size=3)
        assert len(result) == 1
        assert result[0]["chunk_count"] == 4


class TestScratchPromote:
    @pytest.mark.asyncio
    async def test_promote(self, storage):
        await storage.scratch_set("key1", "val1")
        promoted = await storage.scratch_promote("key1")
        assert promoted is True

        entry = await storage.scratch_get("key1")
        assert entry["promoted"] is True

    @pytest.mark.asyncio
    async def test_promote_nonexistent(self, storage):
        promoted = await storage.scratch_promote("nope")
        assert promoted is False
