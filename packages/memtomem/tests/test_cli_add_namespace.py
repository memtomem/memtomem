"""Tests for ``mm add``'s write namespace (#1991).

``mm session start --agent-id planner`` announces
``Namespace: agent-runtime:planner``; before #1991 the following
``mm add`` wrote to ``default`` anyway, so the announced isolation did not
exist. These pin the CLI half of the routing contract that
``server.tools.multi_agent._resolve_agent_namespace`` defines for MCP:
explicit namespace wins, else the active session's *bound* agent, else
un-pinned (the engine's namespace rules decide).

The degradation cases matter as much as the happy path — a stale state
file must not make ``mm add`` fail or route somewhere unexpected.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner
from helpers import set_home

from memtomem import privacy
from memtomem.cli._session_state import _write_current_session
from memtomem.cli.memory import add as add_cmd
from memtomem.constants import INVALID_NAMESPACE_MESSAGE_PREFIX

_CLEAN = "planner private note: roadmap draft v2"
_SESSION_ID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture(autouse=True)
def _reset_privacy_counters():
    privacy.reset_for_tests()
    yield
    privacy.reset_for_tests()


@pytest.fixture
def home(monkeypatch, tmp_path) -> Path:
    """Isolated HOME — the state file this suite writes must never be the
    developer's own ``~/.memtomem/.current_session``."""
    h = tmp_path / "home"
    h.mkdir()
    set_home(monkeypatch, h)
    return h


def _components(tmp_path: Path, session_row: dict | None = None) -> SimpleNamespace:
    """Components double: index engine recording its ``namespace=`` kwarg,
    storage answering ``get_session`` with *session_row* (``None`` = no such
    session)."""
    return SimpleNamespace(
        config=SimpleNamespace(
            indexing=SimpleNamespace(
                memory_dirs=[str(tmp_path / "memories")],
                project_memory_dirs=[],
            )
        ),
        index_engine=SimpleNamespace(
            index_file=AsyncMock(return_value=SimpleNamespace(indexed_chunks=1))
        ),
        storage=SimpleNamespace(
            list_chunks_by_source=AsyncMock(return_value=[]),
            get_session=AsyncMock(return_value=session_row),
        ),
    )


def _patch_components(monkeypatch: pytest.MonkeyPatch, comp: SimpleNamespace) -> None:
    @asynccontextmanager
    async def fake():
        yield comp

    monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake)
    monkeypatch.setattr(
        "memtomem.server.tools.search._resolve_project_context_root", lambda comp: None
    )


def _row(agent_id: str, *, ended: bool = False, namespace: str | None = None) -> dict:
    """A session row. *namespace* defaults to what ``mm session start``
    would derive; pass it explicitly to model ``start --namespace``, whose
    value labels the record without steering writes."""
    return {
        "id": _SESSION_ID,
        "agent_id": agent_id,
        "started_at": "2026-08-02T00:00:00Z",
        "ended_at": "2026-08-02T01:00:00Z" if ended else None,
        "summary": None,
        "namespace": namespace or f"agent-runtime:{agent_id}",
        "metadata": {},
    }


def _indexed_namespace(comp: SimpleNamespace) -> str | None:
    """The ``namespace=`` the write actually reached the engine with."""
    return comp.index_engine.index_file.await_args.kwargs["namespace"]


class TestSessionInheritance:
    def test_active_session_routes_to_agent_namespace(self, monkeypatch, tmp_path, home):
        comp = _components(tmp_path, _row("planner"))
        _patch_components(monkeypatch, comp)
        _write_current_session(_SESSION_ID)

        result = CliRunner().invoke(add_cmd, [_CLEAN])

        assert result.exit_code == 0, result.output
        assert _indexed_namespace(comp) == "agent-runtime:planner"
        # The redirect is visible: a plain ``mm search`` hides this namespace.
        assert "Namespace: agent-runtime:planner" in result.output
        comp.storage.get_session.assert_awaited_once_with(_SESSION_ID)

    def test_default_agent_session_stays_unpinned(self, monkeypatch, tmp_path, home):
        # The ``"default"`` sentinel binds no agent (#1875), so a session
        # started without ``--agent-id`` must not capture writes.
        comp = _components(tmp_path, _row("default"))
        _patch_components(monkeypatch, comp)
        _write_current_session(_SESSION_ID)

        result = CliRunner().invoke(add_cmd, [_CLEAN])

        assert result.exit_code == 0, result.output
        assert _indexed_namespace(comp) is None
        assert "Namespace:" not in result.output

    def test_routes_by_bound_agent_not_the_rows_namespace_column(self, monkeypatch, tmp_path, home):
        # `mm session start --agent-id planner --namespace team-notes`
        # re-points the session *record* only. MCP routes by the bound
        # agent, and the CLI must not diverge.
        comp = _components(tmp_path, _row("planner", namespace="team-notes"))
        _patch_components(monkeypatch, comp)
        _write_current_session(_SESSION_ID)

        result = CliRunner().invoke(add_cmd, [_CLEAN])

        assert result.exit_code == 0, result.output
        assert _indexed_namespace(comp) == "agent-runtime:planner"

    def test_unbound_session_namespace_does_not_capture_writes(self, monkeypatch, tmp_path, home):
        # The same shape with no agent bound: the label must not become a
        # write destination — the write stays un-pinned.
        comp = _components(tmp_path, _row("default", namespace="team-notes"))
        _patch_components(monkeypatch, comp)
        _write_current_session(_SESSION_ID)

        result = CliRunner().invoke(add_cmd, [_CLEAN])

        assert result.exit_code == 0, result.output
        assert _indexed_namespace(comp) is None

    def test_no_session_file_stays_unpinned(self, monkeypatch, tmp_path, home):
        comp = _components(tmp_path, _row("planner"))
        _patch_components(monkeypatch, comp)

        result = CliRunner().invoke(add_cmd, [_CLEAN])

        assert result.exit_code == 0, result.output
        assert _indexed_namespace(comp) is None
        comp.storage.get_session.assert_not_awaited()


class TestStaleSessionState:
    def test_ended_session_stays_unpinned_and_keeps_state_file(self, monkeypatch, tmp_path, home):
        # ``mm session end`` clears the state file, but ``--auto-end-stale``
        # ends rows without touching it — the file can outlive its session.
        comp = _components(tmp_path, _row("planner", ended=True))
        _patch_components(monkeypatch, comp)
        _write_current_session(_SESSION_ID)

        result = CliRunner().invoke(add_cmd, [_CLEAN])

        assert result.exit_code == 0, result.output
        assert _indexed_namespace(comp) is None
        # Read path: resolving must not clean up state it does not own.
        assert (home / ".memtomem" / ".current_session").exists()

    def test_missing_session_row_stays_unpinned(self, monkeypatch, tmp_path, home):
        comp = _components(tmp_path, None)
        _patch_components(monkeypatch, comp)
        _write_current_session(_SESSION_ID)

        result = CliRunner().invoke(add_cmd, [_CLEAN])

        assert result.exit_code == 0, result.output
        assert _indexed_namespace(comp) is None

    def test_corrupted_agent_id_degrades_instead_of_failing(self, monkeypatch, tmp_path, home):
        # A row edited out of band must cost the write its agent scope,
        # not the write itself.
        comp = _components(tmp_path, _row("foo:bar"))
        _patch_components(monkeypatch, comp)
        _write_current_session(_SESSION_ID)

        result = CliRunner().invoke(add_cmd, [_CLEAN])

        assert result.exit_code == 0, result.output
        assert _indexed_namespace(comp) is None


class TestNamespaceFlag:
    def test_flag_overrides_active_session(self, monkeypatch, tmp_path, home):
        comp = _components(tmp_path, _row("planner"))
        _patch_components(monkeypatch, comp)
        _write_current_session(_SESSION_ID)

        result = CliRunner().invoke(add_cmd, [_CLEAN, "--namespace", "shared"])

        assert result.exit_code == 0, result.output
        assert _indexed_namespace(comp) == "shared"

    def test_short_flag_without_session(self, monkeypatch, tmp_path, home):
        comp = _components(tmp_path, None)
        _patch_components(monkeypatch, comp)

        result = CliRunner().invoke(add_cmd, [_CLEAN, "-n", "shared"])

        assert result.exit_code == 0, result.output
        assert _indexed_namespace(comp) == "shared"

    def test_invalid_namespace_rejected(self, monkeypatch, tmp_path, home):
        comp = _components(tmp_path, None)
        _patch_components(monkeypatch, comp)

        result = CliRunner().invoke(add_cmd, [_CLEAN, "--namespace", "a,b"])

        assert result.exit_code != 0
        assert INVALID_NAMESPACE_MESSAGE_PREFIX in result.output
        comp.index_engine.index_file.assert_not_awaited()

    def test_invalid_namespace_json_ack(self, monkeypatch, tmp_path, home):
        comp = _components(tmp_path, None)
        _patch_components(monkeypatch, comp)

        result = CliRunner().invoke(add_cmd, [_CLEAN, "--namespace", "a,b", "--json"])

        assert result.exit_code != 0
        data = json.loads(result.stdout)
        assert data["ok"] is False
        assert INVALID_NAMESPACE_MESSAGE_PREFIX in data["reason"]


class TestJsonAck:
    def test_ack_reports_inherited_namespace(self, monkeypatch, tmp_path, home):
        comp = _components(tmp_path, _row("planner"))
        _patch_components(monkeypatch, comp)
        _write_current_session(_SESSION_ID)

        result = CliRunner().invoke(add_cmd, [_CLEAN, "--json"])

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["namespace"] == "agent-runtime:planner"

    def test_ack_reports_null_when_unpinned(self, monkeypatch, tmp_path, home):
        comp = _components(tmp_path, None)
        _patch_components(monkeypatch, comp)

        result = CliRunner().invoke(add_cmd, [_CLEAN, "--json"])

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["namespace"] is None
