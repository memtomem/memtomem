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
    # Both accessors resolve to the same connection so a check's *behaviour*
    # can be tested without pinning which one it picked. Which accessor each
    # check must use is pinned separately, in ``TestConnectionRouting``.
    app.storage._get_db.return_value = db
    app.storage._get_read_db.return_value = db
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


class TestConnectionRouting:
    """Which connection each check takes (#2185).

    A pure read on the writer connection contends with every write for no
    benefit; the WAL checkpoint genuinely needs the writer. Distinct mocks per
    accessor, so a check reaching for the wrong one produces the wrong rows and
    the assertion below fails loudly rather than passing on a shared mock.
    """

    @pytest.fixture
    def split_app(self):
        app = MagicMock()
        writer, reader = MagicMock(name="writer"), MagicMock(name="reader")
        app.storage._get_db.return_value = writer
        app.storage._get_read_db.return_value = reader
        return app, writer, reader

    @pytest.mark.asyncio
    async def test_dead_memory_pct_reads_from_the_pool(self, split_app):
        from memtomem.server.health_checks import check_dead_memory_pct

        app, writer, reader = split_app
        reader.execute.return_value.fetchone.return_value = (100, 90)
        snap = await check_dead_memory_pct(app)
        assert snap.value["pct"] == 90.0
        writer.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_db_fragmentation_reads_from_the_pool(self, split_app):
        from memtomem.server.health_checks import check_db_fragmentation

        app, writer, reader = split_app
        reader.execute.return_value.fetchone.side_effect = [(100,), (30,), (4096,)]
        snap = await check_db_fragmentation(app)
        assert snap.value["frag_pct"] == 30.0
        writer.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_connectivity_reads_from_the_pool(self, split_app):
        from memtomem.server.health_checks import check_sqlite_connectivity

        app, writer, reader = split_app
        reader.execute.return_value.fetchone.return_value = ("ok",)
        snap = await check_sqlite_connectivity(app)
        assert snap.status == "ok"
        writer.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_wal_status_keeps_the_writer(self, split_app):
        """``PRAGMA wal_checkpoint`` writes — it must not move to the pool."""
        from memtomem.server.health_checks import check_wal_status

        app, writer, reader = split_app
        writer.execute.return_value.fetchone.side_effect = [(0, 10, 10), (4096,)]
        snap = await check_wal_status(app)
        assert snap.status == "ok"
        reader.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_in_flight_transaction_is_busy_not_critical(self, split_app):
        """A concurrent write is the store working, not a damaged database."""
        from memtomem.errors import TransactionOwnedError
        from memtomem.server.health_checks import check_sqlite_connectivity

        app, _writer, _reader = split_app
        app.storage._get_read_db.side_effect = TransactionOwnedError(
            "SQLite transaction is owned by another task; retry after it completes"
        )
        snap = await check_sqlite_connectivity(app)
        assert snap.status == "warning"
        assert snap.value["busy"] is True

    @pytest.mark.asyncio
    async def test_real_storage_failure_is_still_critical(self, split_app):
        """The busy branch must not swallow a genuinely broken store."""
        from memtomem.errors import StorageError
        from memtomem.server.health_checks import check_sqlite_connectivity

        app, _writer, _reader = split_app
        app.storage._get_read_db.side_effect = StorageError("database disk image is malformed")
        snap = await check_sqlite_connectivity(app)
        assert snap.status == "critical"


class TestDiagnosticChecks:
    @pytest.mark.asyncio
    async def test_orphan_count_zero(self, mock_app, tmp_path):
        from memtomem.server.health_checks import check_orphan_count

        app, _db = mock_app
        existing = tmp_path / "note.md"
        existing.write_text("hello", encoding="utf-8")
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
        app.storage.get_health_report.assert_awaited_once_with(project_context_root=None)

    @pytest.mark.asyncio
    async def test_full_health_report_unavailable_sessions_stay_none(self, mock_app):
        """An ``available: false`` sessions block records None, not 0 (#2281)."""
        from memtomem.server.health_checks import check_full_health_report

        app, _db = mock_app
        app.storage.get_health_report = AsyncMock(
            return_value={
                "total_chunks": 10,
                "dead_memories_pct": 10.0,
                "access_coverage": {"pct": 90.0},
                "tag_coverage": {"pct": 50.0},
                "sessions": {
                    "total": None,
                    "active": None,
                    "recent_7d": None,
                    "available": False,
                    "reason": "no_project_identity",
                },
                "cross_references": 1,
            }
        )

        snap = await check_full_health_report(app)

        assert snap.value["active_sessions"] is None

    @pytest.mark.asyncio
    async def test_full_health_report_uses_registered_project_boundary(
        self, mock_app, tmp_path, monkeypatch
    ):
        from memtomem.config import Mem2MemConfig
        from memtomem.server.health_checks import check_full_health_report

        app, _db = mock_app
        project_root = tmp_path / "project"
        project_root.mkdir()
        config = Mem2MemConfig()
        config.indexing.project_memory_dirs = [project_root / ".memtomem" / "memories"]
        app.config = config
        monkeypatch.chdir(project_root)
        app.storage.get_health_report = AsyncMock(
            return_value={
                "total_chunks": 4,
                "dead_memories_pct": 75.0,
                "access_coverage": {"pct": 25.0},
                "tag_coverage": {"pct": 50.0},
                "sessions": {"active": 0},
                "cross_references": 0,
            }
        )

        snap = await check_full_health_report(app)

        assert snap.value["total_chunks"] == 4
        app.storage.get_health_report.assert_awaited_once_with(
            project_context_root=project_root.resolve()
        )

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
    async def test_cleanup_orphans(self, mock_app, tmp_path, monkeypatch):
        from memtomem.server.health_maintenance import MaintenanceExecutor
        from memtomem.storage import orphan_detect

        # Keep the #1565 two-pass re-check instant.
        monkeypatch.setattr(orphan_detect, "ORPHAN_RECHECK_DELAY_SECONDS", 0.0)

        app, _db = mock_app
        config = HealthWatchdogConfig(enabled=True)
        missing = tmp_path / "gone.md"
        app.storage.get_all_source_files = AsyncMock(return_value={missing})
        app.storage.delete_by_source = AsyncMock(return_value=5)

        executor = MaintenanceExecutor(app, config)
        # One orphan is below the mass-delete brake, so it deletes normally.
        result = await executor.cleanup_orphans()
        assert result["orphaned"] == 1
        assert result["deleted_chunks"] == 5
        # #2159-class: deleting chunks must invalidate the search cache, or
        # searches keep returning the deleted chunks for the rest of cache_ttl.
        app.search_pipeline.invalidate_cache.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup_orphans_skips_suspected_mass_delete(
        self, mock_app, tmp_path, monkeypatch
    ):
        """#1565 — a mount blip vanishing every source is refused, not wiped."""
        from memtomem.server.health_maintenance import MaintenanceExecutor
        from memtomem.storage import orphan_detect

        monkeypatch.setattr(orphan_detect, "ORPHAN_RECHECK_DELAY_SECONDS", 0.0)

        app, _db = mock_app
        config = HealthWatchdogConfig(enabled=True)
        missing = {tmp_path / f"gone-{i}.md" for i in range(12)}
        app.storage.get_all_source_files = AsyncMock(return_value=missing)
        app.storage.delete_by_source = AsyncMock(return_value=5)

        executor = MaintenanceExecutor(app, config)
        result = await executor.cleanup_orphans()
        assert result["orphaned"] == 12
        assert result["deleted_chunks"] == 0
        assert result["skipped_reason"] == "orphan_ratio_exceeded"
        app.storage.delete_by_source.assert_not_awaited()
        # Nothing was deleted, so the search cache must stay warm.
        app.search_pipeline.invalidate_cache.assert_not_called()

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
    @pytest.mark.parametrize(
        ("check_name", "expect_maintained"),
        [("search_cache_size", True), ("dead_memory_pct", False)],
    )
    async def test_warning_tier_maintenance_is_opt_in(
        self, mock_app, tmp_path, check_name, expect_maintained
    ):
        """``search_cache_size`` tops out at "warning", so maintenance must act there.

        Every other check keeps the critical-only contract — a warning from
        ``dead_memory_pct`` must not trigger auto-maintenance.
        """
        from memtomem.server.health_watchdog import HealthWatchdog

        app, _db = mock_app
        app.config = MagicMock()
        app.config.storage.sqlite_path = tmp_path / "test.db"

        wd = HealthWatchdog(app, HealthWatchdogConfig(enabled=True))
        with patch("memtomem.server.health_watchdog.HEARTBEAT_CHECKS", []):  # keep the loop inert
            await wd.start()
        try:
            wd._auto_maintain = AsyncMock()
            snap = HealthSnapshot(
                tier="heartbeat",
                check_name=check_name,
                value={"size": 41},
                status="warning",
                created_at=time.time(),
            )
            await wd._run_check(AsyncMock(return_value=snap))
            assert wd._auto_maintain.await_count == (1 if expect_maintained else 0)
        finally:
            await wd.stop()

    @pytest.mark.asyncio
    async def test_warning_tier_maintenance_trims_the_cache(self, mock_app, tmp_path):
        """End-to-end: a warning snapshot reaches ``trim_search_cache``."""
        from memtomem.server.health_watchdog import HealthWatchdog

        app, _db = mock_app
        app.config = MagicMock()
        app.config.storage.sqlite_path = tmp_path / "test.db"

        wd = HealthWatchdog(app, HealthWatchdogConfig(enabled=True))
        with patch("memtomem.server.health_watchdog.HEARTBEAT_CHECKS", []):
            await wd.start()
        try:
            wd._maintenance.trim_search_cache = AsyncMock(
                return_value={"before": 41, "after": 30, "evicted": 11}
            )
            snap = HealthSnapshot(
                tier="heartbeat",
                check_name="search_cache_size",
                value={"size": 41},
                status="warning",
                created_at=time.time(),
            )
            await wd._run_check(AsyncMock(return_value=snap))
            wd._maintenance.trim_search_cache.assert_awaited_once()
        finally:
            await wd.stop()

    @pytest.mark.asyncio
    async def test_warning_tier_maintenance_respects_the_switch(self, mock_app, tmp_path):
        """``auto_maintenance=False`` still disables the warning tier."""
        from memtomem.server.health_watchdog import HealthWatchdog

        app, _db = mock_app
        app.config = MagicMock()
        app.config.storage.sqlite_path = tmp_path / "test.db"

        wd = HealthWatchdog(app, HealthWatchdogConfig(enabled=True, auto_maintenance=False))
        with patch("memtomem.server.health_watchdog.HEARTBEAT_CHECKS", []):
            await wd.start()
        try:
            wd._auto_maintain = AsyncMock()
            snap = HealthSnapshot(
                tier="heartbeat",
                check_name="search_cache_size",
                value={"size": 41},
                status="warning",
                created_at=time.time(),
            )
            await wd._run_check(AsyncMock(return_value=snap))
            wd._auto_maintain.assert_not_awaited()
        finally:
            await wd.stop()

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
