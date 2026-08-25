"""Maintenance run log mixin — append-only record of applied maintenance (#2132).

One row per *applied* (non-dry-run) maintenance run. Writers call
``maintenance_run_start`` before the mutation and ``maintenance_run_finish``
after it, each in its own transaction: the handlers own their own commits, so
folding the audit row into the mutation's transaction would mean restructuring
every policy handler. The cost of that choice is visible rather than silent — a
crash between the mutation's commit and the finish leaves a ``running`` row with
no ``completed_at``, which is exactly the evidence an interrupted run should
leave behind. Writing the row only after the mutation would lose the record.

The recorded chunk ids and namespace names are provenance, not live references:
there is no FK to ``chunks``, and namespace merges do not rewrite rows here.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

# Rows older than this are dropped lazily on the next run start. Matches
# HistoryMixin's retention window.
_MAINTENANCE_RUN_MAX_AGE_DAYS = 90

_FINAL_STATUSES = frozenset({"ok", "error"})

# One static statement, no SQL assembled at call time: a NULL bind means "no
# filter" for that column, so the four filter combinations share one query and
# nothing here can grow into an injection vector as the filters change.
_LATEST_SQL = """
    SELECT id, kind, policy_name, source, status, started_at, completed_at,
           affected_count, namespaces_json, summary_json, error
    FROM maintenance_runs
    WHERE (? IS NULL OR kind = ?)
      AND (? IS NULL OR policy_name = ?)
    ORDER BY id DESC
    LIMIT ?
"""


class MaintenanceRunMixin:
    """Mixin providing the maintenance run log. Requires self._get_db()."""

    async def maintenance_run_start(
        self,
        kind: str,
        *,
        policy_name: str | None = None,
        source: str = "mcp",
    ) -> int:
        """Open a run row and return its id.

        Args:
            kind: Policy type (``auto_expire`` …) or ``consolidate_apply``.
            policy_name: Owning policy, when the run came from one.
            source: ``scheduler`` for unattended runs, ``mcp`` for tool calls.
        """
        db = self._get_db()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._rolls_back_if_standalone(db):
            # The prune is a DELETE, so it has to be inside the protected
            # region: a failure in the INSERT below must not strand it.
            self._prune_maintenance_runs(db)
            cur = db.execute(
                "INSERT INTO maintenance_runs "
                "(kind, policy_name, source, status, started_at) VALUES (?, ?, ?, 'running', ?)",
                (kind, policy_name, source, now),
            )
            # The backend's ``transaction()`` owner suppresses inner commits; an
            # audit write must not end a caller's transaction early.
            self._commit_if_standalone(db)
        return int(cur.lastrowid or 0)

    async def maintenance_run_finish(
        self,
        run_id: int,
        *,
        status: str,
        affected_count: int = 0,
        namespaces: Iterable[str] = (),
        summary: dict | None = None,
        error: str | None = None,
    ) -> None:
        """Finalize a run row opened by ``maintenance_run_start``."""
        if status not in _FINAL_STATUSES:
            raise ValueError(f"status must be one of {sorted(_FINAL_STATUSES)}, got {status!r}")
        db = self._get_db()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        unique_ns = sorted({ns for ns in namespaces if ns})
        with self._rolls_back_if_standalone(db):
            db.execute(
                "UPDATE maintenance_runs SET status = ?, completed_at = ?, affected_count = ?, "
                "namespaces_json = ?, summary_json = ?, error = ? WHERE id = ?",
                (
                    status,
                    now,
                    affected_count,
                    json.dumps(unique_ns, ensure_ascii=False),
                    json.dumps(summary or {}, ensure_ascii=False, sort_keys=True, default=str),
                    error,
                    run_id,
                ),
            )
            self._commit_if_standalone(db)

    async def maintenance_run_latest(
        self,
        *,
        kind: str | None = None,
        policy_name: str | None = None,
        limit: int = 1,
    ) -> list[dict]:
        """Return the most recent runs, newest first."""
        if limit <= 0:
            return []
        db = self._get_read_db()
        rows = db.execute(_LATEST_SQL, (kind, kind, policy_name, policy_name, limit)).fetchall()
        return [
            {
                "id": r[0],
                "kind": r[1],
                "policy_name": r[2],
                "source": r[3],
                "status": r[4],
                "started_at": r[5],
                "completed_at": r[6],
                "affected_count": r[7],
                "namespaces": json.loads(r[8]) if r[8] else [],
                "summary": json.loads(r[9]) if r[9] else {},
                "error": r[10],
            }
            for r in rows
        ]

    def _prune_maintenance_runs(self, db: sqlite3.Connection) -> None:
        """Drop rows older than the retention window (lazy, on write)."""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=_MAINTENANCE_RUN_MAX_AGE_DAYS)
        ).isoformat(timespec="seconds")
        db.execute("DELETE FROM maintenance_runs WHERE started_at < ?", (cutoff,))
