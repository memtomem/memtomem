"""Search history and query suggestion storage methods."""

from __future__ import annotations

import json
import logging
import sqlite3
import struct
from datetime import datetime, timedelta, timezone

from memtomem.errors import FeedbackConflictError, StorageError

_log = logging.getLogger(__name__)

#: Closed relevance-judgment vocabulary (#1801). Validated here, in one
#: place, so the MCP tool and the Web API emit identical error messages.
FEEDBACK_JUDGMENTS: frozenset[str] = frozenset({"relevant", "not_relevant"})


def _feedback_now() -> datetime:
    return datetime.now(timezone.utc)


def _audit_timestamp(now: datetime) -> str:
    """Feedback audit timestamps use microsecond precision, unlike the
    seconds-precision history rows: a replacement inside the creation
    second must still be distinguishable from the original."""
    return now.isoformat(timespec="microseconds")


def _next_audit_timestamp(prev_iso: str) -> str:
    """A replacement's ``updated_at`` must be strictly greater than the
    previous one, even if the wall clock repeats or steps backward."""
    now = _feedback_now()
    try:
        prev = datetime.fromisoformat(prev_iso)
    except ValueError:
        return _audit_timestamp(now)
    if now <= prev:
        now = prev + timedelta(microseconds=1)
    return _audit_timestamp(now)


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
        """Delete query history rows older than _HISTORY_MAX_AGE_DAYS.

        Dependent ``search_feedback`` rows go with them via the FK
        ``ON DELETE CASCADE`` (the write connection runs with
        ``PRAGMA foreign_keys=ON``), so pruning can never orphan feedback.
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=self._HISTORY_MAX_AGE_DAYS)
        ).isoformat(timespec="seconds")
        db = self._get_db()
        deleted = db.execute("DELETE FROM query_history WHERE created_at < ?", (cutoff,)).rowcount
        # Commit unconditionally: the DELETE opens an implicit transaction
        # even when it matches zero rows, and leaving it open makes the next
        # explicit BEGIN IMMEDIATE (save_search_feedback) fail.
        db.commit()
        if deleted:
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

    # ---- explicit relevance feedback (#1801) ------------------------------

    async def save_search_feedback(
        self, run_id: str, chunk_id: str, judgment: str, *, replace: bool = False
    ) -> dict:
        """Record one relevance judgment for a snapshotted result of one run.

        Idempotent: resubmitting the same judgment is a no-op that leaves
        the audit timestamps untouched. A *different* judgment is rejected
        with :class:`FeedbackConflictError` unless ``replace=True``, in
        which case ``updated_at`` advances (strictly) while ``created_at``
        stays put — the timestamp pair is the replacement audit trail.
        """
        if judgment not in FEEDBACK_JUDGMENTS:
            raise ValueError(
                f"judgment must be one of {sorted(FEEDBACK_JUDGMENTS)}, got {judgment!r}"
            )
        if getattr(self, "_in_transaction", False):
            # transaction() only suppresses commits — it takes no lock, so
            # running here would drop the BEGIN IMMEDIATE serialization
            # this read-modify-write depends on.
            raise StorageError("save_search_feedback cannot run inside a transaction block")
        db = self._get_db()
        # Serialize validate→insert/update across connections and processes
        # (mm web and the MCP server may share one DB).
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute(
                "SELECT result_snapshot_json FROM query_history WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"run_id {run_id!r} not found")
            snapshot_ids = {entry.get("chunk_id") for entry in json.loads(row[0] or "[]")}
            if chunk_id not in snapshot_ids:
                raise ValueError(
                    f"chunk_id {chunk_id!r} was not in the result snapshot for run {run_id!r}"
                )
            existing = db.execute(
                "SELECT judgment, created_at, updated_at FROM search_feedback "
                "WHERE run_id = ? AND chunk_id = ?",
                (run_id, chunk_id),
            ).fetchone()
            if existing is None:
                now = _audit_timestamp(_feedback_now())
                db.execute(
                    "INSERT INTO search_feedback "
                    "(run_id, chunk_id, judgment, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (run_id, chunk_id, judgment, now, now),
                )
                db.commit()
                return self._feedback_row(run_id, chunk_id, judgment, now, now, created=True)
            prev_judgment, created_at, updated_at = existing
            if prev_judgment == judgment:
                db.rollback()  # release the lock; nothing to write
                return self._feedback_row(run_id, chunk_id, judgment, created_at, updated_at)
            if not replace:
                raise FeedbackConflictError(
                    f"feedback for run {run_id!r} chunk {chunk_id!r} is already "
                    f"{prev_judgment!r}; pass replace=true to overwrite"
                )
            new_updated = _next_audit_timestamp(updated_at)
            db.execute(
                "UPDATE search_feedback SET judgment = ?, updated_at = ? "
                "WHERE run_id = ? AND chunk_id = ?",
                (judgment, new_updated, run_id, chunk_id),
            )
            db.commit()
            return self._feedback_row(
                run_id, chunk_id, judgment, created_at, new_updated, replaced=True
            )
        except sqlite3.IntegrityError as exc:
            # BEGIN IMMEDIATE makes these unreachable in practice; classify
            # anyway so a constraint hit never leaks as an internal error.
            db.rollback()
            return self._classify_feedback_integrity_error(
                db, exc, run_id, chunk_id, judgment, replace=replace
            )
        except Exception:
            db.rollback()
            raise

    def _classify_feedback_integrity_error(
        self,
        db,
        exc: sqlite3.IntegrityError,
        run_id: str,
        chunk_id: str,
        judgment: str,
        *,
        replace: bool = False,
    ) -> dict:
        """Map a constraint hit on the feedback write to its domain meaning.

        FK violation → the run was pruned between validation and insert
        (KeyError, same as an unknown run). Unique violation → a concurrent
        writer landed this (run_id, chunk_id) first: re-read and classify
        exactly like the existing-row branch. Same judgment is an idempotent
        success; a different judgment honors the caller's ``replace`` intent
        — replacing (timestamp-audited) when set, else raising a conflict.
        Anything else stays a StorageError.
        """
        message = str(exc).upper()
        if "FOREIGN KEY" in message:
            raise KeyError(f"run_id {run_id!r} not found") from exc
        if "UNIQUE" in message:
            landed = db.execute(
                "SELECT judgment, created_at, updated_at FROM search_feedback "
                "WHERE run_id = ? AND chunk_id = ?",
                (run_id, chunk_id),
            ).fetchone()
            if landed is not None:
                prev_judgment, created_at, updated_at = landed
                if prev_judgment == judgment:
                    return self._feedback_row(run_id, chunk_id, judgment, created_at, updated_at)
                if not replace:
                    raise FeedbackConflictError(
                        f"feedback for run {run_id!r} chunk {chunk_id!r} is already "
                        f"{prev_judgment!r}; pass replace=true to overwrite"
                    ) from exc
                new_updated = _next_audit_timestamp(updated_at)
                db.execute(
                    "UPDATE search_feedback SET judgment = ?, updated_at = ? "
                    "WHERE run_id = ? AND chunk_id = ?",
                    (judgment, new_updated, run_id, chunk_id),
                )
                db.commit()
                return self._feedback_row(
                    run_id, chunk_id, judgment, created_at, new_updated, replaced=True
                )
        raise StorageError(f"feedback write failed: {exc}") from exc

    @staticmethod
    def _feedback_row(
        run_id: str,
        chunk_id: str,
        judgment: str,
        created_at: str,
        updated_at: str,
        *,
        created: bool = False,
        replaced: bool = False,
    ) -> dict:
        return {
            "run_id": run_id,
            "chunk_id": chunk_id,
            "judgment": judgment,
            "created_at": created_at,
            "updated_at": updated_at,
            "created": created,
            "replaced": replaced,
        }

    def _require_search_run(self, run_id: str) -> tuple:
        row = (
            self._get_db()
            .execute(
                "SELECT run_id, query_text, created_at, observation_json, result_snapshot_json "
                "FROM query_history WHERE run_id = ?",
                (run_id,),
            )
            .fetchone()
        )
        if row is None:
            raise KeyError(f"run_id {run_id!r} not found")
        return row

    async def get_search_feedback(self, run_id: str) -> list[dict]:
        """Current judgments for one observed run, ordered by chunk_id."""
        self._require_search_run(run_id)
        rows = (
            self._get_db()
            .execute(
                "SELECT chunk_id, judgment, created_at, updated_at FROM search_feedback "
                "WHERE run_id = ? ORDER BY chunk_id",
                (run_id,),
            )
            .fetchall()
        )
        return [
            {"chunk_id": r[0], "judgment": r[1], "created_at": r[2], "updated_at": r[3]}
            for r in rows
        ]

    async def get_search_run(self, run_id: str) -> dict:
        """One observed run: query, observation metadata, ranked snapshot."""
        row = self._require_search_run(run_id)
        return {
            "run_id": row[0],
            "query_text": row[1],
            "created_at": row[2],
            "observation": json.loads(row[3]) if row[3] else {},
            "result_snapshot": json.loads(row[4]) if row[4] else [],
        }

    async def get_search_runs(self, limit: int = 50, since: str | None = None) -> list[dict]:
        """Lightweight, newest-first summaries of observed runs.

        Legacy history rows (``run_id IS NULL``) are excluded — they have
        no snapshot to judge. The embedding blob and full snapshot are
        deliberately not returned; use :meth:`get_search_run` for detail.
        """
        if not 1 <= limit <= 200:
            raise ValueError(f"limit must be between 1 and 200, got {limit}")
        params: list = []
        query = (
            "SELECT h.run_id, h.query_text, h.created_at, h.result_snapshot_json, "
            "h.observation_json, "
            "(SELECT COUNT(*) FROM search_feedback f WHERE f.run_id = h.run_id) "
            "FROM query_history h WHERE h.run_id IS NOT NULL"
        )
        if since:
            try:
                since_dt = datetime.fromisoformat(since)
            except ValueError:
                raise ValueError(f"since must be an ISO-8601 timestamp, got {since!r}") from None
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=timezone.utc)
            query += " AND h.created_at >= ?"
            params.append(since_dt.astimezone(timezone.utc).isoformat(timespec="seconds"))
        query += " ORDER BY h.created_at DESC, h.id DESC LIMIT ?"
        params.append(limit)
        rows = self._get_db().execute(query, params).fetchall()
        return [
            {
                "run_id": r[0],
                "query_text": r[1],
                "created_at": r[2],
                "result_count": len(json.loads(r[3])) if r[3] else 0,
                "origin": (json.loads(r[4]) or {}).get("origin") if r[4] else None,
                "feedback_count": r[5],
            }
            for r in rows
        ]
