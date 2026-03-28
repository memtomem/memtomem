"""Search history and query suggestion storage methods."""

from __future__ import annotations

import json
import struct
from datetime import datetime, timezone


class HistoryMixin:
    """Mixin providing search history methods. Requires self._get_db()."""

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

    async def get_query_history(self, limit: int = 20, since: str | None = None) -> list[dict]:
        db = self._get_db()
        query = "SELECT query_text, result_chunk_ids, result_scores, created_at FROM query_history"
        params: list = []
        if since:
            query += " WHERE created_at >= ?"
            params.append(since)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = db.execute(query, params).fetchall()
        return [
            {
                "query_text": r[0],
                "result_chunk_ids": json.loads(r[1]) if r[1] else [],
                "result_scores": json.loads(r[2]) if r[2] else [],
                "created_at": r[3],
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
