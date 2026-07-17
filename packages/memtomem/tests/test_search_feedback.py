"""Storage tests for explicit relevance feedback on search runs (#1801).

Feedback rows attach a closed-vocabulary judgment to one committed
``query_run_id`` and one chunk of that run's snapshot. The contract under
test: idempotent resubmission, explicit timestamp-audited replacement,
fail-safe rejection of unknown/mismatched IDs without partial writes, and
retention that can never orphan feedback.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from pathlib import Path

import pytest

from memtomem.config import StorageConfig
from memtomem.errors import FeedbackConflictError, StorageError
from memtomem.storage.mixins.history import _next_audit_timestamp
from memtomem.storage.sqlite_backend import SqliteBackend

RUN_A = "11111111-1111-4111-8111-111111111111"
RUN_B = "22222222-2222-4222-8222-222222222222"


async def _seed_run(storage, run_id: str, chunk_ids: list[str] | None = None) -> str:
    chunk_ids = chunk_ids if chunk_ids is not None else ["c1", "c2"]
    snapshot = [
        {"chunk_id": cid, "rank": i + 1, "score": 0.9 - i * 0.1, "source_name": "note.md"}
        for i, cid in enumerate(chunk_ids)
    ]
    return await storage.save_search_observation(
        "quality query",
        [0.1, 0.2],
        chunk_ids,
        [0.9] * len(chunk_ids),
        run_id=run_id,
        observation={"origin": "mcp", "cache_hit": False},
        result_snapshot=snapshot,
    )


def _feedback_rows(storage) -> list[tuple]:
    return (
        storage._get_db()
        .execute("SELECT run_id, chunk_id, judgment, created_at, updated_at FROM search_feedback")
        .fetchall()
    )


class TestSaveSearchFeedback:
    async def test_round_trip_linked_to_one_run(self, storage):
        await _seed_run(storage, RUN_A)
        saved = await storage.save_search_feedback(RUN_A, "c1", "relevant")

        assert saved["created"] is True and saved["replaced"] is False
        assert saved["created_at"] == saved["updated_at"]
        judgments = await storage.get_search_feedback(RUN_A)
        assert judgments == [
            {
                "chunk_id": "c1",
                "judgment": "relevant",
                "created_at": saved["created_at"],
                "updated_at": saved["updated_at"],
            }
        ]

    async def test_identical_resubmit_is_idempotent(self, storage):
        await _seed_run(storage, RUN_A)
        first = await storage.save_search_feedback(RUN_A, "c1", "relevant")
        second = await storage.save_search_feedback(RUN_A, "c1", "relevant")

        assert second["created"] is False and second["replaced"] is False
        assert second["created_at"] == first["created_at"]
        assert second["updated_at"] == first["updated_at"]
        assert len(_feedback_rows(storage)) == 1

    async def test_conflict_without_replace_leaves_row_unchanged(self, storage):
        await _seed_run(storage, RUN_A)
        first = await storage.save_search_feedback(RUN_A, "c1", "relevant")

        with pytest.raises(FeedbackConflictError, match="replace=true"):
            await storage.save_search_feedback(RUN_A, "c1", "not_relevant")
        assert _feedback_rows(storage) == [
            (RUN_A, "c1", "relevant", first["created_at"], first["updated_at"])
        ]

    async def test_replace_advances_updated_at_and_keeps_created_at(self, storage):
        """Replacement inside the creation second must still be audited:
        microsecond precision plus the strictly-after rule guarantee
        ``updated_at > created_at`` even for an immediate replace."""
        await _seed_run(storage, RUN_A)
        first = await storage.save_search_feedback(RUN_A, "c1", "relevant")
        replaced = await storage.save_search_feedback(RUN_A, "c1", "not_relevant", replace=True)

        assert replaced["replaced"] is True and replaced["created"] is False
        assert replaced["created_at"] == first["created_at"]
        assert replaced["updated_at"] > first["updated_at"]

        again = await storage.save_search_feedback(RUN_A, "c1", "relevant", replace=True)
        assert again["updated_at"] > replaced["updated_at"]
        assert len(_feedback_rows(storage)) == 1

    async def test_unknown_run_id_rejected(self, storage):
        with pytest.raises(KeyError, match="not found"):
            await storage.save_search_feedback(RUN_A, "c1", "relevant")
        assert _feedback_rows(storage) == []

    async def test_chunk_outside_snapshot_rejected(self, storage):
        await _seed_run(storage, RUN_A)
        with pytest.raises(ValueError, match="result snapshot"):
            await storage.save_search_feedback(RUN_A, "ghost", "relevant")
        assert _feedback_rows(storage) == []

    async def test_chunk_from_other_run_rejected(self, storage):
        """Snapshot scoping: a chunk id valid for run B is not judgeable
        against run A."""
        await _seed_run(storage, RUN_A, ["c1"])
        await _seed_run(storage, RUN_B, ["other"])
        with pytest.raises(ValueError, match="result snapshot"):
            await storage.save_search_feedback(RUN_A, "other", "relevant")

    async def test_unknown_judgment_rejected(self, storage):
        await _seed_run(storage, RUN_A)
        with pytest.raises(ValueError, match="judgment must be one of"):
            await storage.save_search_feedback(RUN_A, "c1", "maybe")
        assert _feedback_rows(storage) == []

    async def test_rejected_inside_transaction_block(self, storage):
        """transaction() takes no lock, so the feedback read-modify-write
        must refuse to run there rather than lose its serialization."""
        await _seed_run(storage, RUN_A)
        with pytest.raises(StorageError, match="transaction"):
            async with storage.transaction():
                await storage.save_search_feedback(RUN_A, "c1", "relevant")
        assert _feedback_rows(storage) == []

    def test_next_audit_timestamp_survives_clock_backstep(self):
        future = "2999-01-01T00:00:00.000000+00:00"
        assert _next_audit_timestamp(future) == "2999-01-01T00:00:00.000001+00:00"

    async def test_feedback_write_survives_empty_prune(self, storage):
        """A zero-row prune DELETE still opens an implicit transaction; if it
        were left uncommitted, the next BEGIN IMMEDIATE would fail with
        'cannot start a transaction within a transaction'."""
        await _seed_run(storage, RUN_A)
        storage._prune_old_history()  # nothing is old enough → 0 rows
        assert storage._get_db().in_transaction is False
        saved = await storage.save_search_feedback(RUN_A, "c1", "relevant")
        assert saved["created"] is True


class TestIntegrityClassification:
    """The IntegrityError branch is defense-in-depth — BEGIN IMMEDIATE makes
    a real constraint hit unreachable in-process — so exercise the
    classification helper directly against real rows."""

    @staticmethod
    def _classify(storage, exc_message, judgment):
        return storage._classify_feedback_integrity_error(
            storage._get_db(), sqlite3.IntegrityError(exc_message), RUN_A, "c1", judgment
        )

    async def test_fk_violation_maps_to_unknown_run(self, storage):
        with pytest.raises(KeyError, match="not found"):
            self._classify(storage, "FOREIGN KEY constraint failed", "relevant")

    async def test_unique_violation_same_judgment_is_idempotent(self, storage):
        await _seed_run(storage, RUN_A)
        first = await storage.save_search_feedback(RUN_A, "c1", "relevant")
        landed = self._classify(
            storage, "UNIQUE constraint failed: search_feedback.run_id", "relevant"
        )
        assert landed["created"] is False and landed["replaced"] is False
        assert landed["updated_at"] == first["updated_at"]

    async def test_unique_violation_different_judgment_conflicts(self, storage):
        await _seed_run(storage, RUN_A)
        await storage.save_search_feedback(RUN_A, "c1", "relevant")
        with pytest.raises(FeedbackConflictError, match="replace=true"):
            self._classify(
                storage, "UNIQUE constraint failed: search_feedback.run_id", "not_relevant"
            )

    async def test_unrecognized_integrity_error_stays_storage_error(self, storage):
        with pytest.raises(StorageError, match="feedback write failed"):
            self._classify(
                storage, "NOT NULL constraint failed: search_feedback.judgment", "relevant"
            )


class TestFeedbackReads:
    async def test_get_search_feedback_unknown_run(self, storage):
        with pytest.raises(KeyError, match="not found"):
            await storage.get_search_feedback(RUN_A)

    async def test_get_search_run_detail(self, storage):
        await _seed_run(storage, RUN_A, ["c1"])
        run = await storage.get_search_run(RUN_A)
        assert run["run_id"] == RUN_A
        assert run["query_text"] == "quality query"
        assert run["observation"]["origin"] == "mcp"
        assert run["result_snapshot"][0]["chunk_id"] == "c1"
        with pytest.raises(KeyError, match="not found"):
            await storage.get_search_run(RUN_B)

    async def test_get_search_runs_summaries(self, storage):
        await storage.save_query_history("legacy", [], [], [])  # no run_id → excluded
        await _seed_run(storage, RUN_A)
        await storage.save_search_feedback(RUN_A, "c1", "relevant")

        runs = await storage.get_search_runs()
        assert [r["run_id"] for r in runs] == [RUN_A]
        assert runs[0]["result_count"] == 2
        assert runs[0]["feedback_count"] == 1
        assert runs[0]["origin"] == "mcp"
        assert "result_snapshot" not in runs[0]

    async def test_get_search_runs_limit_validation(self, storage):
        for bad in (0, -1, 201):
            with pytest.raises(ValueError, match="limit"):
                await storage.get_search_runs(limit=bad)

    async def test_get_search_runs_since_validation_and_filter(self, storage):
        await _seed_run(storage, RUN_A)
        assert await storage.get_search_runs(since="2020-01-01T00:00:00+00:00")
        assert await storage.get_search_runs(since="2999-01-01T00:00:00+00:00") == []
        # naive timestamps are treated as UTC rather than rejected
        assert await storage.get_search_runs(since="2020-01-01T00:00:00")
        with pytest.raises(ValueError, match="ISO-8601"):
            await storage.get_search_runs(since="yesterday")


class TestRetention:
    async def test_prune_cascades_to_feedback(self, storage):
        """Pruning an observation can never leave orphan feedback (#1801
        acceptance): the FK cascade removes dependent rows in the same
        statement."""
        await _seed_run(storage, RUN_A)
        await _seed_run(storage, RUN_B, ["fresh"])
        await storage.save_search_feedback(RUN_A, "c1", "relevant")
        await storage.save_search_feedback(RUN_B, "fresh", "not_relevant")

        db = storage._get_db()
        db.execute(
            "UPDATE query_history SET created_at = '2020-01-01T00:00:00+00:00' WHERE run_id = ?",
            (RUN_A,),
        )
        db.commit()
        storage._prune_old_history()

        remaining_runs = {r[0] for r in db.execute("SELECT run_id FROM query_history")}
        assert RUN_A not in remaining_runs and RUN_B in remaining_runs
        assert [r[0] for r in _feedback_rows(storage)] == [RUN_B]
        orphans = db.execute(
            "SELECT COUNT(*) FROM search_feedback f WHERE NOT EXISTS "
            "(SELECT 1 FROM query_history h WHERE h.run_id = f.run_id)"
        ).fetchone()[0]
        assert orphans == 0

    async def test_reset_all_clears_feedback(self, storage):
        await _seed_run(storage, RUN_A)
        await storage.save_search_feedback(RUN_A, "c1", "relevant")
        deleted = await storage.reset_all()
        assert deleted.get("search_feedback") == 1
        assert _feedback_rows(storage) == []


class TestCrossConnection:
    """Two backends on one DB file — the mm-web + MCP-server sharing shape.

    Event-loop scheduling keeps each critical section uninterrupted, so
    these pin cross-connection *visibility and semantics* (WAL reads, FK
    cascade, conflict classification), not true lock contention — that is
    what BEGIN IMMEDIATE plus the IntegrityError classification defend.
    """

    @pytest.fixture
    async def two_backends(self, tmp_path):
        cfg = StorageConfig()
        cfg.sqlite_path = tmp_path / "shared.db"
        a = SqliteBackend(cfg, dimension=8)
        await a.initialize()
        b = SqliteBackend(cfg, dimension=8)
        await b.initialize()
        yield a, b
        await a.close()
        await b.close()

    async def test_identical_and_conflicting_submissions(self, two_backends):
        a, b = two_backends
        await _seed_run(a, RUN_A)

        created = await a.save_search_feedback(RUN_A, "c1", "relevant")
        assert created["created"] is True
        echo = await b.save_search_feedback(RUN_A, "c1", "relevant")
        assert echo["created"] is False and echo["updated_at"] == created["updated_at"]
        with pytest.raises(FeedbackConflictError):
            await b.save_search_feedback(RUN_A, "c1", "not_relevant")

    async def test_prune_then_insert_fails_safely(self, two_backends):
        a, b = two_backends
        await _seed_run(a, RUN_A)
        db = a._get_db()
        db.execute("UPDATE query_history SET created_at = '2020-01-01T00:00:00+00:00'")
        db.commit()
        a._prune_old_history()

        with pytest.raises(KeyError, match="not found"):
            await b.save_search_feedback(RUN_A, "c1", "relevant")
        assert _feedback_rows(b) == []


class TestConcurrentContention:
    """Real lock contention through BEGIN IMMEDIATE: two backends on one DB
    file, each driven from its own thread (the write connection is
    thread-bound), released together by a barrier. Whichever loses the lock
    race observes the winner's committed row — the outcome invariants below
    hold for either ordering.
    """

    @staticmethod
    def _submit_from_thread(db_path: Path, judgment: str, barrier, results: dict, key: str):
        async def run():
            cfg = StorageConfig()
            cfg.sqlite_path = db_path
            backend = SqliteBackend(cfg, dimension=8)
            await backend.initialize()
            try:
                barrier.wait(timeout=10)
                return await backend.save_search_feedback(RUN_A, "c1", judgment)
            finally:
                await backend.close()

        def target():
            try:
                results[key] = asyncio.run(run())
            except Exception as exc:  # collected for assertion, not swallowed
                results[key] = exc

        thread = threading.Thread(target=target)
        thread.start()
        return thread

    async def _race(self, tmp_path, judgments: tuple[str, str]) -> tuple[dict, SqliteBackend]:
        cfg = StorageConfig()
        cfg.sqlite_path = tmp_path / "contended.db"
        seed = SqliteBackend(cfg, dimension=8)
        await seed.initialize()
        await _seed_run(seed, RUN_A)

        barrier = threading.Barrier(2)
        results: dict = {}
        threads = [
            self._submit_from_thread(cfg.sqlite_path, judgment, barrier, results, key)
            for key, judgment in zip(("x", "y"), judgments)
        ]
        for thread in threads:
            thread.join(timeout=30)
            assert not thread.is_alive()
        return results, seed

    async def test_simultaneous_identical_submissions(self, tmp_path):
        results, seed = await self._race(tmp_path, ("relevant", "relevant"))
        try:
            for outcome in results.values():
                assert isinstance(outcome, dict), outcome
            assert sorted(o["created"] for o in results.values()) == [False, True]
            assert len(_feedback_rows(seed)) == 1
        finally:
            await seed.close()

    async def test_simultaneous_conflicting_submissions(self, tmp_path):
        results, seed = await self._race(tmp_path, ("relevant", "not_relevant"))
        try:
            outcomes = list(results.values())
            winners = [o for o in outcomes if isinstance(o, dict)]
            losers = [o for o in outcomes if isinstance(o, FeedbackConflictError)]
            assert len(winners) == 1 and len(losers) == 1, outcomes
            rows = _feedback_rows(seed)
            assert len(rows) == 1
            assert rows[0][2] == winners[0]["judgment"]
        finally:
            await seed.close()
