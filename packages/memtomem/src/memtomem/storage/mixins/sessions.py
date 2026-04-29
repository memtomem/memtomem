"""Session (episodic memory) storage methods."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone


class SessionMixin:
    """Mixin providing session lifecycle methods. Requires self._get_db()."""

    async def create_session(
        self, session_id: str, agent_id: str, namespace: str, metadata: dict | None = None
    ) -> None:
        db = self._get_db()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        meta_json = json.dumps(metadata) if metadata else "{}"
        try:
            db.execute(
                "INSERT INTO sessions (id, agent_id, started_at, namespace, metadata)"
                " VALUES (?, ?, ?, ?, ?)",
                (session_id, agent_id, now, namespace, meta_json),
            )
            db.commit()
        except Exception as exc:
            if "UNIQUE constraint" in str(exc):
                pass  # duplicate session ID — expected
            else:
                raise

    async def end_session(self, session_id: str, summary: str | None, metadata: dict) -> None:
        db = self._get_db()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db.execute(
            "UPDATE sessions SET ended_at = ?, summary = ?, metadata = ? WHERE id = ?",
            (now, summary, json.dumps(metadata), session_id),
        )
        db.commit()

    async def add_session_event(
        self,
        session_id: str,
        event_type: str,
        content: str,
        chunk_ids: list[str] | None = None,
        metadata: dict | None = None,
    ) -> None:
        db = self._get_db()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        meta_json = json.dumps(metadata) if metadata else "{}"
        db.execute(
            "INSERT INTO session_events (session_id, event_type, content, chunk_ids, created_at, metadata)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, event_type, content, json.dumps(chunk_ids or []), now, meta_json),
        )
        db.commit()

    async def list_sessions(
        self,
        agent_id: str | None = None,
        since: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        db = self._get_db()
        query = (
            "SELECT id, agent_id, started_at, ended_at, summary, namespace, metadata FROM sessions"
        )
        params: list = []
        conditions: list[str] = []
        if agent_id:
            conditions.append("agent_id = ?")
            params.append(agent_id)
        if since:
            conditions.append("started_at >= ?")
            params.append(since)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)
        rows = db.execute(query, params).fetchall()
        return [
            {
                "id": r[0],
                "agent_id": r[1],
                "started_at": r[2],
                "ended_at": r[3],
                "summary": r[4],
                "namespace": r[5],
                "metadata": r[6],
            }
            for r in rows
        ]

    async def get_session(self, session_id: str) -> dict | None:
        """Return a single session row by id, or ``None`` if not found.

        Added for the Phase B auto-summary path which needs the
        session's ``started_at`` and ``namespace`` to scope the
        recall_chunks lookup. Mirrors the column shape returned by
        ``list_sessions``.
        """
        db = self._get_db()
        row = db.execute(
            "SELECT id, agent_id, started_at, ended_at, summary, namespace, metadata"
            " FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "agent_id": row[1],
            "started_at": row[2],
            "ended_at": row[3],
            "summary": row[4],
            "namespace": row[5],
            "metadata": row[6],
        }

    async def find_stale_active_sessions(self, started_before: str) -> list[dict]:
        """Return active sessions (``ended_at IS NULL``) whose ``started_at``
        is strictly less than the ISO-8601 cutoff.

        Backs ``mm session start --auto-end-stale``: SessionStart hooks call
        this to enumerate orphaned sessions left over from previous Claude
        Code processes that crashed before Stop fired. Caller passes each ID
        to ``end_session`` with an auto-cleanup summary.
        """
        db = self._get_db()
        rows = db.execute(
            "SELECT id, agent_id, started_at, ended_at, summary, namespace, metadata"
            " FROM sessions WHERE ended_at IS NULL AND started_at < ?"
            " ORDER BY started_at ASC",
            (started_before,),
        ).fetchall()
        return [
            {
                "id": r[0],
                "agent_id": r[1],
                "started_at": r[2],
                "ended_at": r[3],
                "summary": r[4],
                "namespace": r[5],
                "metadata": r[6],
            }
            for r in rows
        ]

    async def get_session_events(self, session_id: str) -> list[dict]:
        db = self._get_db()
        rows = db.execute(
            "SELECT event_type, content, chunk_ids, created_at, metadata"
            " FROM session_events WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
        return [
            {
                "event_type": r[0],
                "content": r[1],
                "chunk_ids": json.loads(r[2]),
                "created_at": r[3],
                "metadata": json.loads(r[4]) if r[4] else {},
            }
            for r in rows
        ]

    async def cleanup_old_sessions(self, max_age_days: int = 90) -> int:
        """Delete ended sessions older than max_age_days.

        Session events are cleaned up via ON DELETE CASCADE.
        Only deletes sessions where ended_at is not NULL (completed sessions).
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat(
            timespec="seconds"
        )
        db = self._get_db()
        cursor = db.execute(
            "DELETE FROM sessions WHERE ended_at IS NOT NULL AND ended_at < ?",
            (cutoff,),
        )
        if cursor.rowcount:
            db.commit()
        return cursor.rowcount
