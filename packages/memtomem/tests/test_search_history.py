"""Tests for search history storage methods."""

import pytest


class TestSearchHistory:
    @pytest.mark.asyncio
    async def test_save_and_get(self, storage):
        await storage.save_query_history("test query", [], ["id1", "id2"], [0.9, 0.8])
        history = await storage.get_query_history(limit=10)
        assert len(history) == 1
        assert history[0]["query_text"] == "test query"
        assert len(history[0]["result_chunk_ids"]) == 2

    @pytest.mark.asyncio
    async def test_empty_history(self, storage):
        history = await storage.get_query_history()
        assert history == []

    @pytest.mark.asyncio
    async def test_multiple_queries(self, storage):
        await storage.save_query_history("query1", [], [], [])
        await storage.save_query_history("query2", [], [], [])
        await storage.save_query_history("query3", [], [], [])
        history = await storage.get_query_history(limit=2)
        assert len(history) == 2
        # Most recent first
        assert history[0]["query_text"] == "query3"

    @pytest.mark.asyncio
    async def test_suggest_prefix(self, storage):
        await storage.save_query_history("deployment strategy", [], [], [])
        await storage.save_query_history("deployment pipeline", [], [], [])
        await storage.save_query_history("testing framework", [], [], [])
        suggestions = await storage.suggest_queries("deploy")
        assert len(suggestions) == 2
        assert all("deploy" in s for s in suggestions)

    @pytest.mark.asyncio
    async def test_suggest_no_match(self, storage):
        await storage.save_query_history("hello world", [], [], [])
        suggestions = await storage.suggest_queries("xyz")
        assert suggestions == []


class TestImportanceScores:
    @pytest.mark.asyncio
    async def test_update_and_get(self, storage, components):
        from pathlib import Path
        from memtomem.models import Chunk, ChunkMetadata

        chunk = Chunk(
            content="test",
            metadata=ChunkMetadata(source_file=Path("/t.md")),
            embedding=[0.0] * components.config.embedding.dimension,
        )
        await storage.upsert_chunks([chunk])

        scores = {str(chunk.id): 0.75}
        updated = await storage.update_importance_scores(scores)
        assert updated == 1

        result = await storage.get_importance_scores([chunk.id])
        assert result[str(chunk.id)] == pytest.approx(0.75)

    @pytest.mark.asyncio
    async def test_empty_scores(self, storage):
        result = await storage.get_importance_scores([])
        assert result == {}
