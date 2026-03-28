"""Working memory (scratchpad) storage methods."""

from __future__ import annotations

from datetime import datetime, timezone


class ScratchMixin:
    """Mixin providing scratchpad methods. Requires self._get_db()."""

    async def scratch_set(
        self,
        key: str,
        value: str,
        session_id: str | None = None,
        expires_at: str | None = None,
    ) -> None:
        db = self._get_db()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db.execute(
            "INSERT OR REPLACE INTO working_memory (key, value, session_id, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
            (key, value, session_id, now, expires_at),
        )
        db.commit()

    async def scratch_get(self, key: str) -> dict | None:
        db = self._get_db()
        row = db.execute(
            "SELECT key, value, session_id, created_at, expires_at, promoted FROM working_memory WHERE key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        return {
            "key": row[0],
            "value": row[1],
            "session_id": row[2],
            "created_at": row[3],
            "expires_at": row[4],
            "promoted": bool(row[5]),
        }

    async def scratch_list(self, session_id: str | None = None) -> list[dict]:
        db = self._get_db()
        if session_id:
            rows = db.execute(
                "SELECT key, value, session_id, created_at, expires_at, promoted FROM working_memory WHERE session_id = ? ORDER BY created_at",
                (session_id,),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT key, value, session_id, created_at, expires_at, promoted FROM working_memory ORDER BY created_at",
            ).fetchall()
        return [
            {
                "key": r[0],
                "value": r[1],
                "session_id": r[2],
                "created_at": r[3],
                "expires_at": r[4],
                "promoted": bool(r[5]),
            }
            for r in rows
        ]

    async def scratch_delete(self, key: str) -> bool:
        db = self._get_db()
        cursor = db.execute("DELETE FROM working_memory WHERE key = ?", (key,))
        db.commit()
        return cursor.rowcount > 0

    async def scratch_cleanup(self, session_id: str | None = None) -> int:
        """Remove expired or session-bound entries (keep promoted ones)."""
        db = self._get_db()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        total = 0
        c1 = db.execute(
            "DELETE FROM working_memory WHERE expires_at IS NOT NULL AND expires_at < ? AND promoted = 0",
            (now,),
        )
        total += c1.rowcount
        if session_id:
            c2 = db.execute(
                "DELETE FROM working_memory WHERE session_id = ? AND promoted = 0", (session_id,)
            )
            total += c2.rowcount
        if total:
            db.commit()
        return total

    async def scratch_promote(self, key: str) -> bool:
        """Mark a working memory entry as promoted."""
        db = self._get_db()
        cursor = db.execute("UPDATE working_memory SET promoted = 1 WHERE key = ?", (key,))
        db.commit()
        return cursor.rowcount > 0
