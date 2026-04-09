"""SQLite-backed persistence for health watchdog snapshots."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class HealthSnapshot:
    """Single health check result."""

    tier: str  # heartbeat / diagnostic / deep
    check_name: str
    value: dict
    status: str  # ok / warning / critical
    created_at: float


class HealthStore:
    """Thread-safe SQLite store for health snapshots."""

    def __init__(self, db_path: Path, max_snapshots: int = 1000) -> None:
        self._db_path = db_path
        self._max_snapshots = max_snapshots
        self._db: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def initialize(self) -> None:
        self._db = sqlite3.connect(str(self._db_path), check_same_thread=False, timeout=5.0)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=3000")
        # Table created by sqlite_schema.py; ensure idempotent
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS health_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tier TEXT NOT NULL,
                check_name TEXT NOT NULL,
                value_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'ok',
                created_at REAL NOT NULL
            )
        """)
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_health_snap_name "
            "ON health_snapshots(check_name, created_at)"
        )
        self._db.commit()

    def close(self) -> None:
        if self._db:
            self._db.close()
            self._db = None

    def record(self, snapshot: HealthSnapshot) -> None:
        if self._db is None:
            return
        with self._lock:
            self._db.execute(
                "INSERT INTO health_snapshots (tier, check_name, value_json, status, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    snapshot.tier,
                    snapshot.check_name,
                    json.dumps(snapshot.value, default=str),
                    snapshot.status,
                    snapshot.created_at,
                ),
            )
            self._db.commit()
            self._trim()

    def get_latest(self, check_name: str | None = None, limit: int = 1) -> list[HealthSnapshot]:
        if self._db is None:
            return []
        if check_name:
            rows = self._db.execute(
                "SELECT tier, check_name, value_json, status, created_at "
                "FROM health_snapshots WHERE check_name = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (check_name, limit),
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT tier, check_name, value_json, status, created_at "
                "FROM health_snapshots ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_snapshot(r) for r in rows]

    def get_trend(self, check_name: str, hours: float = 24.0) -> list[HealthSnapshot]:
        if self._db is None:
            return []
        cutoff = time.time() - hours * 3600
        rows = self._db.execute(
            "SELECT tier, check_name, value_json, status, created_at "
            "FROM health_snapshots WHERE check_name = ? AND created_at >= ? "
            "ORDER BY created_at ASC",
            (check_name, cutoff),
        ).fetchall()
        return [self._row_to_snapshot(r) for r in rows]

    def get_summary(self) -> dict[str, dict]:
        """Return the latest snapshot per unique check_name."""
        if self._db is None:
            return {}
        rows = self._db.execute(
            "SELECT tier, check_name, value_json, status, created_at "
            "FROM health_snapshots h1 "
            "WHERE created_at = ("
            "  SELECT MAX(created_at) FROM health_snapshots h2 "
            "  WHERE h2.check_name = h1.check_name"
            ") "
            "ORDER BY check_name"
        ).fetchall()
        return {
            r[1]: {"tier": r[0], "status": r[3], "value": json.loads(r[2]), "at": r[4]}
            for r in rows
        }

    def _trim(self) -> None:
        if self._db is None:
            return
        count = self._db.execute("SELECT COUNT(*) FROM health_snapshots").fetchone()[0]
        if count > self._max_snapshots:
            excess = count - self._max_snapshots
            self._db.execute(
                "DELETE FROM health_snapshots WHERE id IN "
                "(SELECT id FROM health_snapshots ORDER BY created_at ASC LIMIT ?)",
                (excess,),
            )
            self._db.commit()

    @staticmethod
    def _row_to_snapshot(row: tuple) -> HealthSnapshot:
        return HealthSnapshot(
            tier=row[0],
            check_name=row[1],
            value=json.loads(row[2]),
            status=row[3],
            created_at=row[4],
        )
