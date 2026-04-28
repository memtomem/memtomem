"""Schedule storage mixin — CRUD for the ``schedules`` table (P2 Phase A).

Phase A is direct-cron only (no NL parser). Cron expressions are
interpreted in UTC; ``created_at`` and ``last_run_at`` are stored as
UTC ISO strings. ``schedule_list_due`` returns at-most-once catch-up
semantics — if multiple cron slots elapsed since ``last_run_at``, the
schedule fires exactly once on the next dispatcher tick (no backfill).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from croniter import CroniterBadCronError, CroniterBadDateError, croniter

logger = logging.getLogger(__name__)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_iso_utc(value: str) -> datetime:
    """Parse an ISO timestamp; assume UTC if naive."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class ScheduleMixin:
    """Mixin providing scheduled-job CRUD. Requires self._get_db()."""

    async def schedule_insert(
        self,
        cron_expr: str,
        job_kind: str,
        params: dict[str, Any] | None = None,
    ) -> str:
        """Insert a new schedule and return its id.

        ``cron_expr`` must be a valid 5-field cron expression (validated
        by callers via ``croniter.is_valid`` before reaching here);
        ``job_kind`` must be a registered ``JOB_KINDS`` key (validated
        by callers).
        """
        db = self._get_db()
        sched_id = uuid4().hex[:12]
        db.execute(
            "INSERT INTO schedules "
            "(id, cron_expr, job_kind, params_json, enabled, created_at) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            (
                sched_id,
                cron_expr,
                job_kind,
                json.dumps(params or {}),
                _utcnow_iso(),
            ),
        )
        db.commit()
        return sched_id

    async def schedule_get(self, sched_id: str) -> dict | None:
        db = self._get_db()
        row = db.execute(
            "SELECT id, cron_expr, job_kind, params_json, enabled, "
            "created_at, last_run_at, last_run_status, last_run_error "
            "FROM schedules WHERE id=?",
            (sched_id,),
        ).fetchone()
        return _row_to_dict(row) if row else None

    async def schedule_list_all(self) -> list[dict]:
        db = self._get_db()
        rows = db.execute(
            "SELECT id, cron_expr, job_kind, params_json, enabled, "
            "created_at, last_run_at, last_run_status, last_run_error "
            "FROM schedules ORDER BY created_at"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    async def schedule_list_due(self, now: datetime | None = None) -> list[dict]:
        """Return enabled schedules with at least one elapsed cron slot.

        Catch-up semantics: a schedule that missed N slots fires
        **once** on the next call, not N times. The base for the next
        slot is ``last_run_at`` if set, else ``created_at``.

        ``now`` defaults to ``datetime.now(timezone.utc)``; tests pass
        a frozen value.
        """
        if now is None:
            now = datetime.now(timezone.utc)
        elif now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        db = self._get_db()
        rows = db.execute(
            "SELECT id, cron_expr, job_kind, params_json, enabled, "
            "created_at, last_run_at, last_run_status, last_run_error "
            "FROM schedules WHERE enabled=1"
        ).fetchall()

        due: list[dict] = []
        for r in rows:
            sched = _row_to_dict(r)
            base_iso = sched["last_run_at"] or sched["created_at"]
            base = _parse_iso_utc(base_iso)
            try:
                next_fire = croniter(sched["cron_expr"], base).get_next(datetime)
            except (CroniterBadCronError, CroniterBadDateError) as exc:
                # Invalid cron stored — skip rather than crash dispatcher.
                # Loud (warning, not debug) per feedback_silent_except_log_level:
                # this is the most-likely operational footgun (downgrade /
                # bad migration leaving a phantom row), and silent skips
                # would be invisible in production.
                logger.warning(
                    "schedule %s has invalid cron_expr %r; skipping (%s)",
                    sched["id"],
                    sched["cron_expr"],
                    exc,
                )
                continue
            if next_fire.tzinfo is None:
                next_fire = next_fire.replace(tzinfo=timezone.utc)
            if next_fire <= now:
                due.append(sched)
        return due

    async def schedule_set_enabled(self, sched_id: str, enabled: bool) -> bool:
        db = self._get_db()
        cur = db.execute(
            "UPDATE schedules SET enabled=? WHERE id=?",
            (1 if enabled else 0, sched_id),
        )
        db.commit()
        return cur.rowcount > 0

    async def schedule_delete(self, sched_id: str) -> bool:
        db = self._get_db()
        cur = db.execute("DELETE FROM schedules WHERE id=?", (sched_id,))
        db.commit()
        return cur.rowcount > 0

    async def schedule_mark_run(
        self,
        sched_id: str,
        status: str,
        error: str | None = None,
        when: datetime | None = None,
    ) -> None:
        """Record a run outcome. ``status`` ∈ {'ok','error','timeout'}."""
        ts = (when or datetime.now(timezone.utc)).astimezone(timezone.utc)
        db = self._get_db()
        db.execute(
            "UPDATE schedules SET last_run_at=?, last_run_status=?, last_run_error=? WHERE id=?",
            (ts.isoformat(timespec="seconds"), status, error, sched_id),
        )
        db.commit()


def _row_to_dict(row: tuple) -> dict:
    return {
        "id": row[0],
        "cron_expr": row[1],
        "job_kind": row[2],
        "params": json.loads(row[3]) if row[3] else {},
        "enabled": bool(row[4]),
        "created_at": row[5],
        "last_run_at": row[6],
        "last_run_status": row[7],
        "last_run_error": row[8],
    }
