"""The cron check both ways of adding a schedule run before storing it."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from croniter import (  # type: ignore[import-untyped]
    CroniterBadCronError,
    CroniterBadDateError,
    croniter,
)

# croniter 7's field split (``field_re`` with ``re.ASCII``). croniter 2 and 6 also split on
# non-ASCII whitespace; an expression they read as five fields that way is not five fields
# here, so it is not probed.
_FIELD_RE = re.compile(r"\S+", re.ASCII)
_NAME_RE = re.compile(
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|sun|mon|tue|wed|thu|fri|sat)\b"
)
_DETERMINISTIC_RE = re.compile(r"[0-9*,\-/?#lw \t\n\r\f\v]*")


def _is_deterministic(cron_expr: str) -> bool:
    """Whether ``cron_expr`` is five fields that croniter expands the same way every time.

    An allowlist, not a recogniser of croniter's random syntax: after removing whole
    three-letter month and day names, every character must be a digit, one of
    ``* , - / ? # l w`` or ASCII whitespace. A random or hashed field (``R``, ``r(28-30)``,
    ``H/5``) carries an ``r`` or ``h`` and so fails it, as would syntax a later croniter adds
    if it uses a character outside the allowlist.
    Only five-field expressions are looked up. A seven-field expression's year field can
    name a year past croniter's search horizon, so finding no date there does not mean
    there is none; six-field ones (a seconds field) are left out with them.
    """
    lowered = cron_expr.lower()
    if len(_FIELD_RE.findall(lowered)) != 5:
        return False
    return _DETERMINISTIC_RE.fullmatch(_NAME_RE.sub("", lowered)) is not None


def new_cron_problem(cron_expr: str) -> str | None:
    """Why a new schedule with ``cron_expr`` is refused, or ``None`` to accept it.

    ``croniter.is_valid`` only parses, so it accepts a date that never occurs
    (``0 0 30 2 *``), which the dispatcher then skips on every tick. For an expression
    ``_is_deterministic`` accepts, one next-slot computation decides it: every draw would
    give the same answer. Any other expression is accepted once it parses. Its
    ``CroniterBadDateError`` can come from one draw of a random field that the next draw
    does not repeat (``0 0 R(28-30) 2 *`` fails only on the 30th), and croniter has no
    public way to tell that from a date that never occurs.

    The verdict follows the installed croniter, as the dispatcher does: ``0 0 30 2 1`` runs
    on Mondays under croniter 7 and finds no date under 6.
    """
    if not croniter.is_valid(cron_expr):
        return f"invalid cron expression: {cron_expr!r}"
    if not _is_deterministic(cron_expr):
        return None
    try:
        croniter(cron_expr, datetime.now(timezone.utc)).get_next(datetime)
    except (CroniterBadCronError, CroniterBadDateError) as exc:
        return (
            f"invalid cron expression: {cron_expr!r} matches no date croniter can find: "
            f"{type(exc).__name__}: {exc}"
        )
    return None
