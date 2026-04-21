"""Tests for ``mm session list/events --json`` scripting output (#331) and
``mm activity log --json`` ack output (#335)."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner

from memtomem.cli import cli


def _mock_components(*, sessions=None, events=None, add_event=None):
    storage = SimpleNamespace(
        list_sessions=AsyncMock(return_value=list(sessions or [])),
        get_session_events=AsyncMock(return_value=list(events or [])),
        add_session_event=add_event if add_event is not None else AsyncMock(return_value=None),
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


class TestActivityLogJson:
    """``mm activity log --json`` is write-side, so the ack shape uses an
    explicit ``ok`` discriminator (the success payload has no natural
    disambiguator like ``events: [...]``). Error shape intentionally diverges
    from ``session events --json``'s ``{"error": ...}`` for that reason —
    see commit / PR body."""

    def test_success_emits_ok_ack(self, runner, monkeypatch):
        comp = _mock_components()
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        monkeypatch.setattr("memtomem.cli.session_cmd._read_current_session", lambda: "sess-1")

        result = runner.invoke(
            cli, ["activity", "log", "--type", "tool_call", "-c", "ran tests", "--json"]
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data == {"ok": True, "session_id": "sess-1", "event_type": "tool_call"}
        comp.storage.add_session_event.assert_awaited_once()

    def test_no_active_session_emits_skip_ack(self, runner, monkeypatch):
        """With --json and no active session, emit a parseable skip ack on
        stdout (exit 0). Storage must not be touched."""
        comp = _mock_components()
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        monkeypatch.setattr("memtomem.cli.session_cmd._read_current_session", lambda: None)

        result = runner.invoke(cli, ["activity", "log", "-c", "x", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data == {"ok": False, "reason": "no_active_session"}
        comp.storage.add_session_event.assert_not_awaited()

    def test_write_failure_emits_error_ack(self, runner, monkeypatch):
        """Storage exceptions are swallowed (hooks must not fail) but --json
        surfaces them as ``{ok: false, reason: write_failed}``."""
        failing_add = AsyncMock(side_effect=RuntimeError("db is locked"))
        comp = _mock_components(add_event=failing_add)
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        monkeypatch.setattr("memtomem.cli.session_cmd._read_current_session", lambda: "sess-2")
        # Silence logger.warning so the traceback doesn't bleed into CliRunner
        # output and break the JSON parse. Hook-silent contract is already
        # covered by test_text_path_silent_on_success.
        monkeypatch.setattr("memtomem.cli.session_cmd.logger.warning", lambda *a, **kw: None)

        result = runner.invoke(cli, ["activity", "log", "-c", "boom", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data == {"ok": False, "reason": "write_failed"}

    def test_text_path_silent_on_success(self, runner, monkeypatch):
        """Without --json the silent contract is preserved — no stdout, exit 0."""
        comp = _mock_components()
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        monkeypatch.setattr("memtomem.cli.session_cmd._read_current_session", lambda: "sess-3")

        result = runner.invoke(cli, ["activity", "log", "-c", "quiet"])
        assert result.exit_code == 0
        assert result.output == ""
        comp.storage.add_session_event.assert_awaited_once()

    def test_text_path_silent_on_no_session(self, runner, monkeypatch):
        """Without --json, the no-session path is silent — hook callers rely
        on this contract and must not see stray stdout."""
        monkeypatch.setattr("memtomem.cli.session_cmd._read_current_session", lambda: None)

        result = runner.invoke(cli, ["activity", "log", "-c", "quiet"])
        assert result.exit_code == 0
        assert result.output == ""
