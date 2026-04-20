"""Tests for ``mm agent migrate``."""

from __future__ import annotations

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
