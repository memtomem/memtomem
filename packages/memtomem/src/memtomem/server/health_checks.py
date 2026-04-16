"""Individual health check functions for the watchdog system.

Each function takes AppContext and returns a HealthSnapshot.
Organized into three tiers by cost: heartbeat (cheap), diagnostic (moderate), deep (expensive).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from memtomem.server.health_store import HealthSnapshot

if TYPE_CHECKING:
    from memtomem.server.context import AppContext
    from memtomem.server.health_store import HealthStore

logger = logging.getLogger(__name__)

_WAL_CRITICAL_BYTES = 50 * 1024 * 1024  # 50 MB
_WAL_WARNING_BYTES = 20 * 1024 * 1024  # 20 MB

# ---------------------------------------------------------------------------
# Heartbeat tier — ~60s interval, near-zero cost
# ---------------------------------------------------------------------------


async def check_sqlite_connectivity(app: AppContext) -> HealthSnapshot:
    """PRAGMA quick_check(1) on main DB."""
    now = time.time()
    try:
        db = app.storage._get_db()
        result = db.execute("PRAGMA quick_check(1)").fetchone()
        ok = result and result[0] == "ok"
        return HealthSnapshot(
            tier="heartbeat",
            check_name="sqlite_connectivity",
            value={"result": result[0] if result else "no_result"},
            status="ok" if ok else "critical",
            created_at=now,
        )
    except Exception as exc:
        return HealthSnapshot(
            tier="heartbeat",
            check_name="sqlite_connectivity",
            value={"error": str(exc)},
            status="critical",
            created_at=now,
        )


async def check_search_cache_size(app: AppContext) -> HealthSnapshot:
    """Check search pipeline cache size."""
    now = time.time()
    cache = app.search_pipeline._search_cache
    size = len(cache)
    return HealthSnapshot(
        tier="heartbeat",
        check_name="search_cache_size",
        value={"size": size},
        status="warning" if size > 40 else "ok",
        created_at=now,
    )


# ---------------------------------------------------------------------------
# Diagnostic tier — ~5min interval, moderate cost
# ---------------------------------------------------------------------------


async def check_orphan_count(app: AppContext) -> HealthSnapshot:
    """Count source files that no longer exist on disk."""
    now = time.time()
    source_files = await app.storage.get_all_source_files()

    def _count_orphans() -> tuple[int, int]:
        orphaned = 0
        for sf in source_files:
            if not sf.exists():
                orphaned += 1
        return orphaned, len(source_files)

    orphaned, total = await asyncio.to_thread(_count_orphans)

    if orphaned == 0:
        status = "ok"
    elif orphaned < 10:
        status = "warning"
    else:
        status = "critical"

    return HealthSnapshot(
        tier="diagnostic",
        check_name="orphan_count",
        value={"orphaned": orphaned, "total_sources": total},
        status=status,
        created_at=now,
    )


async def check_dead_memory_pct(app: AppContext) -> HealthSnapshot:
    """Percentage of chunks never accessed (access_count=0)."""
    now = time.time()
    db = app.storage._get_db()
    row = db.execute(
        "SELECT COUNT(*), SUM(CASE WHEN access_count = 0 THEN 1 ELSE 0 END) FROM chunks"
    ).fetchone()
    total = row[0] or 0
    dead = row[1] or 0
    pct = round(dead / total * 100, 1) if total > 0 else 0.0

    if pct > 80:
        status = "critical"
    elif pct > 50:
        status = "warning"
    else:
        status = "ok"

    return HealthSnapshot(
        tier="diagnostic",
        check_name="dead_memory_pct",
        value={"dead": dead, "total": total, "pct": pct},
        status=status,
        created_at=now,
    )


async def check_wal_status(app: AppContext) -> HealthSnapshot:
    """Check WAL checkpoint status."""
    now = time.time()
    db = app.storage._get_db()
    row = db.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
    # row = (busy, log_pages, checkpointed_pages)
    busy, log_pages, checkpointed = row if row else (0, 0, 0)
    page_size = db.execute("PRAGMA page_size").fetchone()[0]
    wal_bytes = log_pages * page_size

    if wal_bytes > _WAL_CRITICAL_BYTES:
        status = "critical"
    elif wal_bytes > _WAL_WARNING_BYTES:
        status = "warning"
    else:
        status = "ok"

    return HealthSnapshot(
        tier="diagnostic",
        check_name="wal_status",
        value={
            "log_pages": log_pages,
            "checkpointed": checkpointed,
            "wal_mb": round(wal_bytes / 1024 / 1024, 2),
        },
        status=status,
        created_at=now,
    )


# ---------------------------------------------------------------------------
# Deep tier — ~1h interval, expensive
# ---------------------------------------------------------------------------


async def check_full_health_report(app: AppContext) -> HealthSnapshot:
    """Reuse storage.get_health_report() for comprehensive metrics."""
    now = time.time()
    report = await app.storage.get_health_report()

    dead_pct = report.get("dead_memories_pct", 0)
    if dead_pct > 80:
        status = "critical"
    elif dead_pct > 50:
        status = "warning"
    else:
        status = "ok"

    return HealthSnapshot(
        tier="deep",
        check_name="full_health_report",
        value={
            "total_chunks": report["total_chunks"],
            "dead_memories_pct": dead_pct,
            "access_coverage_pct": report["access_coverage"]["pct"],
            "tag_coverage_pct": report["tag_coverage"]["pct"],
            "active_sessions": report["sessions"]["active"],
            "cross_references": report["cross_references"],
        },
        status=status,
        created_at=now,
    )


async def check_trend_comparison(app: AppContext, store: HealthStore) -> HealthSnapshot:
    """Compare current dead_memory_pct to 24h-ago baseline."""
    now = time.time()
    trend = store.get_trend("dead_memory_pct", hours=24.0)

    if not trend:
        return HealthSnapshot(
            tier="deep",
            check_name="trend_comparison",
            value={"note": "no_baseline_yet"},
            status="ok",
            created_at=now,
        )

    baseline_pct = trend[0].value.get("pct", 0)
    latest = store.get_latest("dead_memory_pct", limit=1)
    current_pct = latest[0].value.get("pct", 0) if latest else 0
    delta = round(current_pct - baseline_pct, 1)

    if delta > 10:
        status = "warning"
    else:
        status = "ok"

    return HealthSnapshot(
        tier="deep",
        check_name="trend_comparison",
        value={"baseline_pct": baseline_pct, "current_pct": current_pct, "delta": delta},
        status=status,
        created_at=now,
    )


async def check_db_fragmentation(app: AppContext) -> HealthSnapshot:
    """Check SQLite page usage and freelist."""
    now = time.time()
    db = app.storage._get_db()
    page_count = db.execute("PRAGMA page_count").fetchone()[0]
    freelist = db.execute("PRAGMA freelist_count").fetchone()[0]
    page_size = db.execute("PRAGMA page_size").fetchone()[0]

    frag_pct = round(freelist / page_count * 100, 1) if page_count > 0 else 0.0
    db_mb = round(page_count * page_size / 1024 / 1024, 2)

    return HealthSnapshot(
        tier="deep",
        check_name="db_fragmentation",
        value={
            "page_count": page_count,
            "freelist": freelist,
            "frag_pct": frag_pct,
            "db_mb": db_mb,
        },
        status="warning" if frag_pct > 20 else "ok",
        created_at=now,
    )


# ---------------------------------------------------------------------------
# Tier registries
# ---------------------------------------------------------------------------

HEARTBEAT_CHECKS = [check_sqlite_connectivity, check_search_cache_size]
DIAGNOSTIC_CHECKS = [check_orphan_count, check_dead_memory_pct, check_wal_status]
DEEP_CHECKS = [check_full_health_report, check_db_fragmentation]
# check_trend_comparison is handled separately (needs store arg)
