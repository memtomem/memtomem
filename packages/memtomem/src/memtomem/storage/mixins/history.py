"""Search history and query suggestion storage methods."""

from __future__ import annotations

import json
import logging
import struct
from datetime import datetime, timedelta, timezone

_log = logging.getLogger(__name__)


class HistoryMixin:
    """Mixin providing search history methods. Requires self._get_db()."""

    _history_save_count: int = 0
    _HISTORY_PRUNE_INTERVAL: int = 100
    _HISTORY_MAX_AGE_DAYS: int = 90

    async def save_query_history(
        self,
        query_text: str,
        query_embedding: list[float],
        result_chunk_ids: list[str],
        result_scores: list[float],
    ) -> None:
        db = self._get_db()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        emb_blob = (
            struct.pack(f"{len(query_embedding)}f", *query_embedding) if query_embedding else b""
        )
        db.execute(
            "INSERT INTO query_history (query_text, query_embedding, result_chunk_ids, result_scores, created_at) VALUES (?, ?, ?, ?, ?)",
            (query_text, emb_blob, json.dumps(result_chunk_ids), json.dumps(result_scores), now),
        )
        db.commit()

        # Periodic pruning of old entries
        self._history_save_count += 1
        if self._history_save_count % self._HISTORY_PRUNE_INTERVAL == 0:
            self._prune_old_history()

    async def save_search_observation(
        self,
        query_text: str,
        query_embedding: list[float],
        result_chunk_ids: list[str],
        result_scores: list[float],
        *,
        run_id: str,
        observation: dict,
        result_snapshot: list[dict],
    ) -> str:
        """Persist one ranked-search invocation and return its durable run ID.

        This is intentionally separate from ``save_query_history`` so storage
        backends that only implement the legacy history contract remain usable.
        The pipeline advertises a run ID only after this local commit succeeds.
        """
        db = self._get_db()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        emb_blob = (
            struct.pack(f"{len(query_embedding)}f", *query_embedding) if query_embedding else b""
        )
        db.execute(
            """INSERT INTO query_history
               (query_text, query_embedding, result_chunk_ids, result_scores,
                run_id, observation_json, result_snapshot_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                query_text,
                emb_blob,
                json.dumps(result_chunk_ids),
                json.dumps(result_scores),
                run_id,
                json.dumps(observation, ensure_ascii=False, sort_keys=True),
                json.dumps(result_snapshot, ensure_ascii=False),
                now,
            ),
        )
        db.commit()
        self._history_save_count += 1
        if self._history_save_count % self._HISTORY_PRUNE_INTERVAL == 0:
            self._prune_old_history()
        return run_id

    def _prune_old_history(self) -> None:
        """Delete query history rows older than _HISTORY_MAX_AGE_DAYS."""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=self._HISTORY_MAX_AGE_DAYS)
        ).isoformat(timespec="seconds")
        db = self._get_db()
        deleted = db.execute("DELETE FROM query_history WHERE created_at < ?", (cutoff,)).rowcount
        if deleted:
            db.commit()
            _log.info(
                "Pruned %d old query_history rows (>%d days)", deleted, self._HISTORY_MAX_AGE_DAYS
            )

    async def get_query_history(self, limit: int = 20, since: str | None = None) -> list[dict]:
        db = self._get_db()
        query = (
            "SELECT query_text, result_chunk_ids, result_scores, created_at, "
            "run_id, observation_json, result_snapshot_json FROM query_history"
        )
        params: list = []
        if since:
            query += " WHERE created_at >= ?"
            params.append(since)
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        rows = db.execute(query, params).fetchall()
        return [
            {
                "query_text": r[0],
                "result_chunk_ids": json.loads(r[1]) if r[1] else [],
                "result_scores": json.loads(r[2]) if r[2] else [],
                "created_at": r[3],
                "run_id": r[4],
                "observation": json.loads(r[5]) if r[5] else {},
                "result_snapshot": json.loads(r[6]) if r[6] else [],
            }
            for r in rows
        ]

    async def suggest_queries(self, prefix: str, limit: int = 5) -> list[str]:
        db = self._get_db()
        rows = db.execute(
            "SELECT query_text, MAX(created_at) as latest FROM query_history WHERE query_text LIKE ? GROUP BY query_text ORDER BY latest DESC LIMIT ?",
            (f"{prefix}%", limit),
        ).fetchall()
        return [r[0] for r in rows]
