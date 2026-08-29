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
        assert history[0]["run_id"] is None
        assert history[0]["observation"] == {}
        assert history[0]["result_snapshot"] == []

    @pytest.mark.asyncio
    async def test_save_search_observation_round_trip(self, storage):
        run_id = "e38ab6c7-4db4-4d68-8dca-93c1da2dcfe6"
        observation = {"origin": "mcp", "profile_id": "abc123", "cache_hit": False}
        snapshot = [{"chunk_id": "id1", "rank": 1, "source_name": "note.md"}]

        saved = await storage.save_search_observation(
            "quality query",
            [0.1, 0.2],
            ["id1"],
            [0.9],
            run_id=run_id,
            observation=observation,
            result_snapshot=snapshot,
        )
        history = await storage.get_query_history(limit=1)

        assert saved == run_id
        assert history[0]["run_id"] == run_id
        assert history[0]["observation"] == observation
        assert history[0]["result_snapshot"] == snapshot

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
        # Deterministic newest-first order even when second-precision
        # ``created_at`` values collide.
        assert [row["query_text"] for row in history] == ["query3", "query2"]

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


class TestObservationCreatedAtNormalization:
    """``save_search_observation`` takes ``created_at`` from the caller, and
    the value becomes a lexically-compared row — so it needs the same UTC
    treatment as a bound, at the column's second precision (#2203)."""

    @staticmethod
    async def _save(storage, created_at):
        return await storage.save_search_observation(
            "stamped query",
            [],
            [],
            [],
            run_id="9f1d0c3e-1111-4444-8888-abcdefabcdef",
            observation={},
            result_snapshot=[],
            created_at=created_at,
        )

    @pytest.mark.asyncio
    async def test_an_offset_created_at_is_stored_as_utc_seconds(self, storage):
        await self._save(storage, "2026-01-01T00:00:00+09:00")
        row = storage._get_db().execute("SELECT created_at FROM query_history").fetchone()
        assert row[0] == "2025-12-31T15:00:00+00:00"

    @pytest.mark.asyncio
    async def test_a_malformed_created_at_is_refused_by_name(self, storage):
        with pytest.raises(ValueError, match="created_at must be an ISO-8601 timestamp"):
            await self._save(storage, "yesterday")

    @pytest.mark.asyncio
    async def test_an_empty_created_at_is_refused_not_restamped(self, storage):
        """An explicit empty string is a malformed argument, not an omitted
        one — truthiness would silently stamp ``now`` and lose the chronology
        the argument exists to record."""
        with pytest.raises(ValueError, match="created_at must be an ISO-8601 timestamp"):
            await self._save(storage, "")
