"""Session (episodic memory) storage methods."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


def decode_session_metadata(raw: object) -> dict:
    """Normalize a stored ``sessions.metadata`` value to a dict.

    The column holds a JSON document written by ``json.dumps``, but a row
    can predate a schema expectation, be ``NULL``, or have been edited by
    hand into valid-but-wrong-shape JSON (``[]``, ``"text"``, ``42``,
    ``null``). Every one of those decodes without error yet has no
    ``.get`` — callers that read a key off the result would raise on data
    they are supposed to tolerate. Return ``{}`` for all of them so a bad
    row degrades to "no metadata" instead of breaking the session path.

    Diagnostics never include the value itself. Session metadata is
    arbitrary caller-supplied data and can carry secret-shaped strings;
    the repo's rule is that matched bytes do not reach logs. Type and
    length are enough to tell an operator which row to look at.
    """
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, (str, bytes, bytearray)):
        logger.warning("session_metadata_unexpected_column_type type=%s", type(raw).__name__)
        return {}
    try:
        decoded = json.loads(raw)
    except ValueError:
        logger.warning("session_metadata_malformed type=%s len=%d", type(raw).__name__, len(raw))
        return {}
    if not isinstance(decoded, dict):
        logger.warning("session_metadata_not_an_object type=%s", type(decoded).__name__)
        return {}
    return decoded


class SessionMixin:
    """Mixin providing session lifecycle methods. Requires self._get_db()."""

    async def create_session(
        self, session_id: str, agent_id: str, namespace: str, metadata: dict | None = None
    ) -> None:
        db = self._get_db()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        meta_json = json.dumps(metadata) if metadata else "{}"
        try:
            # ON CONFLICT(id) DO NOTHING: ignore ONLY the id collision (an
            # idempotent retry of a caller-minted uuid4) inside the statement
            # itself; any other integrity error still raises. The previous
            # ``except Exception`` + bare "UNIQUE constraint" substring both
            # masked every future UNIQUE surface (#1574 item 5) and left the
            # failed INSERT's transaction open on the shared writer
            # connection — the next writer hit "database is locked".
            db.execute(
                "INSERT INTO sessions (id, agent_id, started_at, namespace, metadata)"
                " VALUES (?, ?, ?, ?, ?) ON CONFLICT(id) DO NOTHING",
                (session_id, agent_id, now, namespace, meta_json),
            )
            if not self._in_transaction:
                db.commit()
        except Exception:
            # Close the failed transaction instead of leaving it to be
            # flushed by the next unrelated commit (#1572 idiom).
            if not self._in_transaction:
                db.rollback()
            raise

    async def end_session(self, session_id: str, summary: str | None, metadata: dict) -> None:
        """Close a session, **merging** ``metadata`` into the stored document.

        The merge is shallow: top-level keys in ``metadata`` replace their
        stored counterparts and every other key survives. Ending a session
        used to overwrite the whole document with just ``event_counts``,
        silently discarding the ``title`` recorded at session start — and
        any other key a caller had put there.

        Shallow is the right depth, not a compromise. SQLite's
        ``json_patch`` implements JSON Merge Patch, which recurses into
        nested objects: patching a stored
        ``{"event_counts": {"query": 2, "add": 4}}`` with
        ``{"event_counts": {"query": 1}}`` yields
        ``{"query": 1, "add": 4}`` — resurrecting an event type the new
        snapshot says is gone. ``event_counts`` is a complete snapshot and
        must replace wholesale. ``json_patch`` additionally deletes keys
        on a ``null`` value (so a null could never be persisted) and
        raises on a row whose stored JSON is malformed, which
        :func:`decode_session_metadata` deliberately tolerates.

        Merging makes this a read-modify-write, so the read and the write
        have to be one atomic unit: without that, a concurrent writer
        landing between them has its keys silently reverted by this stale
        merge. When this method owns the transaction it takes
        ``BEGIN IMMEDIATE`` before the SELECT — the same reason
        :func:`memtomem.storage.orphan_gc.sweep_project_root` does. When
        the caller already opened one (``self._in_transaction``), the
        enclosing transaction supplies the atomicity and this method must
        neither begin nor commit: committing here would prematurely flush
        the caller's earlier work and put it beyond rollback.
        """
        db = self._get_db()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        owns_transaction = not self._in_transaction
        if owns_transaction:
            db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute("SELECT metadata FROM sessions WHERE id = ?", (session_id,)).fetchone()
            merged = {**decode_session_metadata(row[0] if row else None), **metadata}
            db.execute(
                "UPDATE sessions SET ended_at = ?, summary = ?, metadata = ? WHERE id = ?",
                (now, summary, json.dumps(merged), session_id),
            )
            if owns_transaction:
                db.execute("COMMIT")
        except Exception:
            # Close the failed transaction rather than leaving it open for
            # the next unrelated commit to flush (the #1572 idiom, same as
            # ``create_session`` above).
            if owns_transaction:
                db.rollback()
            raise

    async def add_session_event(
        self,
        session_id: str,
        event_type: str,
        content: str,
        chunk_ids: list[str] | None = None,
        metadata: dict | None = None,
    ) -> None:
        """Append an event to a session's log.

        Honors an enclosing transaction and closes its own on failure —
        the contract every sibling write in this mixin already has
        (``create_session``, ``end_session``). It was the lone exception:
        it committed unconditionally, so a caller who opened a
        transaction had its earlier work flushed here and put beyond
        rollback, and a failing INSERT left the transaction open on the
        shared writer connection for the next unrelated commit to flush
        (the #1572 idiom).

        The rollback arm is what makes the caller's failure handling
        viable: a caller that reacts to a failed event write by recording
        the failure elsewhere needs the connection usable afterwards. A
        left-open transaction would take that fallback down with the
        primary write.
        """
        db = self._get_db()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        meta_json = json.dumps(metadata) if metadata else "{}"
        try:
            db.execute(
                "INSERT INTO session_events"
                " (session_id, event_type, content, chunk_ids, created_at, metadata)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, event_type, content, json.dumps(chunk_ids or []), now, meta_json),
            )
            if not self._in_transaction:
                db.commit()
        except Exception:
            if not self._in_transaction:
                db.rollback()
            raise

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

        ``metadata`` comes back **decoded to a dict**, matching
        ``get_session_events`` rather than the raw JSON string this used
        to return; see :func:`decode_session_metadata` for how bad rows
        degrade.
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
            "metadata": decode_session_metadata(row[6]),
        }

    async def find_stale_active_sessions(
        self, started_before: str, *, limit: int = 100
    ) -> list[dict]:
        """Return active sessions (``ended_at IS NULL``) whose ``started_at``
        is strictly less than the ISO-8601 cutoff, oldest-first, up to
        ``limit`` rows.

        Backs ``mm session start --auto-end-stale``: SessionStart hooks call
        this to enumerate orphaned sessions left over from previous Claude
        Code processes that crashed before Stop fired. Caller passes each ID
        to ``end_session`` with an auto-cleanup summary.

        Both ``started_before`` and the rows' ``started_at`` are compared as
        ISO-8601 strings — relies on the format ``create_session`` writes
        (``isoformat(timespec="seconds")`` on a tz-aware UTC datetime, e.g.
        ``2026-04-29T04:39:35+00:00``). Mixing tz-naive and tz-aware values
        breaks lexicographic ordering because ``+`` (0x2B) sorts before
        digits — keep both sides in the same format.

        ``limit`` caps a single SessionStart hook's blocking work: at
        100/fire, a 1000-orphan backlog drains over ~10 invocations rather
        than stalling boot for minutes synchronously. Caller should warn
        when the result count hits the limit so users know more remain.
        """
        db = self._get_db()
        rows = db.execute(
            "SELECT id, agent_id, started_at, ended_at, summary, namespace, metadata"
            " FROM sessions WHERE ended_at IS NULL AND started_at < ?"
            " ORDER BY started_at ASC LIMIT ?",
            (started_before, limit),
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
            "SELECT id, event_type, content, chunk_ids, created_at, metadata"
            " FROM session_events WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
        return [
            {
                "id": r[0],
                "event_type": r[1],
                "content": r[2],
                "chunk_ids": json.loads(r[3]),
                "created_at": r[4],
                "metadata": json.loads(r[5]) if r[5] else {},
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
