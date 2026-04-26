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


def _create_session_call_args(mock: AsyncMock) -> tuple:
    """Return ``create_session`` positional args, tolerating future kwargs.

    The storage signature is ``create_session(session_id, agent_id,
    namespace, metadata)``. If the call site ever switches any field to a
    keyword, this helper merges kwargs back into the positional tuple so
    tests stay aligned with the contract instead of crashing on unpacking.
    """
    call = mock.await_args
    if call is None:  # pragma: no cover — defensive
        raise AssertionError("create_session was not awaited")
    keys = ("session_id", "agent_id", "namespace", "metadata")
    merged = list(call.args) + [None] * (len(keys) - len(call.args))
    for idx, key in enumerate(keys):
        if key in call.kwargs:
            merged[idx] = call.kwargs[key]
    return tuple(merged)


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
    from ``session events --json``'s ``{"error": ...}`` — see the "JSON error
    shape" subsection of ``CONTRIBUTING.md`` for the read/write rule. If you
    change either this class's shape or ``TestSessionEventsJson``'s, update
    CONTRIBUTING.md in the same PR or the docs and tests will drift."""

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

    def test_invalid_meta_emits_invalid_meta_ack(self, runner, monkeypatch):
        """Malformed --meta under --json emits {ok: false, reason:
        invalid_meta} (exit 0). Storage must not be touched — the parse
        error happens before the async call."""
        comp = _mock_components()
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        monkeypatch.setattr("memtomem.cli.session_cmd._read_current_session", lambda: "sess-1")

        result = runner.invoke(cli, ["activity", "log", "-c", "x", "--meta", "{oops", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data == {"ok": False, "reason": "invalid_meta"}
        comp.storage.add_session_event.assert_not_awaited()

    def test_invalid_meta_text_path_bubbles(self, runner, monkeypatch):
        """Malformed --meta without --json lets the JSONDecodeError bubble to
        Click so a hook author mistyping meta sees the traceback.
        Intentional per issue #338 — the silent-by-default hook contract is
        about the *write* failure mode, not programmer input errors."""
        comp = _mock_components()
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        monkeypatch.setattr("memtomem.cli.session_cmd._read_current_session", lambda: "sess-2")

        result = runner.invoke(cli, ["activity", "log", "-c", "x", "--meta", "{oops"])
        assert result.exit_code != 0
        assert isinstance(result.exception, json.JSONDecodeError)
        comp.storage.add_session_event.assert_not_awaited()


class TestSessionStartNamespaceDerivation:
    """``mm session start --agent-id <id>`` derives ``agent-runtime:<id>``,
    mirroring ``mem_session_start`` MCP behavior (PR #475). Until this fix
    the CLI silently lost ``--agent-id`` for namespace derivation, leaving
    sessions in ``default`` despite the multi-agent contract advertised on
    the public page."""

    @staticmethod
    def _comp_with_create_spy() -> tuple[SimpleNamespace, AsyncMock]:
        create_session = AsyncMock(return_value=None)
        comp = SimpleNamespace(storage=SimpleNamespace(create_session=create_session))
        return comp, create_session

    def test_default_agent_lands_in_default_ns(self, runner, monkeypatch):
        comp, create_session = self._comp_with_create_spy()
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        monkeypatch.setattr(
            "memtomem.cli.session_cmd._write_current_session", lambda _session_id: None
        )

        result = runner.invoke(cli, ["session", "start"])
        assert result.exit_code == 0, result.output
        create_session.assert_awaited_once()
        call_args = _create_session_call_args(create_session)
        agent_id, ns = call_args[1], call_args[2]
        assert agent_id == "default"
        assert ns == "default"
        assert "Namespace: default" in result.output

    def test_agent_id_derives_agent_runtime_namespace(self, runner, monkeypatch):
        comp, create_session = self._comp_with_create_spy()
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        monkeypatch.setattr(
            "memtomem.cli.session_cmd._write_current_session", lambda _session_id: None
        )

        result = runner.invoke(cli, ["session", "start", "--agent-id", "planner"])
        assert result.exit_code == 0, result.output
        call_args = _create_session_call_args(create_session)
        agent_id, ns = call_args[1], call_args[2]
        assert agent_id == "planner"
        assert ns == "agent-runtime:planner"
        assert "Namespace: agent-runtime:planner" in result.output

    def test_explicit_namespace_overrides_agent_id(self, runner, monkeypatch):
        comp, create_session = self._comp_with_create_spy()
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        monkeypatch.setattr(
            "memtomem.cli.session_cmd._write_current_session", lambda _session_id: None
        )

        result = runner.invoke(
            cli,
            ["session", "start", "--agent-id", "planner", "--namespace", "custom"],
        )
        assert result.exit_code == 0, result.output
        call_args = _create_session_call_args(create_session)
        agent_id, ns = call_args[1], call_args[2]
        assert agent_id == "planner"
        assert ns == "custom"
        assert "Namespace: custom" in result.output


class TestSessionWrapNamespaceDerivation:
    """``mm session wrap --agent-id <id>`` derives the same
    ``agent-runtime:<id>`` namespace as ``mm session start``. Without this,
    the CLI fix would close half of gap G2 — `wrap` is the headless surface
    most users hit when invoking sub-agents (`mm session wrap -- claude -p
    "..."`), and it had a hard-coded ``"default"`` namespace regardless of
    ``--agent-id``."""

    @staticmethod
    def _comp_with_storage_spies() -> tuple[SimpleNamespace, AsyncMock]:
        create_session = AsyncMock(return_value=None)
        end_session = AsyncMock(return_value=None)
        get_session_events = AsyncMock(return_value=[])
        comp = SimpleNamespace(
            storage=SimpleNamespace(
                create_session=create_session,
                end_session=end_session,
                get_session_events=get_session_events,
            )
        )
        return comp, create_session

    @staticmethod
    def _patch_runtime(monkeypatch, comp):
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        monkeypatch.setattr("memtomem.cli.session_cmd._write_current_session", lambda _id: None)
        monkeypatch.setattr("memtomem.cli.session_cmd._clear_current_session", lambda: None)
        # Avoid spawning the wrapped subprocess.
        monkeypatch.setattr(
            "subprocess.run",
            lambda *_args, **_kwargs: SimpleNamespace(returncode=0),
        )

    def test_wrap_default_headless_derives_agent_runtime_namespace(self, runner, monkeypatch):
        comp, create_session = self._comp_with_storage_spies()
        self._patch_runtime(monkeypatch, comp)

        result = runner.invoke(cli, ["session", "wrap", "--", "true"])
        assert result.exit_code == 0, result.output
        call_args = _create_session_call_args(create_session)
        assert call_args[1] == "headless"
        assert call_args[2] == "agent-runtime:headless"

    def test_wrap_explicit_agent_id_derives_namespace(self, runner, monkeypatch):
        comp, create_session = self._comp_with_storage_spies()
        self._patch_runtime(monkeypatch, comp)

        result = runner.invoke(cli, ["session", "wrap", "--agent-id", "planner", "--", "true"])
        assert result.exit_code == 0, result.output
        call_args = _create_session_call_args(create_session)
        assert call_args[1] == "planner"
        assert call_args[2] == "agent-runtime:planner"

    def test_wrap_default_token_lands_in_default_ns(self, runner, monkeypatch):
        """Edge case: explicit ``--agent-id default`` (the legacy reserved
        token) falls through to the ``default`` namespace, matching the
        contract on `mem_session_start` (only non-default agent_ids derive
        ``agent-runtime:*``)."""
        comp, create_session = self._comp_with_storage_spies()
        self._patch_runtime(monkeypatch, comp)

        result = runner.invoke(cli, ["session", "wrap", "--agent-id", "default", "--", "true"])
        assert result.exit_code == 0, result.output
        call_args = _create_session_call_args(create_session)
        assert call_args[1] == "default"
        assert call_args[2] == "default"
