"""Tests for cross-session surfacing dedup (persistent seen_memories)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest

from memtomem_stm.surfacing.config import SurfacingConfig
from memtomem_stm.surfacing.engine import SurfacingEngine
from memtomem_stm.surfacing.feedback import FeedbackTracker
from memtomem_stm.surfacing.feedback_store import FeedbackStore

# ── Helpers ──────────────────────────────────────────────────────────────

from unittest.mock import AsyncMock


@dataclass
class FakeChunkMeta:
    source_file: Path = Path("/notes/test.md")
    namespace: str = "default"


@dataclass
class FakeChunk:
    id: str = ""
    content: str = "some memory"
    metadata: FakeChunkMeta | None = None

    def __post_init__(self):
        if not self.id:
            self.id = str(uuid4())
        if self.metadata is None:
            self.metadata = FakeChunkMeta()


@dataclass
class FakeSearchResult:
    chunk: FakeChunk
    score: float
    rank: int = 1


def _make_config(**overrides) -> SurfacingConfig:
    defaults = {
        "enabled": True,
        "min_response_chars": 10,
        "timeout_seconds": 5.0,
        "min_score": 0.02,
        "max_results": 3,
        "cooldown_seconds": 0.0,
        "max_surfacings_per_minute": 1000,
        "auto_tune_enabled": False,
        "include_session_context": False,
        "fire_webhook": False,
        "cache_ttl_seconds": 60.0,
        "dedup_ttl_seconds": 604800.0,  # 7 days
    }
    defaults.update(overrides)
    return SurfacingConfig(**defaults)


def _make_search_pipeline(results=None):
    pipeline = AsyncMock()
    pipeline.search = AsyncMock(return_value=(results or [], {}))
    return pipeline


LONG_RESPONSE = "x" * 200
VALID_ARGS = {"path": "src/app.py", "_context_query": "Flask web framework"}


# ── FeedbackStore seen_memories table ────────────────────────────────────


class TestFeedbackStoreSeenMemories:
    def test_mark_and_get_seen_ids(self, tmp_path):
        store = FeedbackStore(tmp_path / "test.db")
        store.initialize()

        store.mark_surfaced(["mem-1", "mem-2"])
        seen = store.get_seen_ids(ttl_seconds=3600)
        assert seen == {"mem-1", "mem-2"}
        store.close()

    def test_get_seen_ids_empty(self, tmp_path):
        store = FeedbackStore(tmp_path / "test.db")
        store.initialize()

        seen = store.get_seen_ids(ttl_seconds=3600)
        assert seen == set()
        store.close()

    def test_mark_surfaced_updates_count(self, tmp_path):
        store = FeedbackStore(tmp_path / "test.db")
        store.initialize()

        store.mark_surfaced(["mem-1"])
        store.mark_surfaced(["mem-1"])

        row = store._db.execute(
            "SELECT seen_count FROM seen_memories WHERE memory_id = ?", ("mem-1",)
        ).fetchone()
        assert row[0] == 2
        store.close()

    def test_mark_surfaced_updates_last_seen(self, tmp_path):
        store = FeedbackStore(tmp_path / "test.db")
        store.initialize()

        store.mark_surfaced(["mem-1"])
        row1 = store._db.execute(
            "SELECT last_seen_at FROM seen_memories WHERE memory_id = ?", ("mem-1",)
        ).fetchone()

        time.sleep(0.01)
        store.mark_surfaced(["mem-1"])
        row2 = store._db.execute(
            "SELECT last_seen_at FROM seen_memories WHERE memory_id = ?", ("mem-1",)
        ).fetchone()

        assert row2[0] > row1[0]
        store.close()

    def test_get_seen_ids_respects_ttl(self, tmp_path):
        store = FeedbackStore(tmp_path / "test.db")
        store.initialize()

        store.mark_surfaced(["mem-old"])
        # Backdate the entry
        store._db.execute(
            "UPDATE seen_memories SET last_seen_at = ? WHERE memory_id = ?",
            (time.time() - 100, "mem-old"),
        )
        store._db.commit()

        store.mark_surfaced(["mem-new"])

        # TTL 50s → only mem-new visible
        seen = store.get_seen_ids(ttl_seconds=50)
        assert "mem-new" in seen
        assert "mem-old" not in seen

        # TTL 200s → both visible
        seen_all = store.get_seen_ids(ttl_seconds=200)
        assert seen_all == {"mem-old", "mem-new"}
        store.close()

    def test_cleanup_expired(self, tmp_path):
        store = FeedbackStore(tmp_path / "test.db")
        store.initialize()

        store.mark_surfaced(["mem-1", "mem-2"])
        # Backdate mem-1
        store._db.execute(
            "UPDATE seen_memories SET last_seen_at = ? WHERE memory_id = ?",
            (time.time() - 1000, "mem-1"),
        )
        store._db.commit()

        deleted = store.cleanup_expired(ttl_seconds=500)
        assert deleted == 1

        seen = store.get_seen_ids(ttl_seconds=999999)
        assert seen == {"mem-2"}
        store.close()

    def test_mark_surfaced_empty_list(self, tmp_path):
        store = FeedbackStore(tmp_path / "test.db")
        store.initialize()
        store.mark_surfaced([])  # should not error
        assert store.get_seen_ids(3600) == set()
        store.close()

    def test_mark_surfaced_no_db(self):
        store = FeedbackStore(Path("/nonexistent"))
        # Not initialized — _db is None
        store.mark_surfaced(["mem-1"])  # should not error
        assert store.get_seen_ids(3600) == set()


# ── SurfacingEngine cross-session dedup integration ──────────────────────


class TestCrossSessionDedup:
    async def test_engine_loads_persisted_ids(self, tmp_path):
        """Engine seeded with seen IDs from previous session skips those memories."""
        store = FeedbackStore(tmp_path / "feedback.db")
        store.initialize()

        chunk_old = FakeChunk(content="old memory")
        chunk_new = FakeChunk(content="new memory")

        # Simulate previous session: mark chunk_old as seen
        store.mark_surfaced([chunk_old.id])

        tracker = FeedbackTracker(
            config=_make_config(), db_path=tmp_path / "feedback.db"
        )

        results = [
            FakeSearchResult(chunk=chunk_old, score=0.5),
            FakeSearchResult(chunk=chunk_new, score=0.4),
        ]
        engine = SurfacingEngine(
            config=_make_config(),
            search_pipeline=_make_search_pipeline(results),
            feedback_tracker=tracker,
        )

        # chunk_old should be pre-loaded in _surfaced_ids
        assert chunk_old.id in engine._surfaced_ids
        assert chunk_new.id not in engine._surfaced_ids

        output = await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)
        # Only new memory should be surfaced
        assert "new memory" in output
        assert "old memory" not in output

        tracker.close()
        store.close()

    async def test_engine_persists_new_ids(self, tmp_path):
        """After surfacing, new memory IDs are persisted for future sessions."""
        tracker = FeedbackTracker(
            config=_make_config(), db_path=tmp_path / "feedback.db"
        )

        chunk = FakeChunk(content="surfaced now")
        results = [FakeSearchResult(chunk=chunk, score=0.5)]
        engine = SurfacingEngine(
            config=_make_config(),
            search_pipeline=_make_search_pipeline(results),
            feedback_tracker=tracker,
        )

        await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)

        # Verify persisted
        seen = tracker.store.get_seen_ids(ttl_seconds=3600)
        assert chunk.id in seen

        tracker.close()

    async def test_expired_ids_not_loaded(self, tmp_path):
        """Memory IDs older than dedup_ttl are not loaded on engine init."""
        store = FeedbackStore(tmp_path / "feedback.db")
        store.initialize()

        store.mark_surfaced(["mem-expired"])
        store._db.execute(
            "UPDATE seen_memories SET last_seen_at = ? WHERE memory_id = ?",
            (time.time() - 1000, "mem-expired"),
        )
        store._db.commit()
        store.close()

        tracker = FeedbackTracker(
            config=_make_config(dedup_ttl_seconds=500),  # 500s TTL
            db_path=tmp_path / "feedback.db",
        )

        engine = SurfacingEngine(
            config=_make_config(dedup_ttl_seconds=500),
            search_pipeline=_make_search_pipeline([]),
            feedback_tracker=tracker,
        )

        # Expired → not loaded
        assert "mem-expired" not in engine._surfaced_ids
        tracker.close()

    async def test_dedup_disabled_when_ttl_zero(self, tmp_path):
        """dedup_ttl_seconds=0 disables cross-session loading."""
        store = FeedbackStore(tmp_path / "feedback.db")
        store.initialize()
        store.mark_surfaced(["mem-1"])
        store.close()

        tracker = FeedbackTracker(
            config=_make_config(dedup_ttl_seconds=0),
            db_path=tmp_path / "feedback.db",
        )

        engine = SurfacingEngine(
            config=_make_config(dedup_ttl_seconds=0),
            search_pipeline=_make_search_pipeline([]),
            feedback_tracker=tracker,
        )

        # TTL=0 → no loading
        assert "mem-1" not in engine._surfaced_ids
        tracker.close()

    async def test_no_feedback_tracker_no_crash(self):
        """Engine without feedback_tracker still works (no cross-session dedup)."""
        chunk = FakeChunk(content="memory")
        results = [FakeSearchResult(chunk=chunk, score=0.5)]
        engine = SurfacingEngine(
            config=_make_config(),
            search_pipeline=_make_search_pipeline(results),
        )

        output = await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)
        assert "memory" in output
