"""Tests for ``mm schedule`` CLI (P2 cron Phase A.4)."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner

from memtomem.cli import cli


def _patched_cli_components(comp):
    @asynccontextmanager
    async def fake():
        yield comp

    return fake


def _mock_components(*, schedules=None, insert_id="abc123", delete_ok=True):
    rows = list(schedules or [])
    storage = SimpleNamespace(
        schedule_insert=AsyncMock(return_value=insert_id),
        schedule_list_all=AsyncMock(return_value=rows),
        schedule_delete=AsyncMock(return_value=delete_ok),
    )
    return SimpleNamespace(storage=storage, config=SimpleNamespace(scheduler=None))


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


class TestScheduleAdd:
    def test_happy_path(self, runner, monkeypatch):
        comp = _mock_components(insert_id="sch-001")
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        result = runner.invoke(
            cli, ["schedule", "add", "--cron", "0 3 * * 0", "--job", "compaction"]
        )
        assert result.exit_code == 0, result.output
        assert "sch-001" in result.output
        comp.storage.schedule_insert.assert_awaited_once_with("0 3 * * 0", "compaction", {})

    def test_invalid_cron_rejected(self, runner, monkeypatch):
        comp = _mock_components()
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        result = runner.invoke(
            cli, ["schedule", "add", "--cron", "not a cron", "--job", "compaction"]
        )
        assert result.exit_code != 0
        assert "invalid cron" in result.output.lower()
        comp.storage.schedule_insert.assert_not_awaited()

    def test_unknown_job_rejected(self, runner):
        # click.Choice rejects pre-bootstrap, so no monkeypatch needed.
        result = runner.invoke(
            cli, ["schedule", "add", "--cron", "* * * * *", "--job", "no_such_job"]
        )
        assert result.exit_code != 0
        assert "no_such_job" in result.output or "Invalid value" in result.output

    def test_invalid_params_json_rejected(self, runner, monkeypatch):
        comp = _mock_components()
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        result = runner.invoke(
            cli,
            [
                "schedule",
                "add",
                "--cron",
                "* * * * *",
                "--job",
                "importance_decay",
                "--params",
                "{not-json",
            ],
        )
        assert result.exit_code != 0
        comp.storage.schedule_insert.assert_not_awaited()


class TestScheduleList:
    def test_empty(self, runner, monkeypatch):
        comp = _mock_components(schedules=[])
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        result = runner.invoke(cli, ["schedule", "list"])
        assert result.exit_code == 0
        assert "No schedules" in result.output

    def test_json_output(self, runner, monkeypatch):
        rows = [
            {
                "id": "s1",
                "cron_expr": "0 3 * * 0",
                "job_kind": "compaction",
                "params": {},
                "enabled": True,
                "created_at": "2026-04-28T00:00:00+00:00",
                "last_run_at": None,
                "last_run_status": None,
                "last_run_error": None,
            }
        ]
        comp = _mock_components(schedules=rows)
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        result = runner.invoke(cli, ["schedule", "list", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data[0]["id"] == "s1"


class TestScheduleDelete:
    def test_happy_path(self, runner, monkeypatch):
        comp = _mock_components(delete_ok=True)
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        result = runner.invoke(cli, ["schedule", "delete", "s1"])
        assert result.exit_code == 0
        comp.storage.schedule_delete.assert_awaited_once_with("s1")

    def test_not_found(self, runner, monkeypatch):
        comp = _mock_components(delete_ok=False)
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        result = runner.invoke(cli, ["schedule", "delete", "nope"])
        assert result.exit_code != 0
        assert "not found" in result.output.lower()


class TestScheduleCliIntegration:
    """End-to-end CLI ↔ real SQLite storage smoke (per
    ``feedback_mocked_storage_hides_sql_bugs``)."""

    def test_add_list_delete_roundtrip(self, runner, components, monkeypatch):
        monkeypatch.setattr(
            "memtomem.cli._bootstrap.cli_components", _patched_cli_components(components)
        )

        added = runner.invoke(
            cli, ["schedule", "add", "--cron", "0 3 * * 0", "--job", "compaction"]
        )
        assert added.exit_code == 0, added.output
        sched_id = added.output.strip().split()[1]

        listed = runner.invoke(cli, ["schedule", "list", "--json"])
        assert listed.exit_code == 0
        rows = json.loads(listed.output)
        assert any(r["id"] == sched_id and r["job_kind"] == "compaction" for r in rows)

        deleted = runner.invoke(cli, ["schedule", "delete", sched_id])
        assert deleted.exit_code == 0

        after = runner.invoke(cli, ["schedule", "list", "--json"])
        assert all(r["id"] != sched_id for r in json.loads(after.output))
