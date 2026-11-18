"""Schedule storage mixin — CRUD for the ``schedules`` table (P2 Phase A).

Phase A is direct-cron only (no NL parser). Cron expressions are
interpreted in UTC; ``created_at`` and ``last_run_at`` are stored as
UTC ISO strings. ``schedule_list_due`` returns at-most-once catch-up
semantics — if multiple cron slots elapsed since ``last_run_at``, the
schedule fires exactly once on the next dispatcher tick (no backfill).

Dispatch is also at-most-once **per slot** across processes and crashes:
``schedule_try_claim`` does a compare-and-set on ``last_run_at`` so a
second dispatcher (a concurrent MCP-server process on the same DB) or a
post-crash restart that reads the same due row loses the claim and skips
it. A crash between the claim and the terminal ``schedule_mark_run``
skips the slot rather than re-running it — deliberate, because job
bodies aren't all idempotent. See issue #1564.

A job that continues between runs keeps its position in ``job_state``
(#3432). A claim leaves it alone, and ``schedule_mark_run`` writes it with
the outcome of the run that moved it.
"""

from __future__ import annotations

import enum
import json
import logging
from datetime import datetime, timezone
from typing import Any, Final
from uuid import uuid4

from croniter import CroniterBadCronError, CroniterBadDateError, croniter  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)


class _KeepState(enum.Enum):
    KEEP = "keep"


KEEP_STATE: Final = _KeepState.KEEP
"""``schedule_mark_run``'s default for ``state``: leave ``job_state`` as stored.

Distinct from ``None``, which clears the column. An enum member, so it is
not JSON: it cannot be stored by mistake.
"""

StateUpdate = dict[str, Any] | None | _KeepState
"""What a run does to the stored state: replace it, clear it, or keep it."""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _run_stamp(when: datetime | None) -> str:
    """Format a ``last_run_at`` value written by this binary.

    Always microsecond precision, even when zero (``.000000``). Binaries from
    before ``last_run_result`` existed write whole seconds, so a stamp they
    write never equals one written here, even within the same second. That
    inequality is what hides a result an older binary's later run left behind
    (see ``_decode_result``). Readers parse both forms, and the claim CAS
    compares the token the caller read, so mixing them is safe.
    """
    ts = (when or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return ts.isoformat(timespec="microseconds")


def _parse_iso_utc(value: str) -> datetime:
    """Parse an ISO timestamp; assume UTC if naive."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


_CRON_REDRAWS: Final = 32
"""Extra computations ``_cron_error`` makes after a croniter error.

A random ``R`` field is drawn again on every construction, so one failed
draw is not a verdict. If any of these succeeds, the expression sometimes
schedules: ``0 0 R(28-30) 2 *`` fails one draw in three, so 32 more all
failing has odds below 1e-5. A fixed expression gives the same answer every
time, so the redraws cost something only for rows that already failed.
"""


def _cron_error(cron_expr: str, created_at: str, last_run_at: str | None) -> str | None:
    """Why the dispatcher cannot schedule this row, or ``None`` when it can.

    Repeats the next-slot computation ``schedule_list_due`` makes, from the
    same base (``last_run_at`` or ``created_at``), so a row it skips is the
    row reported here. That covers an expression the installed croniter no
    longer accepts (croniter 7 refuses non-ASCII digits and spaces that 6
    accepted) and one it accepts but can never fire, such as ``0 0 30 2 *``.

    A croniter refusal is ``"<error class>: <message>"``. Anything else is
    prefixed ``"unchecked: "``: it is not a verdict on the expression, and it
    must not fail the listing that carries it, so it is returned instead of
    raised. That covers a stored timestamp that does not parse, and an
    expression whose random ``R`` field fails on some draws and not on
    others: the dispatcher draws again on every tick, so it runs on the draws
    that schedule (see ``_CRON_REDRAWS``).
    """
    try:
        base = _parse_iso_utc(last_run_at or created_at)
    except Exception as exc:
        return f"unchecked: {type(exc).__name__}: {exc}"
    try:
        croniter(cron_expr, base).get_next(datetime)
    except (CroniterBadCronError, CroniterBadDateError) as exc:
        first = exc
    except Exception as exc:
        return f"unchecked: {type(exc).__name__}: {exc}"
    else:
        return None
    for _ in range(_CRON_REDRAWS):
        try:
            croniter(cron_expr, base).get_next(datetime)
        except (CroniterBadCronError, CroniterBadDateError):
            continue
        except Exception as exc:
            return f"unchecked: {type(exc).__name__}: {exc}"
        return f"unchecked: some draws of a random field fail: {type(first).__name__}: {first}"
    return f"{type(first).__name__}: {first}"


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
        sched_id = str(uuid4())
        with self._rolls_back_if_standalone(db):
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
            self._commit_if_standalone(db)
        return sched_id

    async def schedule_get(self, sched_id: str) -> dict | None:
        db = self._get_db()
        row = db.execute(
            "SELECT id, cron_expr, job_kind, params_json, enabled, "
            "created_at, last_run_at, last_run_status, last_run_error, last_run_result, "
            "job_state FROM schedules WHERE id=?",
            (sched_id,),
        ).fetchone()
        return _row_to_dict(row) if row else None

    async def schedule_list_all(self) -> list[dict]:
        db = self._get_db()
        rows = db.execute(
            "SELECT id, cron_expr, job_kind, params_json, enabled, "
            "created_at, last_run_at, last_run_status, last_run_error, last_run_result, "
            "job_state FROM schedules ORDER BY created_at"
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
            "created_at, last_run_at, last_run_status, last_run_error, last_run_result, "
            "job_state FROM schedules WHERE enabled=1"
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
        with self._rolls_back_if_standalone(db):
            cur = db.execute(
                "UPDATE schedules SET enabled=? WHERE id=?",
                (1 if enabled else 0, sched_id),
            )
            self._commit_if_standalone(db)
        return cur.rowcount > 0

    async def schedule_delete(self, sched_id: str) -> bool:
        db = self._get_db()
        with self._rolls_back_if_standalone(db):
            cur = db.execute("DELETE FROM schedules WHERE id=?", (sched_id,))
            self._commit_if_standalone(db)
        return cur.rowcount > 0

    async def schedule_try_claim(
        self,
        sched_id: str,
        prev_last_run_at: str | None,
        when: datetime | None = None,
    ) -> bool:
        """Atomically claim a due schedule for this dispatcher.

        Compare-and-set on ``last_run_at``: advance it to ``when`` (now)
        and set status='running' only if the row's ``last_run_at`` still
        equals the value the caller read in ``schedule_list_due``
        (``prev_last_run_at`` — possibly None on first run, hence ``IS``
        not ``=``). Returns True iff this call won the claim
        (``rowcount == 1``). See issue #1564.

        ``last_run_error`` and ``last_run_result`` are cleared here so the
        prior run's outcome doesn't linger while the new run is in flight;
        the terminal ``schedule_mark_run`` repopulates them. ``job_state``
        is not touched: it is where the claimed run starts from. WAL +
        ``busy_timeout`` on the write connection serializes the racing
        UPDATEs, so a single conditional statement is self-atomic — no
        explicit transaction needed.
        """
        # At-most-once depends on the winning claim being durable before
        # the job runs. Joining a caller's transaction would let a rollback
        # make an already-executing slot claimable again (#1564, #2162), so
        # the claim and its terminal record own their own durability.
        self._require_transaction_idle("schedule_try_claim")
        db = self._get_db()
        with self._rolls_back_if_standalone(db):
            cur = db.execute(
                "UPDATE schedules SET last_run_at=?, last_run_status='running', "
                "last_run_error=NULL, last_run_result=NULL "
                "WHERE id=? AND last_run_at IS ?",
                (_run_stamp(when), sched_id, prev_last_run_at),
            )
            self._commit_if_standalone(db)
        return cur.rowcount == 1

    async def schedule_mark_run(
        self,
        sched_id: str,
        status: str,
        error: str | None = None,
        when: datetime | None = None,
        result: dict[str, Any] | None = None,
        state: StateUpdate = KEEP_STATE,
    ) -> None:
        """Record a run outcome. ``status`` ∈ {'ok','skipped','error','timeout'}.

        ``result`` is the runner's returned summary. It is stored with the
        ``last_run_at`` written by the same statement, so a reader can tell
        whether it still describes the latest run.

        ``state`` is what the run does to ``job_state``: a dict replaces it,
        ``None`` clears it, and ``KEEP_STATE`` (the default) leaves it out of
        the statement. A run that did not finish passes nothing, so the next
        run starts where this one did. The state is written by the statement
        that writes ``last_run_at``, so a claim that still matches
        ``last_run_at`` also matches the state its caller read.
        """
        stamp = _run_stamp(when)
        payload = (
            None if result is None else json.dumps({"run_at": stamp, "result": result}, default=str)
        )
        state_json = None if state is KEEP_STATE or state is None else json.dumps(state)
        db = self._get_db()
        # Terminal record for a claim that owns its durability: rolling it
        # back would leave the schedule stuck in 'running' forever.
        self._require_transaction_idle("schedule_mark_run")
        with self._rolls_back_if_standalone(db):
            if state is KEEP_STATE:
                db.execute(
                    "UPDATE schedules SET last_run_at=?, last_run_status=?, last_run_error=?, "
                    "last_run_result=? WHERE id=?",
                    (stamp, status, error, payload, sched_id),
                )
            else:
                db.execute(
                    "UPDATE schedules SET last_run_at=?, last_run_status=?, last_run_error=?, "
                    "last_run_result=?, job_state=? WHERE id=?",
                    (stamp, status, error, payload, state_json, sched_id),
                )
            self._commit_if_standalone(db)


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
        "last_run_result": _decode_result(row[0], row[6], row[9]),
        "state": _decode_state(row[0], row[10]),
        "cron_error": _cron_error(row[1], row[5], row[6]),
    }


def _decode_state(sched_id: str, raw: str | None) -> dict | None:
    """Return the stored job state, or ``None`` when there is none to use.

    NULL is the ordinary value: a job that keeps no state, a schedule that
    has not run, or a walk that starts over. A value that is not a JSON
    object is logged and read as ``None``; what an object has to hold is
    the job's own check.

    Text nested too deeply to parse raises ``RecursionError``, not
    ``ValueError``. Every schedule read decodes every row it returns, so
    letting that through would fail the listing the dispatcher reads and
    stop every schedule in the store, not only the one with the bad row.
    """
    if raw is None:
        return None
    try:
        state = json.loads(raw)
    except (ValueError, RecursionError):
        logger.warning("schedule %s has an unreadable job_state; ignoring", sched_id)
        return None
    if not isinstance(state, dict):
        logger.warning("schedule %s has a malformed job_state; ignoring", sched_id)
        return None
    return state


def _decode_result(sched_id: str, last_run_at: str | None, raw: str | None) -> dict | None:
    """Return the stored result only if it was written with the current ``last_run_at``.

    An older binary's claim or completion advances ``last_run_at`` without
    touching ``last_run_result``, so an envelope whose ``run_at`` differs
    describes an earlier run. Compare the raw strings: as parsed datetimes, a
    ``.000000`` stamp would equal the whole-second stamp such a binary writes.
    """
    if raw is None:
        return None
    try:
        envelope = json.loads(raw)
    except ValueError:
        logger.warning("schedule %s has an unreadable last_run_result; ignoring", sched_id)
        return None
    if not isinstance(envelope, dict) or not isinstance(envelope.get("result"), dict):
        logger.warning("schedule %s has a malformed last_run_result; ignoring", sched_id)
        return None
    if envelope.get("run_at") != last_run_at:
        return None
    return envelope["result"]
