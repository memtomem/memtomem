"""SQLite persistent metrics store for proxy call history."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path

from memtomem_stm.proxy.metrics import CallMetrics

logger = logging.getLogger(__name__)

_CREATE = """
CREATE TABLE IF NOT EXISTS proxy_metrics (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    server          TEXT    NOT NULL,
    tool            TEXT    NOT NULL,
    original_chars  INTEGER NOT NULL,
    compressed_chars INTEGER NOT NULL,
    cleaned_chars   INTEGER NOT NULL DEFAULT 0,
    created_at      REAL    NOT NULL
);
"""

_INDEX = "CREATE INDEX IF NOT EXISTS idx_metrics_created ON proxy_metrics(created_at);"


class MetricsStore:
    """SQLite-backed persistent metrics for proxy calls."""

    def __init__(self, db_path: Path, max_history: int = 10000) -> None:
        self._db_path = db_path
        self._max_history = max_history
        self._db: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def initialize(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._db = sqlite3.connect(str(self._db_path), check_same_thread=False, timeout=5.0)
        try:
            self._db_path.chmod(0o600)
        except OSError:
            pass
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=3000")
        self._db.execute(_CREATE)
        self._db.execute(_INDEX)
        self._db.commit()

    def close(self) -> None:
        if self._db:
            self._db.close()
            self._db = None

    def record(self, metrics: CallMetrics) -> None:
        if self._db is None:
            return
        now = time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO proxy_metrics (server, tool, original_chars, compressed_chars, cleaned_chars, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    metrics.server,
                    metrics.tool,
                    metrics.original_chars,
                    metrics.compressed_chars,
                    metrics.cleaned_chars,
                    now,
                ),
            )
            self._db.commit()
            self._trim()

    def _trim(self) -> None:
        if self._db is None:
            return
        count = self._db.execute("SELECT COUNT(*) FROM proxy_metrics").fetchone()[0]
        if count > self._max_history:
            excess = count - self._max_history
            self._db.execute(
                "DELETE FROM proxy_metrics WHERE id IN "
                "(SELECT id FROM proxy_metrics ORDER BY created_at ASC LIMIT ?)",
                (excess,),
            )
            self._db.commit()

    def get_history(self, limit: int = 100) -> list[dict]:
        if self._db is None:
            return []
        rows = self._db.execute(
            "SELECT server, tool, original_chars, compressed_chars, cleaned_chars, created_at "
            "FROM proxy_metrics ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "server": r[0],
                "tool": r[1],
                "original_chars": r[2],
                "compressed_chars": r[3],
                "cleaned_chars": r[4],
                "created_at": r[5],
            }
            for r in rows
        ]
