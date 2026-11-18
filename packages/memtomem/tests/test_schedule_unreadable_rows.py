"""One schedule row the scheduler cannot read stops no other schedule (#3440).

Every schedule read decodes every row it returns, so one stored value that
raised failed ``schedule_list_all`` and ``schedule_list_due`` for the whole
store, and the dispatcher, which logs a failed due scan and returns, ran no
schedule at all. Reaching it needs a write from outside memtomem; the rows
here are corrupted with a direct ``UPDATE``, as the issue reproduced them.

An unreadable ``params_json`` is never read as ``{}``: that would run the job
with its defaults, and ``importance_decay``'s are a deletion.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner

from memtomem.cli import cli
from memtomem.config import HealthWatchdogConfig, SchedulerConfig
from memtomem.scheduler.jobs import JOB_KINDS
from memtomem.server.context import AppContext
from memtomem.server.health_watchdog import HealthWatchdog
from memtomem.server.tools import schedule as schedule_tools
from memtomem.storage.mixins import schedules as schedules_mod

LOGGER = "memtomem.storage.mixins.schedules"
LIMIT = schedules_mod._MAX_STORED_DEPTH
LATER = datetime(2030, 1, 1, tzinfo=timezone.utc)


def nested(depth: int) -> dict:
    """An object *depth* levels deep, alternating dicts and lists below the root."""
    value: object = 1
    for level in range(depth - 1):
        value = [value] if level % 2 else {"k": value}
    return {"k": value}


def depth_of(value: object) -> int:
    if isinstance(value, dict):
        return 1 + max((depth_of(v) for v in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((depth_of(v) for v in value), default=0)
    return 0


def test_the_fixture_depths_are_what_they_say():
    assert depth_of(nested(LIMIT)) == LIMIT
    assert depth_of(nested(LIMIT + 1)) == LIMIT + 1


class _Rows:
    def __init__(self, storage):
        self.storage = storage

    def add(self, job_kind="dead_chunk_link_cleanup", params=None) -> str:
        return asyncio.run(self.storage.schedule_insert("* * * * *", job_kind, params or {}))

    def set(self, sid, column, value) -> None:
        db = self.storage._get_db()
        db.execute(f"UPDATE schedules SET {column}=? WHERE id=?", (value, sid))
        db.commit()

    def get(self, sid) -> dict:
        return asyncio.run(self.storage.schedule_get(sid))

    def listed(self) -> dict[str, dict]:
        return {r["id"]: r for r in asyncio.run(self.storage.schedule_list_all())}

    def due(self) -> list[str]:
        return [r["id"] for r in asyncio.run(self.storage.schedule_list_due(LATER))]


@pytest.fixture
def rows(storage):
    return _Rows(storage)


# ── The three JSON columns ────────────────────────────────────────────


def _result_envelope(run_at: str, result: object) -> str:
    # A valid envelope written with the row's own stamp, so only the nesting
    # inside ``result`` can make the read refuse it.
    return json.dumps({"run_at": run_at, "result": result})


DEEP_TEXT = '{"k":' * 10_000 + "1" + "}" * 10_000


def _parses(text: str) -> bool:
    try:
        json.loads(text)
    except RecursionError:
        return False
    return True


# 10,000 levels raise ``RecursionError`` on 3.12 and 3.13 and parse on 3.14,
# where the depth limit refuses them instead.
CORRUPT = {
    "not json": "unreadable",
    "10,000 levels": "malformed" if _parses(DEEP_TEXT) else "unreadable",
    "past the depth limit": "malformed",
    "an array": "malformed",
}


def _corrupt_text(case: str) -> str:
    return {
        "not json": "{not json",
        "10,000 levels": DEEP_TEXT,
        "past the depth limit": json.dumps(nested(LIMIT + 1)),
        "an array": "[]",
    }[case]


def _store_corrupt(rows: _Rows, sid: str, column: str, case: str) -> None:
    if column == "last_run_result":
        stamp = "2026-01-01T00:00:00.000000+00:00"
        rows.set(sid, "last_run_at", stamp)
        if case == "past the depth limit":
            text = _result_envelope(stamp, nested(LIMIT))  # LIMIT + 1 with the envelope
        elif case == "10,000 levels":
            text = '{"run_at": "%s", "result": %s}' % (stamp, DEEP_TEXT)
        else:
            text = _corrupt_text(case)
        rows.set(sid, column, text)
    else:
        rows.set(sid, column, _corrupt_text(case))


READ_AS = {"params_json": "params", "job_state": "state", "last_run_result": "last_run_result"}


@pytest.mark.parametrize("column", list(READ_AS))
@pytest.mark.parametrize("case", list(CORRUPT))
def test_every_listing_survives_one_unreadable_value(rows, column, case):
    bad = rows.add()
    good = rows.add()
    _store_corrupt(rows, bad, column, case)

    listed = rows.listed()

    assert set(listed) == {bad, good}
    assert listed[bad][READ_AS[column]] is None
    assert rows.get(bad)[READ_AS[column]] is None
    assert sorted(rows.due()) == sorted([bad, good])
    # Both shapes the listing surfaces serialise (CLI --json, mem_schedule_list).
    assert json.loads(json.dumps(list(listed.values()), indent=2, default=str))
    assert json.loads(json.dumps({"schedules": list(listed.values())}))


@pytest.mark.parametrize("column", ["job_state", "last_run_result"])
@pytest.mark.parametrize("case", list(CORRUPT))
def test_state_and_result_are_ignored_with_a_warning(rows, caplog, column, case):
    sid = rows.add()
    _store_corrupt(rows, sid, column, case)
    kind = CORRUPT[case]
    article = "an" if kind == "unreadable" else "a"

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        rows.get(sid)

    assert [r.getMessage() for r in caplog.records] == [
        f"schedule {sid} has {article} {kind} {column}; ignoring"
    ]


def test_values_at_the_depth_limit_are_read_as_written(rows):
    sid = rows.add()
    stamp = "2026-01-01T00:00:00.000000+00:00"
    rows.set(sid, "last_run_at", stamp)
    # The envelope adds one level above ``result``.
    rows.set(sid, "last_run_result", _result_envelope(stamp, nested(LIMIT - 1)))
    rows.set(sid, "job_state", json.dumps(nested(LIMIT)))
    rows.set(sid, "params_json", json.dumps(nested(LIMIT)))

    got = rows.get(sid)

    assert got["last_run_result"] == nested(LIMIT - 1)
    assert got["state"] == nested(LIMIT)
    assert (got["params"], got["params_error"]) == (nested(LIMIT), None)


def test_a_parser_that_gives_up_on_params_is_a_params_error(rows, monkeypatch):
    """``RecursionError`` is unreadable on every interpreter, not only 3.12 and 3.13.

    3.14 parses 100,000 levels, so the parser is replaced for the stored text.
    """
    stored = '{"max_age_days": 30}'
    sid = rows.add("importance_decay")
    rows.set(sid, "params_json", stored)
    parse = json.loads

    def give_up_on(raw, *args, **kwargs):
        if raw == stored:
            raise RecursionError("maximum recursion depth exceeded")
        return parse(raw, *args, **kwargs)

    monkeypatch.setattr(json, "loads", give_up_on)

    got = rows.get(sid)

    assert got["params"] is None
    assert got["params_error"] == (
        "unreadable params_json (RecursionError: maximum recursion depth exceeded)"
    )


# ── params_json: never the defaults ──────────────────────────────────

UNUSABLE_PARAMS = {
    "": "unreadable params_json (JSONDecodeError: ",
    "null": "malformed params_json (a JSON NoneType, not an object)",
    "[]": "malformed params_json (a JSON list, not an object)",
    "{not json": "unreadable params_json (JSONDecodeError: ",
    json.dumps(nested(LIMIT + 1)): f"malformed params_json (nested deeper than {LIMIT} levels)",
}


@pytest.mark.parametrize("stored", list(UNUSABLE_PARAMS), ids=lambda s: repr(s[:12]))
def test_unusable_params_are_an_error_on_the_row(rows, caplog, stored):
    sid = rows.add("importance_decay", {"max_age_days": 30})
    rows.set(sid, "params_json", stored)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        got = rows.get(sid)

    assert got["params"] is None
    assert got["params_error"].startswith(UNUSABLE_PARAMS[stored]), got["params_error"]
    assert [r.getMessage() for r in caplog.records] == [
        f"schedule {sid} has {got['params_error']}; its runs record an error"
    ]


SURFACES = ["dispatcher", "mcp", "cli"]


@pytest.fixture
def run_through(components, monkeypatch):
    """Run a schedule through one of the three paths; record runner calls."""
    app = AppContext.from_components(components)

    @asynccontextmanager
    async def fake_components():
        yield components

    monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake_components)
    monkeypatch.setattr(schedule_tools, "_get_app_initialized", AsyncMock(return_value=app))
    monkeypatch.setattr(schedule_tools, "_run_now_timeout", lambda app: 5.0)
    calls: list[dict] = []

    async def spy(app, **kwargs):
        calls.append(kwargs)
        return {"deleted_chunks": 0}

    spec = JOB_KINDS["importance_decay"]
    monkeypatch.setitem(JOB_KINDS, "importance_decay", dataclasses.replace(spec, runner=spy))

    def run(surface: str, sid: str):
        if surface == "dispatcher":

            async def go():
                sched = await components.storage.schedule_get(sid)
                watchdog = HealthWatchdog(
                    app,
                    HealthWatchdogConfig(enabled=True),
                    SchedulerConfig(enabled=True, runner_timeout_seconds=5.0),
                )
                await watchdog._run_schedule(sched, 5.0)

            asyncio.run(go())
            return None
        if surface == "mcp":
            return json.loads(asyncio.run(schedule_tools.mem_schedule_run_now(sid)))
        return CliRunner().invoke(cli, ["schedule", "run-now", sid])

    run.calls = calls  # type: ignore[attr-defined]
    run.app = app  # type: ignore[attr-defined]
    return run


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("stored", ["", "null", "[]", "{not json"], ids=repr)
def test_no_run_path_runs_unusable_params_with_the_defaults(rows, run_through, surface, stored):
    sid = rows.add("importance_decay", {"max_age_days": 30})
    rows.set(sid, "params_json", stored)
    reason = rows.get(sid)["params_error"]

    returned = run_through(surface, sid)

    assert run_through.calls == []
    after = rows.get(sid)
    assert after["last_run_status"] == "error"
    # The decoder's reason, not pydantic's complaint about ``None``.
    assert after["last_run_error"] == f"invalid params: {reason}"
    if surface == "mcp":
        assert returned == {"ok": False, "reason": f"invalid params: {reason}"}
    elif surface == "cli":
        assert returned.exit_code != 0
        assert f"invalid params: {reason}" in returned.output


def test_the_reason_is_in_the_json_listing_and_the_plain_one_shows_the_error(rows, run_through):
    sid = rows.add("importance_decay", {"max_age_days": 30})
    rows.set(sid, "params_json", "[]")
    run_through("dispatcher", sid)
    reason = "malformed params_json (a JSON list, not an object)"

    plain = CliRunner().invoke(cli, ["schedule", "list"])
    as_json = CliRunner().invoke(cli, ["schedule", "list", "--json"])

    assert plain.exit_code == 0, plain.output
    (line,) = [ln for ln in plain.output.splitlines() if sid in ln]
    assert line.endswith("(error)"), line
    (row,) = [r for r in json.loads(as_json.stdout) if r["id"] == sid]
    assert (row["params"], row["params_error"]) == (None, reason)
    assert row["last_run_error"] == f"invalid params: {reason}"


@pytest.mark.parametrize("surface", SURFACES)
def test_readable_params_still_run(rows, run_through, surface):
    sid = rows.add("importance_decay", {"max_age_days": 30})

    run_through(surface, sid)

    assert run_through.calls == [{"max_age_days": 30.0, "source_filter": None}]
    assert rows.get(sid)["last_run_status"] == "ok"


def test_the_dispatcher_runs_the_healthy_row_beside_an_unusable_one(rows, run_through):
    bad = rows.add("importance_decay", {"max_age_days": 30})
    good = rows.add("importance_decay", {"max_age_days": 7})
    rows.set(bad, "params_json", "{not json")
    for sid in (bad, good):  # due now: a slot has passed since creation
        rows.set(sid, "created_at", "2026-01-01T00:00:00+00:00")

    async def tick():
        watchdog = HealthWatchdog(
            run_through.app,
            HealthWatchdogConfig(enabled=True),
            SchedulerConfig(enabled=True, runner_timeout_seconds=5.0),
        )
        await watchdog._dispatch_schedules()

    asyncio.run(tick())

    assert run_through.calls == [{"max_age_days": 7.0, "source_filter": None}]
    assert rows.get(good)["last_run_status"] == "ok"
    assert rows.get(bad)["last_run_status"] == "error"
