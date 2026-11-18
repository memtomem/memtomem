"""A schedule the dispatcher cannot schedule says so in every listing (#3459).

croniter 7 refuses expressions with non-ASCII digits or spaces that croniter 6
accepted, so a row stored under 6 raises in ``schedule_list_due`` on every
tick and is skipped with only a log line. Each read now carries
``cron_error``: the croniter error when croniter cannot parse the expression
(blocked), ``unparsable <column>: ...`` when the stored base timestamp does not
parse (blocked, #3440), or ``unchecked: ...`` when the check did not reach a
verdict, which includes ``CroniterBadDateError`` (a date that never occurs, or
one draw of a random field).
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import croniter as croniter_mod  # type: ignore[import-untyped]
import pytest
from click.testing import CliRunner
from croniter import croniter  # type: ignore[import-untyped]

from memtomem.cli import cli
from memtomem.server.context import AppContext
from memtomem.server.tools import schedule as schedule_tools
from memtomem.storage.mixins import schedules as schedules_mod

DIGIT = "0 \u0663 * * *"  # ARABIC-INDIC DIGIT THREE as the hour
NBSP = "0\u00a03 * * *"  # NO-BREAK SPACE between the fields
BOGUS = "bogus"  # refused by every croniter version
NEVER = "0 0 30 2 *"  # parses, no 30 February
RANDOM = "0 0 R(28-30) 2 *"  # fails only on the draw that lands on the 30th
RARE = "0 0 29 2 * 0 R(1970-2028)"  # schedules only when the year draw is 2028
VALID = "*/5 * * * *"
BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)

# croniter 6 accepted both Unicode forms; the lockfile and CI run 7.
needs_croniter_7 = pytest.mark.skipif(
    croniter.is_valid(DIGIT) or croniter.is_valid(NBSP),
    reason="this croniter accepts non-ASCII digits and spaces (croniter < 7)",
)

BLOCKED_MARK = "(automatic runs blocked: cron not accepted)"
TIMESTAMP_MARK = "(automatic runs blocked: stored timestamp unreadable)"
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


def _error(storage, sid: str) -> str | None:
    return asyncio.run(storage.schedule_get(sid))["cron_error"]


def _blocked_class(value: str | None) -> type | None:
    """The croniter class a blocked value names, or None when it is not blocked."""
    if value is None or value.startswith("unchecked: "):
        return None
    return getattr(croniter_mod, value.split(":", 1)[0])


class TestBlocked:
    @pytest.mark.parametrize(
        "expr",
        [
            BOGUS,
            pytest.param(DIGIT, marks=needs_croniter_7),
            pytest.param(NBSP, marks=needs_croniter_7),
        ],
    )
    def test_an_expression_croniter_cannot_parse_is_blocked(self, wired, expr):
        sid = _insert(wired, expr)

        got = _error(wired, sid)
        listed = {r["id"]: r for r in asyncio.run(wired.schedule_list_all())}

        # croniter raises subclasses (CroniterNotAlphaError for the digit); the
        # dispatcher's catch is on the base class, so the name must be one of them.
        raised = _blocked_class(got)
        assert raised is not None and issubclass(raised, croniter_mod.CroniterBadCronError), got
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

    @pytest.mark.parametrize("ran", [False, True])
    def test_the_base_is_the_dispatchers(self, wired, monkeypatch, ran):
        # last_run_at once the row has run, created_at before.
        sid = _insert(wired, VALID, ran_at=BASE if ran else None)
        row = asyncio.run(wired.schedule_get(sid))
        bases: list[datetime] = []
        real = schedules_mod.croniter

        def spy(expr, base, *args, **kwargs):
            bases.append(base)
            return real(expr, base, *args, **kwargs)

        monkeypatch.setattr(schedules_mod, "croniter", spy)
        asyncio.run(wired.schedule_get(sid))

        expected = row["last_run_at"] if ran else row["created_at"]
        assert (row["last_run_at"] is None) is (not ran)
        assert bases == [schedules_mod._parse_iso_utc(expected)]


class TestUnchecked:
    def test_a_date_that_never_comes_is_unchecked(self, wired):
        sid = _insert(wired, NEVER)

        got = _error(wired, sid)

        assert got.startswith("unchecked: CroniterBadDateError: "), got
        due = asyncio.run(wired.schedule_list_due(BASE + timedelta(days=1)))
        assert sid not in [d["id"] for d in due]

    @pytest.mark.parametrize("expr", [RANDOM, RARE])
    def test_a_random_field_is_never_reported_blocked(self, wired, expr):
        # Each read draws again; whatever it draws, it is not a blocked verdict.
        sid = _insert(wired, expr)

        seen = {_error(wired, sid) for _ in range(30)}

        assert all(v is None or v.startswith("unchecked: ") for v in seen), seen

    def test_a_date_error_is_one_computation_not_a_search_loop(self, wired, monkeypatch):
        sid = _insert(wired, VALID)
        calls = {"n": 0}

        def date_error(*args, **kwargs):
            calls["n"] += 1
            raise croniter_mod.CroniterBadDateError("failed to find next date")

        monkeypatch.setattr(schedules_mod, "croniter", date_error)
        got = _error(wired, sid)

        assert got == "unchecked: CroniterBadDateError: failed to find next date"
        assert calls["n"] == 1

    def test_a_date_out_of_range_is_unchecked_and_skips_only_its_row(self, wired, caplog):
        # The timestamp parses; croniter raises OverflowError, which neither of
        # the due scan's named catches covers (#3440).
        sid = _insert(wired, VALID)
        _set_last_run_at(wired, sid, "9999-12-31T23:59:30+00:00")
        good = _insert(wired, VALID)

        got = _error(wired, sid)
        with caplog.at_level(logging.WARNING, logger="memtomem.storage.mixins.schedules"):
            due = asyncio.run(wired.schedule_list_due(BASE + timedelta(days=1)))

        assert got.startswith("unchecked: OverflowError: "), got
        assert [d["id"] for d in due] == [good]
        assert [r.getMessage() for r in caplog.records] == [
            f"schedule {sid}: no next slot for '{VALID}' from last_run_at "
            "'9999-12-31T23:59:30+00:00'; skipping"
        ]

    def test_an_unexpected_failure_is_unchecked_and_does_not_fail_the_listing(
        self, wired, monkeypatch
    ):
        sid = _insert(wired, VALID)

        def broken(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(schedules_mod, "croniter", broken)
        listed = {r["id"]: r for r in asyncio.run(wired.schedule_list_all())}

        assert listed[sid]["cron_error"] == "unchecked: RuntimeError: boom"


def _set_created_at(storage, sid: str, value: str) -> None:
    db = storage._get_db()
    db.execute("UPDATE schedules SET created_at=? WHERE id=?", (value, sid))
    db.commit()


class TestUnparsableTimestamp:
    """A base timestamp that does not parse is blocked, and skips only its row (#3440).

    Before, it was ``unchecked:`` in the listings and raised out of the due
    scan, so the dispatcher skipped the tick for every schedule.
    """

    # (last_run_at, created_at) as stored; None leaves the column as written.
    CASES = {
        "last_run_at text": ("not-a-timestamp", None, "last_run_at"),
        # An empty last_run_at is not "never run": the base stays last_run_at.
        "last_run_at empty": ("", None, "last_run_at"),
        "created_at, never run": (None, "not-a-timestamp", "created_at"),
    }

    def _store(self, storage, last_run_at, created_at) -> str:
        sid = _insert(storage, VALID, ran_at=None)
        if created_at is not None:
            _set_created_at(storage, sid, created_at)
        if last_run_at is not None:
            _set_last_run_at(storage, sid, last_run_at)
        return sid

    @pytest.mark.parametrize("case", list(CASES))
    def test_the_listing_and_the_due_scan_agree(self, wired, caplog, case):
        last_run_at, created_at, column = self.CASES[case]
        sid = self._store(wired, last_run_at, created_at)
        good = _insert(wired, VALID)
        stored = last_run_at if column == "last_run_at" else created_at

        got = _error(wired, sid)
        with caplog.at_level(logging.WARNING, logger="memtomem.storage.mixins.schedules"):
            due = asyncio.run(wired.schedule_list_due(BASE + timedelta(days=1)))

        assert got == f"unparsable {column}: ValueError: Invalid isoformat string: {stored!r}"
        assert [d["id"] for d in due] == [good]
        assert [r.getMessage() for r in caplog.records] == [
            f"schedule {sid} has an unparsable {column} {stored!r}; skipping (ValueError)"
        ]

    def test_a_bad_created_at_is_not_the_base_once_the_row_has_run(self, wired):
        sid = _insert(wired, VALID)  # ran at BASE, written by this binary
        _set_created_at(wired, sid, "not-a-timestamp")
        _set_last_run_at(wired, sid, "2026-01-01T00:00:00+00:00")  # an older binary's stamp

        due = asyncio.run(wired.schedule_list_due(BASE + timedelta(minutes=5)))

        assert _error(wired, sid) is None
        assert [d["id"] for d in due] == [sid]

    def test_the_logged_value_is_cut_short(self, wired, caplog):
        sid = self._store(wired, "x" * 10_000, None)

        with caplog.at_level(logging.WARNING, logger="memtomem.storage.mixins.schedules"):
            asyncio.run(wired.schedule_list_due(BASE + timedelta(days=1)))

        (message,) = [r.getMessage() for r in caplog.records]
        assert (
            message
            == f"schedule {sid} has an unparsable last_run_at '{'x' * 79}; skipping (ValueError)"
        )


class TestListings:
    def test_the_json_listings_carry_the_field(self, wired):
        bad = _insert(wired, BOGUS)
        never = _insert(wired, NEVER)
        good = _insert(wired, VALID)

        mcp = json.loads(asyncio.run(schedule_tools.mem_schedule_list()))["schedules"]
        cli_rows = json.loads(CliRunner().invoke(cli, ["schedule", "list", "--json"]).stdout)

        for rows in (mcp, cli_rows):
            errors = {r["id"]: r["cron_error"] for r in rows}
            assert errors[good] is None
            assert _blocked_class(errors[bad]) is not None
            assert errors[never].startswith("unchecked: CroniterBadDateError: ")

    def test_the_plain_listing_marks_each_kind_and_says_what_to_do_once(self, wired):
        bad = _insert(wired, BOGUS)
        never = _insert(wired, NEVER)
        odd = _insert(wired, VALID)
        _set_last_run_at(wired, odd, "not-a-timestamp")
        good = _insert(wired, VALID)

        result = CliRunner().invoke(cli, ["schedule", "list"])

        assert result.exit_code == 0, result.output
        lines = {ln.split()[0]: ln for ln in result.output.splitlines() if ln.startswith("  ")}
        assert lines[bad].endswith(BLOCKED_MARK)
        assert lines[never].endswith(UNCHECKED_MARK)
        assert lines[odd].endswith(TIMESTAMP_MARK)
        assert lines[good].endswith("(ok)"), lines[good]
        closing = [ln for ln in result.output.splitlines() if "not run automatically" in ln]
        assert closing == [
            "2 schedule(s) are not run automatically: the dispatcher cannot schedule them "
            "(see cron_error in 'mm schedule list --json'). Replace each with "
            "'mm schedule add', then remove it with 'mm schedule delete <id>'."
        ]

    def test_a_listing_with_nothing_blocked_has_no_closing_line(self, wired):
        _insert(wired, VALID)
        never = _insert(wired, NEVER)

        result = CliRunner().invoke(cli, ["schedule", "list"])

        assert result.exit_code == 0, result.output
        assert "not run automatically" not in result.output
        assert len(result.output.splitlines()) == 2
        (never_line,) = [ln for ln in result.output.splitlines() if never in ln]
        assert never_line.endswith(UNCHECKED_MARK), never_line


class TestRunNowStillRunsABlockedRow:
    def test_cli_run_now(self, wired, monkeypatch):
        monkeypatch.setattr(schedule_tools, "_run_now_timeout", lambda app: 5.0)
        sid = _insert(wired, BOGUS, ran_at=None)
        before = asyncio.run(wired.schedule_get(sid))
        assert _blocked_class(before["cron_error"]) is not None
        assert before["last_run_status"] is None

        result = CliRunner().invoke(cli, ["schedule", "run-now", sid])

        assert result.exit_code == 0, result.output
        after = asyncio.run(wired.schedule_get(sid))
        assert after["last_run_status"] == "ok"
        assert after["last_run_result"] == {"dead_links_deleted": 0}

    def test_mcp_run_now(self, wired, monkeypatch):
        monkeypatch.setattr(schedule_tools, "_run_now_timeout", lambda app: 5.0)
        sid = _insert(wired, BOGUS, ran_at=None)
        before = asyncio.run(wired.schedule_get(sid))
        assert _blocked_class(before["cron_error"]) is not None
        assert before["last_run_status"] is None

        out = json.loads(asyncio.run(schedule_tools.mem_schedule_run_now(sid)))

        assert out == {"ok": True, "reason": "ran", "result": {"dead_links_deleted": 0}}, out
        assert asyncio.run(wired.schedule_get(sid))["last_run_status"] == "ok"
