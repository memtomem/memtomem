"""Tests for ``mm session list/events --json`` scripting output (#331)."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner

from memtomem.cli import cli


def _mock_components(*, sessions=None, events=None):
    storage = SimpleNamespace(
        list_sessions=AsyncMock(return_value=list(sessions or [])),
        get_session_events=AsyncMock(return_value=list(events or [])),
    )
    return SimpleNamespace(storage=storage)


def _patched_cli_components(comp):
    @asynccontextmanager
    async def fake():
        yield comp

    return fake


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


class TestSessionListJson:
    def test_happy_path(self, runner, monkeypatch):
        comp = _mock_components(
            sessions=[
                {
                    "id": "sess-1",
                    "agent_id": "claude",
                    "started_at": "2026-04-21T12:00:00",
                    "ended_at": "2026-04-21T13:00:00",
                },
                {
                    "id": "sess-2",
                    "agent_id": "codex",
                    "started_at": "2026-04-21T14:00:00",
                    "ended_at": None,
                },
            ]
        )
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = runner.invoke(cli, ["session", "list", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["count"] == 2
        assert [s["id"] for s in data["sessions"]] == ["sess-1", "sess-2"]
        assert data["sessions"][0]["status"] == "ended"
        assert data["sessions"][1]["status"] == "active"

    def test_empty(self, runner, monkeypatch):
        comp = _mock_components(sessions=[])
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = runner.invoke(cli, ["session", "list", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data == {"sessions": [], "count": 0}


class TestSessionEventsJson:
    def test_happy_path(self, runner, monkeypatch):
        comp = _mock_components(
            events=[
                {
                    "created_at": "2026-04-21T12:00:00",
                    "event_type": "tool_call",
                    "content": "ran tests",
                },
            ]
        )
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = runner.invoke(cli, ["session", "events", "sess-1", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["session_id"] == "sess-1"
        assert data["count"] == 1
        assert data["events"][0]["event_type"] == "tool_call"

    def test_empty(self, runner, monkeypatch):
        comp = _mock_components(events=[])
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = runner.invoke(cli, ["session", "events", "sess-empty", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data == {"session_id": "sess-empty", "events": [], "count": 0}

    def test_no_session_returns_error_shape(self, runner, monkeypatch):
        """With --json and no session_id argument + no active session, emit a
        parseable error shape on stdout (exit 0) instead of the text-path
        ClickException. Lets ``mm session events --json | jq`` degrade
        gracefully when nothing is active."""
        monkeypatch.setattr("memtomem.cli.session_cmd._read_current_session", lambda: None)

        result = runner.invoke(cli, ["session", "events", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data == {"error": "no_session"}

    def test_no_session_text_path_unchanged(self, runner, monkeypatch):
        """Without --json, the no-session path still raises ClickException so
        existing text callers aren't silently degraded."""
        monkeypatch.setattr("memtomem.cli.session_cmd._read_current_session", lambda: None)

        result = runner.invoke(cli, ["session", "events"])
        assert result.exit_code != 0
        assert "No session ID provided" in result.output
