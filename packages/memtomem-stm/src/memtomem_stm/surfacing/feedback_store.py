"""SQLite persistence for surfacing events and feedback."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS surfacing_events (
    id          TEXT    PRIMARY KEY,
    server      TEXT    NOT NULL,
    tool        TEXT    NOT NULL,
    query       TEXT    NOT NULL,
    memory_ids  TEXT    NOT NULL,
    scores      TEXT    NOT NULL,
    created_at  REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS surfacing_feedback (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    surfacing_id    TEXT    NOT NULL REFERENCES surfacing_events(id),
    memory_id       TEXT,
    rating          TEXT    NOT NULL,
    created_at      REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS seen_memories (
    memory_id       TEXT    PRIMARY KEY,
    first_seen_at   REAL    NOT NULL,
    last_seen_at    REAL    NOT NULL,
    seen_count      INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_feedback_surfacing ON surfacing_feedback(surfacing_id);
CREATE INDEX IF NOT EXISTS idx_events_tool ON surfacing_events(tool);
CREATE INDEX IF NOT EXISTS idx_seen_last ON seen_memories(last_seen_at);
"""


class FeedbackStore:
    """SQLite store for surfacing events and feedback ratings."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def initialize(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._db = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        if self._db:
            self._db.close()
            self._db = None

    def record_surfacing(
        self,
        surfacing_id: str,
        server: str,
        tool: str,
        query: str,
        memory_ids: list[str],
        scores: list[float],
    ) -> None:
        if self._db is None:
            return
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO surfacing_events (id, server, tool, query, memory_ids, scores, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    surfacing_id,
                    server,
                    tool,
                    query,
                    json.dumps(memory_ids),
                    json.dumps(scores),
                    time.time(),
                ),
            )
            self._db.commit()

    def record_feedback(
        self,
        surfacing_id: str,
        rating: str,
        memory_id: str | None = None,
    ) -> bool:
        if self._db is None:
            return False
        # Verify surfacing event exists
        exists = self._db.execute(
            "SELECT 1 FROM surfacing_events WHERE id = ?", (surfacing_id,)
        ).fetchone()
        if not exists:
            return False
        with self._lock:
            self._db.execute(
                "INSERT INTO surfacing_feedback (surfacing_id, memory_id, rating, created_at) "
                "VALUES (?, ?, ?, ?)",
                (surfacing_id, memory_id, rating, time.time()),
            )
            self._db.commit()
        return True

    def get_memory_ids_for_surfacing(self, surfacing_id: str) -> list[str]:
        """Return memory_ids from a surfacing event."""
        if self._db is None:
            return []
        row = self._db.execute(
            "SELECT memory_ids FROM surfacing_events WHERE id = ?", (surfacing_id,)
        ).fetchone()
        if not row:
            return []
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return []

    def get_tool_feedback_summary(self, tool: str | None = None) -> dict:
        """Get feedback summary, optionally filtered by tool."""
        if self._db is None:
            return {"total_surfacings": 0, "total_feedback": 0, "by_rating": {}}

        if tool:
            total_surfacings = self._db.execute(
                "SELECT COUNT(*) FROM surfacing_events WHERE tool = ?", (tool,)
            ).fetchone()[0]
            rows = self._db.execute(
                "SELECT f.rating, COUNT(*) FROM surfacing_feedback f "
                "JOIN surfacing_events e ON f.surfacing_id = e.id "
                "WHERE e.tool = ? GROUP BY f.rating",
                (tool,),
            ).fetchall()
        else:
            total_surfacings = self._db.execute("SELECT COUNT(*) FROM surfacing_events").fetchone()[
                0
            ]
            rows = self._db.execute(
                "SELECT rating, COUNT(*) FROM surfacing_feedback GROUP BY rating"
            ).fetchall()

        by_rating = {r[0]: r[1] for r in rows}
        total_feedback = sum(by_rating.values())

        return {
            "total_surfacings": total_surfacings,
            "total_feedback": total_feedback,
            "by_rating": by_rating,
        }

    # ── Cross-session dedup ────────────────────────────────────────────

    def mark_surfaced(self, memory_ids: list[str]) -> None:
        """Record memory IDs as surfaced for cross-session dedup."""
        if self._db is None or not memory_ids:
            return
        now = time.time()
        with self._lock:
            for mid in memory_ids:
                self._db.execute(
                    "INSERT INTO seen_memories (memory_id, first_seen_at, last_seen_at, seen_count) "
                    "VALUES (?, ?, ?, 1) "
                    "ON CONFLICT(memory_id) DO UPDATE SET "
                    "last_seen_at = excluded.last_seen_at, "
                    "seen_count = seen_count + 1",
                    (mid, now, now),
                )
            self._db.commit()

    def get_seen_ids(self, ttl_seconds: float) -> set[str]:
        """Return memory IDs surfaced within the TTL window."""
        if self._db is None:
            return set()
        cutoff = time.time() - ttl_seconds
        rows = self._db.execute(
            "SELECT memory_id FROM seen_memories WHERE last_seen_at >= ?", (cutoff,)
        ).fetchall()
        return {r[0] for r in rows}

    def cleanup_expired(self, ttl_seconds: float) -> int:
        """Delete seen_memories entries older than TTL. Returns count deleted."""
        if self._db is None:
            return 0
        cutoff = time.time() - ttl_seconds
        with self._lock:
            cursor = self._db.execute(
                "DELETE FROM seen_memories WHERE last_seen_at < ?", (cutoff,)
            )
            self._db.commit()
            return cursor.rowcount

    def get_tool_not_relevant_ratio(
        self, tool: str | None, min_samples: int = 20
    ) -> float | None:
        """Return ratio of not_relevant feedback. None if insufficient samples.

        If tool is None, returns the global ratio across all tools (used
        as a cold-start fallback when a specific tool has too few samples).
        """
        if self._db is None:
            return None
        if tool is not None:
            total = self._db.execute(
                "SELECT COUNT(*) FROM surfacing_feedback f "
                "JOIN surfacing_events e ON f.surfacing_id = e.id "
                "WHERE e.tool = ?",
                (tool,),
            ).fetchone()[0]
            if total < min_samples:
                return None
            not_relevant = self._db.execute(
                "SELECT COUNT(*) FROM surfacing_feedback f "
                "JOIN surfacing_events e ON f.surfacing_id = e.id "
                "WHERE e.tool = ? AND f.rating = 'not_relevant'",
                (tool,),
            ).fetchone()[0]
        else:
            total = self._db.execute(
                "SELECT COUNT(*) FROM surfacing_feedback"
            ).fetchone()[0]
            if total < min_samples:
                return None
            not_relevant = self._db.execute(
                "SELECT COUNT(*) FROM surfacing_feedback WHERE rating = 'not_relevant'"
            ).fetchone()[0]
        return not_relevant / total if total > 0 else 0.0
