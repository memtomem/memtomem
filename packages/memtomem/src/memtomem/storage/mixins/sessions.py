"""Session (episodic memory) storage methods."""

from __future__ import annotations

import json
from datetime import datetime, timezone


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
    ) -> None:
        db = self._get_db()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db.execute(
            "INSERT INTO session_events (session_id, event_type, content, chunk_ids, created_at) VALUES (?, ?, ?, ?, ?)",
            (session_id, event_type, content, json.dumps(chunk_ids or []), now),
        )
        db.commit()

    async def list_sessions(
        self,
        agent_id: str | None = None,
        since: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        db = self._get_db()
        query = "SELECT id, agent_id, started_at, ended_at, summary, namespace FROM sessions"
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
            }
            for r in rows
        ]

    async def get_session_events(self, session_id: str) -> list[dict]:
        db = self._get_db()
        rows = db.execute(
            "SELECT event_type, content, chunk_ids, created_at FROM session_events WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
        return [
            {"event_type": r[0], "content": r[1], "chunk_ids": json.loads(r[2]), "created_at": r[3]}
            for r in rows
        ]
