"""``_parse_duration`` accepts one integer and one unit, and nothing else.

``mm session start --auto-end-stale`` turns its argument into a ``timedelta``
through ``_parse_duration`` (``memtomem/cli/session_cmd.py``). The parser is
hand-written, and the only coverage it had was indirect: three CLI tests that
used ``24h``, ``forever`` and two oversized values (#3242).

That leaves the parser's actual contract unpinned - the other three units,
upper case, the optional whitespace, leading zeros, and the near-miss inputs a
hook author is likely to type (``1w``, ``1.5h``, ``-1h``, ``1d2h``). These two
tables are that contract: one row per accepted spelling asserting the exact
``timedelta``, one row per rejected spelling asserting the
``click.BadParameter`` the CLI surfaces.

``"0s"`` is deliberately absent from both tables. It currently returns
``timedelta(0)``, but whether a zero cutoff should be accepted is an open
question rather than a contract, so pinning it either way would freeze an
undecided behaviour.
"""

from __future__ import annotations

from datetime import timedelta

import click
import pytest

from memtomem.cli.session_cmd import _parse_duration


ACCEPTED = pytest.mark.parametrize(
    "spec,expected",
    [
        ("45s", timedelta(seconds=45)),
        ("30m", timedelta(minutes=30)),
        ("24h", timedelta(hours=24)),
        ("7d", timedelta(days=7)),
        ("24H", timedelta(hours=24)),
        (" 7d ", timedelta(days=7)),
        ("24 h", timedelta(hours=24)),
        ("01h", timedelta(hours=1)),
    ],
    ids=[
        "seconds",
        "minutes",
        "hours",
        "days",
        "upper-case-unit",
        "surrounding-whitespace",
        "inner-whitespace",
        "leading-zero",
    ],
)

REJECTED = pytest.mark.parametrize(
    "spec",
    ["", "h", "1w", "1.5h", "-1h", "1d2h"],
    ids=[
        "empty",
        "no-number",
        "weeks-are-not-a-unit",
        "not-an-integer",
        "negative",
        "two-pairs",
    ],
)


@ACCEPTED
def test_parse_duration_returns_the_exact_timedelta(spec: str, expected: timedelta) -> None:
    assert _parse_duration(spec) == expected


@REJECTED
def test_parse_duration_rejects_the_spec(spec: str) -> None:
    with pytest.raises(click.BadParameter, match="invalid duration"):
        _parse_duration(spec)
