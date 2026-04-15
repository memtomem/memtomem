"""Tests for the health watchdog system."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from memtomem.config import HealthWatchdogConfig
from memtomem.server.health_store import HealthSnapshot, HealthStore


# ── HealthStore tests ──────────────────────────────────────────────


class TestHealthStore:
    @pytest.fixture
    def store(self, tmp_path):
        s = HealthStore(tmp_path / "test.db", max_snapshots=10)
        s.initialize()
        yield s
        s.close()

    def _snap(self, name="test_check", status="ok", value=None, tier="heartbeat"):
        return HealthSnapshot(
            tier=tier,
            check_name=name,
            value=value or {"v": 1},
            status=status,
            created_at=time.time(),
        )

    def test_record_and_get_latest(self, store):
        store.record(self._snap(value={"v": 1}))
        store.record(self._snap(value={"v": 2}))
        latest = store.get_latest("test_check", limit=1)
        assert len(latest) == 1
        assert latest[0].value["v"] == 2

    def test_get_latest_all(self, store):
        store.record(self._snap(name="a"))
        store.record(self._snap(name="b"))
        latest = store.get_latest(check_name=None, limit=10)
        assert len(latest) == 2

    def test_get_trend(self, store):
        for i in range(5):
            snap = self._snap(value={"i": i})
            snap.created_at = time.time() - (4 - i) * 60  # spread over 4 minutes
            store.record(snap)
        trend = store.get_trend("test_check", hours=1.0)
        assert len(trend) == 5
        assert trend[0].value["i"] == 0  # oldest first

    def test_get_trend_excludes_old(self, store):
        old = self._snap(value={"old": True})
        old.created_at = time.time() - 48 * 3600  # 48h ago
        store.record(old)
        store.record(self._snap(value={"new": True}))
        trend = store.get_trend("test_check", hours=24.0)
        assert len(trend) == 1
        assert trend[0].value.get("new")

    def test_get_summary(self, store):
        store.record(self._snap(name="a", status="ok"))
        store.record(self._snap(name="b", status="warning"))
        store.record(self._snap(name="a", status="critical"))
        summary = store.get_summary()
        assert summary["a"]["status"] == "critical"
        assert summary["b"]["status"] == "warning"

    def test_trim(self, store):
        for i in range(15):
            store.record(self._snap(value={"i": i}))
        # max_snapshots=10, so only 10 should remain
        all_snaps = store.get_latest(check_name=None, limit=100)
        assert len(all_snaps) <= 10

    def test_close_and_reopen(self, tmp_path):
        db_path = tmp_path / "persist.db"
        s1 = HealthStore(db_path, max_snapshots=100)
        s1.initialize()
        s1.record(self._snap(value={"persist": True}))
        s1.close()

        s2 = HealthStore(db_path, max_snapshots=100)
        s2.initialize()
        latest = s2.get_latest("test_check", limit=1)
        assert len(latest) == 1
        assert latest[0].value["persist"] is True
        s2.close()

    def test_record_on_closed_store(self, tmp_path):
        s = HealthStore(tmp_path / "test.db", max_snapshots=10)
        # Not initialized — should not raise
        s.record(self._snap())
        assert s.get_latest("test_check") == []


# ── Health check function tests ────────────────────────────────────


@pytest.fixture
def mock_app():
    """Create a minimal mock AppContext for health checks."""
    app = MagicMock()
    db = MagicMock()
    app.storage._get_db.return_value = db
    app.search_pipeline._search_cache = {}
    return app, db


class TestHeartbeatChecks:
    @pytest.mark.asyncio
    async def test_sqlite_connectivity_ok(self, mock_app):
        from memtomem.server.health_checks import check_sqlite_connectivity

        app, db = mock_app
        db.execute.return_value.fetchone.return_value = ("ok",)
        snap = await check_sqlite_connectivity(app)
        assert snap.status == "ok"
        assert snap.check_name == "sqlite_connectivity"

    @pytest.mark.asyncio
    async def test_sqlite_connectivity_fail(self, mock_app):
        from memtomem.server.health_checks import check_sqlite_connectivity

        app, db = mock_app
        db.execute.side_effect = Exception("db locked")
        snap = await check_sqlite_connectivity(app)
        assert snap.status == "critical"

    @pytest.mark.asyncio
    async def test_search_cache_size_ok(self, mock_app):
        from memtomem.server.health_checks import check_search_cache_size

        app, _db = mock_app
        app.search_pipeline._search_cache = {"a": 1, "b": 2}
        snap = await check_search_cache_size(app)
        assert snap.status == "ok"
        assert snap.value["size"] == 2

    @pytest.mark.asyncio
    async def test_search_cache_size_warning(self, mock_app):
        from memtomem.server.health_checks import check_search_cache_size

        app, _db = mock_app
        app.search_pipeline._search_cache = {str(i): i for i in range(45)}
        snap = await check_search_cache_size(app)
        assert snap.status == "warning"


class TestDiagnosticChecks:
    @pytest.mark.asyncio
    async def test_orphan_count_zero(self, mock_app, tmp_path):
        from memtomem.server.health_checks import check_orphan_count

        app, _db = mock_app
        existing = tmp_path / "note.md"
        existing.write_text("hello")
        app.storage.get_all_source_files = AsyncMock(return_value={existing})
        snap = await check_orphan_count(app)
        assert snap.status == "ok"
        assert snap.value["orphaned"] == 0

    @pytest.mark.asyncio
    async def test_orphan_count_critical(self, mock_app, tmp_path):
        from memtomem.server.health_checks import check_orphan_count

        app, _db = mock_app
        missing = {tmp_path / f"gone_{i}.md" for i in range(15)}
        app.storage.get_all_source_files = AsyncMock(return_value=missing)
        snap = await check_orphan_count(app)
        assert snap.status == "critical"
        assert snap.value["orphaned"] == 15

    @pytest.mark.asyncio
    async def test_dead_memory_pct(self, mock_app):
        from memtomem.server.health_checks import check_dead_memory_pct

        app, db = mock_app
        db.execute.return_value.fetchone.return_value = (100, 90)  # 90% dead
        snap = await check_dead_memory_pct(app)
        assert snap.status == "critical"
        assert snap.value["pct"] == 90.0

    @pytest.mark.asyncio
    async def test_wal_status_ok(self, mock_app):
        from memtomem.server.health_checks import check_wal_status

        app, db = mock_app
        # Simulate: first call = wal_checkpoint, second = page_size
        db.execute.return_value.fetchone.side_effect = [(0, 10, 10), (4096,)]
        snap = await check_wal_status(app)
        assert snap.status == "ok"


class TestDeepChecks:
    @pytest.mark.asyncio
    async def test_full_health_report(self, mock_app):
        from memtomem.server.health_checks import check_full_health_report

        app, _db = mock_app
        app.storage.get_health_report = AsyncMock(
            return_value={
                "total_chunks": 100,
                "dead_memories_pct": 30.0,
                "access_coverage": {"pct": 70.0},
                "tag_coverage": {"pct": 50.0},
                "sessions": {"active": 2},
                "cross_references": 10,
            }
        )
        snap = await check_full_health_report(app)
        assert snap.status == "ok"
        assert snap.value["total_chunks"] == 100

    @pytest.mark.asyncio
    async def test_db_fragmentation(self, mock_app):
        from memtomem.server.health_checks import check_db_fragmentation

        app, db = mock_app
        db.execute.return_value.fetchone.side_effect = [(1000,), (50,), (4096,)]
        snap = await check_db_fragmentation(app)
        assert snap.status == "ok"
        assert snap.value["frag_pct"] == 5.0


# ── MaintenanceExecutor tests ─────────────────────────────────────


class TestMaintenanceExecutor:
    @pytest.mark.asyncio
    async def test_cleanup_orphans(self, mock_app, tmp_path):
        from memtomem.server.health_maintenance import MaintenanceExecutor

        app, _db = mock_app
        config = HealthWatchdogConfig(enabled=True)
        missing = tmp_path / "gone.md"
        app.storage.get_all_source_files = AsyncMock(return_value={missing})
        app.storage.delete_by_source = AsyncMock(return_value=5)

        executor = MaintenanceExecutor(app, config)
        result = await executor.cleanup_orphans()
        assert result["orphaned"] == 1
        assert result["deleted_chunks"] == 5

    @pytest.mark.asyncio
    async def test_trim_search_cache(self, mock_app):
        from memtomem.server.health_maintenance import MaintenanceExecutor

        app, _db = mock_app
        config = HealthWatchdogConfig(enabled=True)
        app.search_pipeline._search_cache = {str(i): (time.time() - i, [], None) for i in range(40)}

        executor = MaintenanceExecutor(app, config)
        result = await executor.trim_search_cache(max_entries=10)
        assert result["evicted"] == 30
        assert result["after"] == 10


# ── HealthWatchdog lifecycle tests ─────────────────────────────────


class TestHealthWatchdog:
    @pytest.mark.asyncio
    async def test_start_stop(self, mock_app, tmp_path):
        from memtomem.server.health_watchdog import HealthWatchdog

        app, db = mock_app
        app.config = MagicMock()
        app.config.storage.sqlite_path = tmp_path / "test.db"

        config = HealthWatchdogConfig(
            enabled=True,
            heartbeat_interval_seconds=0.1,
            diagnostic_interval_seconds=100,
            deep_interval_seconds=100,
        )
        wd = HealthWatchdog(app, config)

        # Mock all checks to avoid real DB calls
        with patch("memtomem.server.health_watchdog.HEARTBEAT_CHECKS", []):
            await wd.start()
            assert wd._task is not None
            await asyncio.sleep(0.05)
            await wd.stop()
            assert wd._task is None

    @pytest.mark.asyncio
    async def test_run_now(self, mock_app, tmp_path):
        from memtomem.server.health_watchdog import HealthWatchdog

        app, db = mock_app
        app.config = MagicMock()
        app.config.storage.sqlite_path = tmp_path / "test.db"

        # Mock storage calls
        db.execute.return_value.fetchone.return_value = ("ok",)
        app.storage.get_all_source_files = AsyncMock(return_value=set())
        app.storage.get_health_report = AsyncMock(
            return_value={
                "total_chunks": 0,
                "dead_memories_pct": 0,
                "access_coverage": {"pct": 0},
                "tag_coverage": {"pct": 0},
                "sessions": {"active": 0},
                "cross_references": 0,
            }
        )

        config = HealthWatchdogConfig(enabled=True)
        wd = HealthWatchdog(app, config)
        await wd.start()
        results = await wd.run_now()
        await wd.stop()

        assert "sqlite_connectivity" in results
        assert results["sqlite_connectivity"]["status"] == "ok"

    @pytest.mark.asyncio
    async def test_get_status_disabled(self, mock_app, tmp_path):
        from memtomem.server.health_watchdog import HealthWatchdog

        app, _db = mock_app
        app.config = MagicMock()
        app.config.storage.sqlite_path = tmp_path / "test.db"

        config = HealthWatchdogConfig(enabled=False)
        wd = HealthWatchdog(app, config)
        assert wd.get_status() == {"enabled": False}


# ── Config tests ───────────────────────────────────────────────────


class TestConfig:
    def test_default_config(self):
        config = HealthWatchdogConfig()
        assert config.enabled is False
        assert config.heartbeat_interval_seconds == 60.0
        assert config.auto_maintenance is True

    def test_config_in_mem2mem(self):
        from memtomem.config import Mem2MemConfig

        config = Mem2MemConfig()
        assert hasattr(config, "health_watchdog")
        assert config.health_watchdog.enabled is False
