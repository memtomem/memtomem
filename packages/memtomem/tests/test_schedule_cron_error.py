"""A schedule the dispatcher cannot schedule says so in every listing (#3459).

croniter 7 refuses expressions with non-ASCII digits or spaces that croniter 6
accepted, so a row stored under 6 raises in ``schedule_list_due`` on every
tick and is skipped with only a log line. An expression for a date that never
comes (``0 0 30 2 *``) passes ``croniter.is_valid`` and is skipped the same
way. Each read now carries ``cron_error`` for such a row.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner
import croniter as croniter_mod  # type: ignore[import-untyped]
from croniter import croniter  # type: ignore[import-untyped]

from memtomem.cli import cli
from memtomem.server.context import AppContext
from memtomem.server.tools import schedule as schedule_tools
from memtomem.storage.mixins import schedules as schedules_mod

DIGIT = "0 ٣ * * *"  # ARABIC-INDIC DIGIT THREE as the hour
NBSP = "0 3 * * *"  # NO-BREAK SPACE between the fields
NEVER = "0 0 30 2 *"  # valid syntax, no 30 February
VALID = "*/5 * * * *"
BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)

# croniter 6 accepted both Unicode forms; the lockfile and CI run 7.
needs_croniter_7 = pytest.mark.skipif(
    croniter.is_valid(DIGIT) or croniter.is_valid(NBSP),
    reason="this croniter accepts non-ASCII digits and spaces (croniter < 7)",
)

BLOCKED_MARK = "(automatic runs blocked: cron not accepted)"
UNCHECKED_MARK = "(cron check failed)"


@pytest.fixture
def wired(components, monkeypatch):
    @asynccontextmanager
    async def fake_components():
        yield components

    monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake_components)
    monkeypatch.setattr(
        schedule_tools,
        "_get_app_initialized",
        AsyncMock(return_value=AppContext.from_components(components)),
    )
    return components.storage


def _insert(storage, expr: str, *, ran_at: datetime | None = BASE) -> str:
    # Inserted directly: both add paths validate, a row stored by an older
    # croniter did not go through this one's check.
    sid = asyncio.run(storage.schedule_insert(expr, "dead_chunk_link_cleanup", {}))
    if ran_at is not None:
        asyncio.run(storage.schedule_mark_run(sid, "ok", when=ran_at))
    return sid


def _set_last_run_at(storage, sid: str, value: str) -> None:
    db = storage._get_db()
    db.execute("UPDATE schedules SET last_run_at=? WHERE id=?", (value, sid))
    db.commit()


class TestStoredRows:
    @pytest.mark.parametrize(
        ("expr", "error_class"),
        [
            pytest.param(DIGIT, "CroniterBadCronError", marks=needs_croniter_7),
            pytest.param(NBSP, "CroniterBadCronError", marks=needs_croniter_7),
            (NEVER, "CroniterBadDateError"),
        ],
    )
    def test_a_row_the_dispatcher_skips_carries_the_croniter_error(self, wired, expr, error_class):
        sid = _insert(wired, expr)

        got = asyncio.run(wired.schedule_get(sid))["cron_error"]
        listed = {r["id"]: r for r in asyncio.run(wired.schedule_list_all())}

        # croniter raises subclasses (CroniterNotAlphaError for the digit); the
        # dispatcher's catch is on the base class, so the name must be one of them.
        assert got is not None, got
        raised = getattr(croniter_mod, got.split(":", 1)[0])
        assert issubclass(raised, getattr(croniter_mod, error_class)), got
        assert listed[sid]["cron_error"] == got
        # The row is the one the dispatcher skips, a day of slots later.
        due = asyncio.run(wired.schedule_list_due(BASE + timedelta(days=1)))
        assert sid not in [d["id"] for d in due]

    def test_a_schedulable_row_has_none_including_a_disabled_one(self, wired):
        on = _insert(wired, VALID)
        off = _insert(wired, VALID)
        asyncio.run(wired.schedule_set_enabled(off, False))

        listed = {r["id"]: r for r in asyncio.run(wired.schedule_list_all())}

        assert listed[on]["cron_error"] is None
        assert listed[off]["cron_error"] is None
        assert listed[off]["enabled"] is False

    def test_a_due_row_carries_none(self, wired):
        sid = _insert(wired, VALID)

        due = asyncio.run(wired.schedule_list_due(BASE + timedelta(minutes=5)))

        assert [(d["id"], d["cron_error"]) for d in due] == [(sid, None)]

    def test_the_base_is_created_at_before_the_first_run(self, wired):
        sid = _insert(wired, NEVER, ran_at=None)

        row = asyncio.run(wired.schedule_get(sid))

        assert row["last_run_at"] is None
        assert row["cron_error"].startswith("CroniterBadDateError: ")

    def test_a_timestamp_that_does_not_parse_is_unchecked_not_raised(self, wired):
        sid = _insert(wired, VALID)
        _set_last_run_at(wired, sid, "not-a-timestamp")

        got = asyncio.run(wired.schedule_get(sid))["cron_error"]
        listed = {r["id"]: r for r in asyncio.run(wired.schedule_list_all())}

        assert got.startswith("unchecked: ValueError: "), got
        assert listed[sid]["cron_error"] == got
        # The due scan keeps its behaviour: it still raises for this row, and
        # the dispatcher logs and skips the tick (health_watchdog.py).
        with pytest.raises(ValueError):
            asyncio.run(wired.schedule_list_due(BASE + timedelta(days=1)))

    def test_an_unexpected_failure_is_unchecked_and_does_not_fail_the_listing(
        self, wired, monkeypatch
    ):
        sid = _insert(wired, VALID)

        def broken(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(schedules_mod, "croniter", broken)
        listed = {r["id"]: r for r in asyncio.run(wired.schedule_list_all())}

        assert listed[sid]["cron_error"] == "unchecked: RuntimeError: boom"


class TestListings:
    def test_the_json_listings_carry_the_field(self, wired):
        bad = _insert(wired, NEVER)
        good = _insert(wired, VALID)

        mcp = json.loads(asyncio.run(schedule_tools.mem_schedule_list()))["schedules"]
        cli_rows = json.loads(CliRunner().invoke(cli, ["schedule", "list", "--json"]).stdout)

        for rows in (mcp, cli_rows):
            errors = {r["id"]: r["cron_error"] for r in rows}
            assert errors[good] is None
            assert errors[bad].startswith("CroniterBadDateError: ")

    @needs_croniter_7
    def test_the_plain_listing_marks_each_kind_and_says_what_to_do_once(self, wired):
        bad = _insert(wired, DIGIT)
        never = _insert(wired, NEVER)
        odd = _insert(wired, VALID)
        _set_last_run_at(wired, odd, "not-a-timestamp")
        good = _insert(wired, VALID)

        result = CliRunner().invoke(cli, ["schedule", "list"])

        assert result.exit_code == 0, result.output
        lines = {ln.split()[0]: ln for ln in result.output.splitlines() if ln.startswith("  ")}
        assert lines[bad].endswith(BLOCKED_MARK)
        assert lines[never].endswith(BLOCKED_MARK)
        assert lines[odd].endswith(UNCHECKED_MARK)
        assert lines[good].endswith("(ok)"), lines[good]
        closing = [ln for ln in result.output.splitlines() if "not run automatically" in ln]
        assert closing == [
            "2 schedule(s) are not run automatically: their cron expression cannot be "
            "scheduled (see cron_error in 'mm schedule list --json'). Replace each with "
            "'mm schedule add', then remove it with 'mm schedule delete <id>'."
        ]

    def test_a_listing_with_nothing_blocked_has_no_closing_line(self, wired):
        _insert(wired, VALID)
        odd = _insert(wired, VALID)
        _set_last_run_at(wired, odd, "not-a-timestamp")

        result = CliRunner().invoke(cli, ["schedule", "list"])

        assert result.exit_code == 0, result.output
        assert "not run automatically" not in result.output
        assert len(result.output.splitlines()) == 2


class TestRunNowStillRunsABlockedRow:
    def test_cli_run_now(self, wired, monkeypatch):
        monkeypatch.setattr(schedule_tools, "_run_now_timeout", lambda app: 5.0)
        sid = _insert(wired, NEVER)

        result = CliRunner().invoke(cli, ["schedule", "run-now", sid])

        assert result.exit_code == 0, result.output
        assert asyncio.run(wired.schedule_get(sid))["last_run_status"] == "ok"

    def test_mcp_run_now(self, wired, monkeypatch):
        monkeypatch.setattr(schedule_tools, "_run_now_timeout", lambda app: 5.0)
        sid = _insert(wired, NEVER)

        out = json.loads(asyncio.run(schedule_tools.mem_schedule_run_now(sid)))

        assert out["ok"] is True, out
