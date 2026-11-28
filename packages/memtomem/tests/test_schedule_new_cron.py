"""``new_cron_problem``: the cron check run before a schedule is stored (#3461)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from croniter import CroniterBadDateError, croniter  # type: ignore[import-untyped]

from memtomem.scheduler.cron import new_cron_problem


def _finds_a_date(expr: str) -> bool:
    try:
        croniter(expr, datetime.now(timezone.utc)).get_next(datetime)
    except CroniterBadDateError:
        return False
    return True


@pytest.mark.parametrize(
    "expr", ["0 0 30 2 *", "0 0 31 4 *", "0 0 31 2 *", "0 0 30 FEB *", "0 0 30 feb *"]
)
def test_a_date_that_never_occurs_is_refused(expr):
    problem = new_cron_problem(expr)
    assert problem is not None
    assert problem.startswith(
        f"invalid cron expression: {expr!r} matches no date croniter can find: "
    )
    assert "CroniterBadDateError" in problem


@pytest.mark.parametrize("expr", ["0 0 29 2 *", "0 3 * * 0", "*/5 * * * *", "0 0 L * *", "@yearly"])
def test_a_date_that_occurs_is_accepted(expr):
    assert new_cron_problem(expr) is None


@pytest.mark.parametrize("expr", ["0 0 30 2 1", "0 0 31 apr thu", "0 0 30 feb mon"])
def test_day_of_week_follows_the_installed_croniter(expr):
    """croniter 7 ORs day-of-month with day-of-week here; 6 and earlier find no date."""
    assert (new_cron_problem(expr) is None) is _finds_a_date(expr)


@pytest.mark.parametrize("expr", ["0 0 R(30-31) 2 *", "0 0 r(30-31) 2 *"])
def test_a_random_field_is_not_probed(expr):
    """No draw of these finds a date, so only the skipped probe lets them through."""
    assert not _finds_a_date(expr)
    assert new_cron_problem(expr) is None


def test_a_random_field_that_sometimes_fails_is_accepted():
    # ``R(28-30)`` fails only on the draw that lands on the 30th.
    assert new_cron_problem("0 0 R(28-30) 2 *") is None


def test_six_fields_are_not_probed():
    """A seconds field: valid on every supported croniter and with no date from any base."""
    assert not _finds_a_date("0 0 30 2 * 0")
    assert new_cron_problem("0 0 30 2 * 0") is None


def test_a_year_past_the_search_horizon_is_not_refused_for_it():
    expr = "0 0 1 1 * 0 2099"
    if croniter.is_valid(expr):
        assert new_cron_problem(expr) is None
    else:  # croniter 2.0 has no year field
        assert new_cron_problem(expr) == f"invalid cron expression: {expr!r}"


def test_an_expression_croniter_cannot_parse_keeps_its_message():
    assert new_cron_problem("not a cron") == "invalid cron expression: 'not a cron'"


def test_a_hashed_field_does_not_parse_without_a_hash_id():
    """The guide says so: neither way of adding a schedule passes a hash id."""
    assert new_cron_problem("H/5 * * * *") == "invalid cron expression: 'H/5 * * * *'"
