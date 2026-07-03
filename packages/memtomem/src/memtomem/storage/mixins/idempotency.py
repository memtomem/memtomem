"""Idempotency-ledger storage methods (issue #1573).

Records ``(tool, key) -> original result string`` for keyed memory writes so
a transport retry of a lost response returns the stored result instead of
performing a second write.

Two-phase, so a *concurrent* same-key call to a different target can't slip
past the file-lock recheck and double-write (Codex review, #1573):

1. ``idempotency_claim`` atomically inserts a *pending* row (NULL result). The
   first caller to insert wins and proceeds to write; a later concurrent caller
   sees the row and gets ``"pending"`` (in flight) or ``"completed"`` (replay).
   The claim is global on ``(tool, key)`` — a DB row, not a file lock — so it
   serializes same-key callers regardless of which file they target.
2. ``idempotency_complete`` fills in the result after the durable write.
   ``idempotency_release`` deletes the pending row if the write fails, keeping
   the key re-runnable.

``idempotency_get`` is a side-effect-free read of a *completed* row, used on the
fast replay path so a sequential retry short-circuits before re-running the
write's redaction gates. Rows expire after ``IDEMPOTENCY_TTL_S`` (a crash
between claim and complete leaves a pending row that the TTL purge reclaims);
purge is lazy, mirroring the ``working_memory`` cleanup in :class:`ScratchMixin`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# A transport retry lands within seconds–minutes, but an MCP client
# reconnect / session-resume can replay a much older call. 24h covers any
# realistic replay horizon while bounding table growth (result strings are a
# few KB at most).
IDEMPOTENCY_TTL_S = 24 * 60 * 60


class IdempotencyMixin:
    """Mixin providing idempotency-ledger methods. Requires ``self._get_db()``."""

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _purge_expired(self, db) -> None:
        db.execute("DELETE FROM idempotency_ledger WHERE expires_at < ?", (self._now_iso(),))

    async def idempotency_get(self, tool: str, key: str) -> str | None:
        """Return the stored result for a *completed*, unexpired ``(tool, key)``.

        Side-effect-free (no claim). A pending row (NULL result) or an expired
        row is a miss, so a stale key never suppresses a legitimate fresh write.
        """
        db = self._get_db()
        row = db.execute(
            "SELECT result FROM idempotency_ledger "
            "WHERE tool = ? AND key = ? AND result IS NOT NULL AND expires_at > ?",
            (tool, key, self._now_iso()),
        ).fetchone()
        return row[0] if row is not None else None

    async def idempotency_claim(
        self, tool: str, key: str, ttl_s: int = IDEMPOTENCY_TTL_S
    ) -> tuple[str, str | None]:
        """Atomically claim ``(tool, key)`` before a write.

        Returns one of:

        * ``("won", None)`` — we inserted the pending row; the caller owns the
          write and must follow up with ``idempotency_complete`` (or
          ``idempotency_release`` on failure).
        * ``("completed", result)`` — a prior call already finished; replay.
        * ``("pending", None)`` — another call with this key is mid-write;
          the caller must not write (return a retryable error).

        ``INSERT OR IGNORE`` is the atomic test-and-set: exactly one concurrent
        caller inserts the row, the rest fall through to the SELECT.
        """
        db = self._get_db()
        self._purge_expired(db)
        now = self._now_iso()
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl_s)).isoformat(
            timespec="seconds"
        )
        cur = db.execute(
            "INSERT OR IGNORE INTO idempotency_ledger "
            "(tool, key, result, created_at, expires_at) VALUES (?, ?, NULL, ?, ?)",
            (tool, key, now, expires_at),
        )
        db.commit()
        if cur.rowcount == 1:
            return ("won", None)
        row = db.execute(
            "SELECT result FROM idempotency_ledger WHERE tool = ? AND key = ?",
            (tool, key),
        ).fetchone()
        if row is not None and row[0] is not None:
            return ("completed", row[0])
        return ("pending", None)

    async def idempotency_complete(
        self, tool: str, key: str, result: str, ttl_s: int = IDEMPOTENCY_TTL_S
    ) -> None:
        """Fill in the result for a won claim after the durable write.

        TTL is measured from completion so a replay horizon starts once the
        write actually landed.
        """
        db = self._get_db()
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl_s)).isoformat(
            timespec="seconds"
        )
        db.execute(
            "UPDATE idempotency_ledger SET result = ?, expires_at = ? WHERE tool = ? AND key = ?",
            (result, expires_at, tool, key),
        )
        db.commit()

    async def idempotency_release(self, tool: str, key: str) -> None:
        """Delete a *pending* claim so a failed write stays re-runnable.

        Scoped to ``result IS NULL`` so it never removes an already-completed
        row (a late failure after another path recorded success).
        """
        db = self._get_db()
        db.execute(
            "DELETE FROM idempotency_ledger WHERE tool = ? AND key = ? AND result IS NULL",
            (tool, key),
        )
        db.commit()
