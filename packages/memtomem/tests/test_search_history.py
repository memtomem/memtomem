"""Tests for search history storage methods."""

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

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

    @pytest.mark.asyncio
    async def test_project_history_detail_suggestions_and_feedback_are_isolated(self, storage):
        project_a = Path("/workspace/project-a")
        project_b = Path("/workspace/project-b")
        run_a = "aaaaaaaa-1111-4111-8111-111111111111"
        run_b = "bbbbbbbb-2222-4222-8222-222222222222"
        for project, run_id, query, chunk_id in (
            (project_a, run_a, "deploy alpha", "a-chunk"),
            (project_b, run_b, "deploy beta", "b-chunk"),
        ):
            await storage.save_search_observation(
                query,
                [],
                [chunk_id],
                [1.0],
                run_id=run_id,
                observation={"filters": {"scope": None}},
                result_snapshot=[{"chunk_id": chunk_id, "rank": 1}],
                project_context_root=project,
            )

        history_a = await storage.get_query_history(project_context_root=project_a)
        assert [row["query_text"] for row in history_a] == ["deploy alpha"]
        assert await storage.suggest_queries("deploy", project_context_root=project_a) == [
            "deploy alpha"
        ]
        assert (await storage.get_search_run(run_a, project_context_root=project_a))[
            "run_id"
        ] == run_a
        with pytest.raises(KeyError, match="not found"):
            await storage.get_search_run(run_b, project_context_root=project_a)
        with pytest.raises(KeyError, match="not found"):
            await storage.save_search_feedback(
                run_b,
                "b-chunk",
                "relevant",
                project_context_root=project_a,
            )

    @pytest.mark.asyncio
    async def test_migrated_unscoped_rows_fail_closed(self, storage):
        await storage.save_query_history("legacy secret", [], [], [])
        storage._get_db().execute(
            "UPDATE query_history SET project_key = NULL, legacy_unscoped = 1"
        )
        storage._get_db().commit()

        assert await storage.get_query_history() == []
        assert await storage.suggest_queries("legacy") == []


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


_EXPIRED_RUN = "eeeeeeee-1111-4111-8111-111111111111"


def _history_row_at(storage, text: str, age: timedelta, run_id: str | None = None) -> None:
    stamp = (datetime.now(timezone.utc) - age).isoformat(timespec="seconds")
    storage._get_db().execute(
        "INSERT INTO query_history (query_text, query_embedding, result_chunk_ids, "
        "result_scores, project_key, legacy_unscoped, created_at, run_id, "
        "result_snapshot_json) VALUES (?, x'', '[]', '[]', 'user', 0, ?, ?, ?)",
        (text, stamp, run_id, '[{"chunk_id": "c1"}]' if run_id else "[]"),
    )
    storage._get_db().commit()


def _texts(storage) -> set[str]:
    return {r[0] for r in storage._get_db().execute("SELECT query_text FROM query_history")}


async def _save_legacy(storage, text: str) -> None:
    await storage.save_query_history(text, [], [], [])


async def _save_observation(storage, text: str, run_id: str = "") -> None:
    await storage.save_search_observation(
        text,
        [],
        [],
        [],
        run_id=run_id or "aaaaaaaa-0000-4000-8000-000000000001",
        observation={},
        result_snapshot=[],
    )


class TestRetentionOnEverySave:
    """History older than 90 days goes on the next save, in any process.

    The prune used to run on every 100th save counted per process, and the
    counter restarted at zero in each one: a per-session MCP server or a CLI
    run that recorded fewer than 100 searches never pruned at all (#2686).
    The ``storage`` fixture is such a fresh process-side backend.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("save", [_save_legacy, _save_observation], ids=["legacy", "observed"])
    async def test_first_save_of_a_fresh_backend_prunes_expired_rows(self, storage, save):
        _history_row_at(storage, "expired", timedelta(days=91))
        _history_row_at(storage, "retained", timedelta(days=89))

        await save(storage, "new query")

        assert _texts(storage) == {"retained", "new query"}
        assert storage._get_db().in_transaction is False

    @pytest.mark.asyncio
    async def test_a_failed_save_keeps_the_rows_its_prune_deleted(self, storage):
        """The prune shares the save's write, so a save whose INSERT fails
        must not still delete history — or its feedback — on the way out."""
        taken = "bbbbbbbb-0000-4000-8000-000000000002"
        await _save_observation(storage, "first", run_id=taken)
        _history_row_at(storage, "expired", timedelta(days=91), run_id=_EXPIRED_RUN)
        db = storage._get_db()
        db.execute(
            "INSERT INTO search_feedback (run_id, chunk_id, judgment, created_at, updated_at) "
            "VALUES (?, 'c1', 'relevant', '2020-01-01T00:00:00+00:00', "
            "'2020-01-01T00:00:00+00:00')",
            (_EXPIRED_RUN,),
        )
        db.commit()

        with pytest.raises(sqlite3.IntegrityError):
            # A reused run ID trips the unique index after the prune ran.
            await _save_observation(storage, "dup", run_id=taken)

        assert db.in_transaction is False
        assert _texts(storage) == {"expired", "first"}
        assert db.execute("SELECT run_id FROM search_feedback").fetchall() == [(_EXPIRED_RUN,)]
