"""Tests for ``mm agent`` (migrate / register / list / share / debug-resolve)."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

from click.testing import CliRunner

from memtomem.cli import cli


def _mock_components(legacy_namespaces, existing_new_namespaces=()):
    """Storage stub: ``list_namespaces`` returns the given namespace set.

    Each entry is ``(namespace, chunk_count)`` — mirrors the real
    ``NamespaceOps.list_namespaces`` shape.  ``rename_namespace`` returns a
    fixed count so the CLI sees non-zero chunk updates.
    """
    pairs = [(ns, 2) for ns in legacy_namespaces]
    pairs.extend((ns, 1) for ns in existing_new_namespaces)
    storage = SimpleNamespace(
        list_namespaces=AsyncMock(return_value=pairs),
        rename_namespace=AsyncMock(return_value=2),
    )
    return SimpleNamespace(storage=storage)


def _patched_cli_components(comp):
    @asynccontextmanager
    async def fake():
        yield comp

    return fake


class TestAgentMigrate:
    def test_no_legacy_namespaces_nothing_to_do(self, monkeypatch):
        comp = _mock_components([])
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        result = CliRunner().invoke(cli, ["agent", "migrate"])
        assert result.exit_code == 0
        assert "Nothing to migrate" in result.output
        comp.storage.rename_namespace.assert_not_awaited()

    def test_dry_run_lists_without_renaming(self, monkeypatch):
        comp = _mock_components(["agent/alpha", "agent/beta"])
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        result = CliRunner().invoke(cli, ["agent", "migrate", "--dry-run"])
        assert result.exit_code == 0
        assert "agent/alpha  ->  agent-runtime:alpha" in result.output
        assert "agent/beta  ->  agent-runtime:beta" in result.output
        assert "dry-run" in result.output
        comp.storage.rename_namespace.assert_not_awaited()

    def test_apply_renames_each_namespace(self, monkeypatch):
        comp = _mock_components(["agent/alpha", "agent/beta"])
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        result = CliRunner().invoke(cli, ["agent", "migrate"])
        assert result.exit_code == 0
        assert comp.storage.rename_namespace.await_count == 2
        comp.storage.rename_namespace.assert_any_await("agent/alpha", "agent-runtime:alpha")
        comp.storage.rename_namespace.assert_any_await("agent/beta", "agent-runtime:beta")
        assert "Migration complete" in result.output

    def test_ignores_already_migrated_namespaces(self, monkeypatch):
        comp = _mock_components(
            legacy_namespaces=[],
            existing_new_namespaces=["agent-runtime:alpha", "claude-memory:x"],
        )
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        result = CliRunner().invoke(cli, ["agent", "migrate"])
        assert result.exit_code == 0
        assert "Nothing to migrate" in result.output
        comp.storage.rename_namespace.assert_not_awaited()


def _registry_components(
    namespaces: list[tuple[str, int]] | None = None,
    namespace_meta: list[dict] | None = None,
    shared_meta: dict | None = None,
):
    """Storage stub for the register/list path.

    ``list_namespaces`` returns ``(namespace, count)`` pairs and
    ``list_namespace_meta`` returns the agent meta records. ``get_namespace_meta``
    returns ``shared_meta`` when queried for ``"shared"`` (the only key the
    list command looks up explicitly), and ``None`` otherwise.
    """
    namespaces = namespaces or []
    namespace_meta = namespace_meta or []

    async def _get_meta(ns: str) -> dict | None:
        if ns == "shared":
            return shared_meta
        return None

    storage = SimpleNamespace(
        list_namespaces=AsyncMock(return_value=namespaces),
        list_namespace_meta=AsyncMock(return_value=namespace_meta),
        get_namespace_meta=AsyncMock(side_effect=_get_meta),
        set_namespace_meta=AsyncMock(return_value=None),
    )
    return SimpleNamespace(storage=storage)


class TestAgentRegister:
    def test_register_creates_namespace_and_shared(self, monkeypatch):
        comp = _registry_components(shared_meta=None)
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = CliRunner().invoke(
            cli, ["agent", "register", "planner", "--description", "the planning agent"]
        )

        assert result.exit_code == 0, result.output
        assert "Agent registered: planner" in result.output
        assert "agent-runtime:planner" in result.output
        # Both the agent NS and shared NS were upserted (shared was missing)
        assert comp.storage.set_namespace_meta.await_count == 2
        first_call = comp.storage.set_namespace_meta.await_args_list[0]
        assert first_call.args[0] == "agent-runtime:planner"
        assert first_call.kwargs["description"] == "the planning agent"

    def test_register_skips_shared_when_already_exists(self, monkeypatch):
        comp = _registry_components(shared_meta={"namespace": "shared"})
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = CliRunner().invoke(cli, ["agent", "register", "coder"])

        assert result.exit_code == 0, result.output
        # Only the agent NS was upserted; shared was left alone
        assert comp.storage.set_namespace_meta.await_count == 1
        assert comp.storage.set_namespace_meta.await_args_list[0].args[0] == "agent-runtime:coder"

    def test_register_rejects_empty_agent_id(self, monkeypatch):
        comp = _registry_components()
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = CliRunner().invoke(cli, ["agent", "register", "   "])

        assert result.exit_code != 0
        assert "non-empty" in result.output


class TestAgentList:
    def test_list_table_groups_agents_and_shared(self, monkeypatch):
        comp = _registry_components(
            namespaces=[
                ("agent-runtime:planner", 5),
                ("agent-runtime:coder", 2),
                ("shared", 3),
                ("default", 8),
            ],
            namespace_meta=[
                {
                    "namespace": "agent-runtime:planner",
                    "description": "planner role",
                    "color": None,
                },
                {"namespace": "agent-runtime:coder", "description": None, "color": "#abcdef"},
                {"namespace": "default", "description": None, "color": None},
            ],
            shared_meta={"namespace": "shared", "description": "shared knowledge"},
        )
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = CliRunner().invoke(cli, ["agent", "list"])

        assert result.exit_code == 0, result.output
        assert "Agents: 2" in result.output
        assert "planner" in result.output
        assert "agent-runtime:planner" in result.output
        assert "planner role" in result.output
        assert "coder" in result.output
        assert "Shared: shared" in result.output
        assert "shared knowledge" in result.output
        # Non-agent namespaces are not surfaced in the table
        assert "default" not in result.output.split("Shared:")[0]

    def test_list_json_machine_readable(self, monkeypatch):
        comp = _registry_components(
            namespaces=[("agent-runtime:planner", 7), ("shared", 2)],
            namespace_meta=[
                {"namespace": "agent-runtime:planner", "description": None, "color": None},
            ],
            shared_meta={"namespace": "shared", "description": None},
        )
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = CliRunner().invoke(cli, ["agent", "list", "--json"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert {a["agent_id"] for a in payload["agents"]} == {"planner"}
        assert payload["agents"][0]["chunks"] == 7
        assert payload["shared"]["chunks"] == 2

    def test_list_empty_state_message(self, monkeypatch):
        comp = _registry_components()
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = CliRunner().invoke(cli, ["agent", "list"])

        assert result.exit_code == 0, result.output
        assert "No agents registered" in result.output


class TestAgentDebugResolve:
    """``mm agent debug-resolve`` is the hidden e2e helper — JSON-only output
    so integration scripts can assert resolved namespaces without standing up
    an MCP client.
    """

    def test_explicit_agent_id_with_shared(self):
        result = CliRunner().invoke(cli, ["agent", "debug-resolve", "--agent-id", "planner"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["agent_namespace"] == "agent-runtime:planner"
        assert payload["resolved_namespace_filter"] == "agent-runtime:planner,shared"

    def test_falls_back_to_current_agent_id(self):
        result = CliRunner().invoke(
            cli,
            [
                "agent",
                "debug-resolve",
                "--current-agent-id",
                "planner",
                "--no-include-shared",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["agent_namespace"] == "agent-runtime:planner"
        assert payload["resolved_namespace_filter"] == "agent-runtime:planner"

    def test_legacy_current_namespace_fallback(self):
        result = CliRunner().invoke(
            cli,
            [
                "agent",
                "debug-resolve",
                "--current-namespace",
                "legacy:project",
                "--no-include-shared",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["agent_namespace"] == "legacy:project"
        assert payload["resolved_namespace_filter"] == "legacy:project"

    def test_no_inputs_returns_null_filter(self):
        result = CliRunner().invoke(cli, ["agent", "debug-resolve"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["agent_namespace"] is None
        assert payload["resolved_namespace_filter"] is None
