"""Tests for the ``mm add --json`` write ack (#1615).

The three write-shaped commands (``add``, ``reset``, ``purge``) gained
``--json`` acks in the same change; reset/purge tests live next to their
existing suites (``test_reset_cmd.py`` / ``test_purge_cli.py``). This
file covers ``mm add``: the CONTRIBUTING write-command shape
(``{"ok": true, ...}`` / ``{"ok": false, "reason": ...}`` with exit 0
for CLI-classified failures) and the loud nonzero exit for unexpected
errors.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner

from memtomem import privacy
from memtomem.cli.memory import add as add_cmd

_CLEAN = "team retro notes: rotate the on-call schedule monthly"
_SECRET = "api_key=AKIA1234567890ABCDEF"


@pytest.fixture(autouse=True)
def _reset_privacy_counters():
    privacy.reset_for_tests()
    yield
    privacy.reset_for_tests()


def _components(tmp_path: Path, *, index_error: Exception | None = None) -> SimpleNamespace:
    """Minimal Components double for the ``_add`` happy path: config with
    a tmp memory dir, an index engine returning 2 chunks (or raising),
    and a storage whose tag lookup is never reached (no tags passed)."""
    index_file = (
        AsyncMock(side_effect=index_error)
        if index_error is not None
        else AsyncMock(return_value=SimpleNamespace(indexed_chunks=2))
    )
    return SimpleNamespace(
        config=SimpleNamespace(
            indexing=SimpleNamespace(
                memory_dirs=[str(tmp_path / "memories")],
                project_memory_dirs=[],
            )
        ),
        index_engine=SimpleNamespace(index_file=index_file),
        storage=SimpleNamespace(list_chunks_by_source=AsyncMock(return_value=[])),
    )


def _patch_components(monkeypatch: pytest.MonkeyPatch, comp: SimpleNamespace) -> None:
    @asynccontextmanager
    async def fake():
        yield comp

    monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake)
    # ``_add`` lazy-imports this from its defining module; ``None`` means
    # "not inside a project", which keeps the default user scope simple.
    monkeypatch.setattr(
        "memtomem.server.tools.search._resolve_project_context_root", lambda comp: None
    )


class TestAddJsonAck:
    def test_success_json_ack(self, monkeypatch, tmp_path):
        comp = _components(tmp_path)
        _patch_components(monkeypatch, comp)

        result = CliRunner().invoke(add_cmd, [_CLEAN, "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["ok"] is True
        assert data["chunks"] == 2
        assert data["target"].endswith(".md")
        assert Path(data["target"]).exists()

    def test_text_path_unchanged(self, monkeypatch, tmp_path):
        comp = _components(tmp_path)
        _patch_components(monkeypatch, comp)

        result = CliRunner().invoke(add_cmd, [_CLEAN])

        assert result.exit_code == 0, result.output
        assert "Added to" in result.output
        assert "(2 chunks indexed)" in result.output

    def test_privacy_block_json_exit_zero(self, monkeypatch):
        # The guard fires before component bootstrap — a bootstrap call
        # would mean the blocked write slipped past the chokepoint.
        bootstrap = AsyncMock()
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", bootstrap)

        result = CliRunner().invoke(add_cmd, [_SECRET, "--json"])

        bootstrap.assert_not_called()
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["ok"] is False
        assert "privacy pattern" in data["reason"]

    def test_project_shared_decline_json_exit_zero(self, monkeypatch, tmp_path):
        # Gate B prompt under --json: chrome rides stderr, and declining
        # is a handled outcome ({"ok": false}, exit 0) instead of the
        # text path's click.Abort.
        proj = tmp_path / "proj"
        base = proj / ".memtomem" / "memories"
        comp = _components(tmp_path)
        comp.config.indexing.project_memory_dirs = [str(base)]
        _patch_components(monkeypatch, comp)
        monkeypatch.setattr(
            "memtomem.server.tools.search._resolve_project_context_root", lambda comp: proj
        )

        result = CliRunner().invoke(
            add_cmd, [_CLEAN, "--scope", "project_shared", "--json"], input="n\n"
        )

        assert result.exit_code == 0, result.output
        # stdout alone is the machine-readable ack; the prompt is stderr.
        data = json.loads(result.stdout)
        assert data == {"ok": False, "reason": "cancelled at project_shared confirmation prompt"}
        assert "Continue?" in result.stderr
        comp.index_engine.index_file.assert_not_called()

    def test_project_shared_decline_json_win_prompt_branch(self, monkeypatch, tmp_path):
        """#1640: forced WIN prompt branch must not pollute the JSON ack —
        this flow failed on windows-latest until Gate B moved to
        ``_prompts.confirm``."""
        import click.termui

        proj = tmp_path / "proj"
        base = proj / ".memtomem" / "memories"
        comp = _components(tmp_path)
        comp.config.indexing.project_memory_dirs = [str(base)]
        _patch_components(monkeypatch, comp)
        monkeypatch.setattr(
            "memtomem.server.tools.search._resolve_project_context_root", lambda comp: proj
        )
        monkeypatch.setattr(click.termui, "WIN", True)

        result = CliRunner().invoke(
            add_cmd, [_CLEAN, "--scope", "project_shared", "--json"], input="n\n"
        )

        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        assert data == {"ok": False, "reason": "cancelled at project_shared confirmation prompt"}

    def test_unexpected_error_keeps_nonzero_exit(self, monkeypatch, tmp_path):
        # Only CLI-classified failures (ClickException) ride the exit-0
        # JSON body; a crashed index engine must stay loud (CONTRIBUTING:
        # unhandled exceptions surface through Click).
        comp = _components(tmp_path, index_error=RuntimeError("boom"))
        _patch_components(monkeypatch, comp)

        result = CliRunner().invoke(add_cmd, [_CLEAN, "--json"])

        assert result.exit_code != 0
        assert "boom" in result.output
