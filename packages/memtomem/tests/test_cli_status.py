"""Tests for ``mm status`` — terminal mirror of the MCP ``mem_status`` tool (#382)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner

from memtomem.cli import cli
from memtomem.config import Mem2MemConfig


def _mock_components(
    *,
    total_chunks: int = 0,
    total_sources: int = 0,
    source_files: list[Path] | None = None,
    stored_embedding_info: dict | None = None,
    embedding_mismatch: dict | None = None,
) -> SimpleNamespace:
    """Build a minimal ``Components``-shaped mock for ``mm status`` tests.

    ``AppContext.from_components`` reads ``config``, ``storage``, and
    ``embedder`` off the container; ``format_status_report`` reads
    ``app.storage.get_stats()`` / ``get_all_source_files()`` plus the two
    optional ``stored_embedding_info`` / ``embedding_mismatch`` attributes.
    A ``SimpleNamespace`` covers all of that without dragging in the real
    ``Components`` dataclass (which would require building a SqliteBackend
    and an embedder).
    """
    storage = SimpleNamespace(
        get_stats=AsyncMock(
            return_value={"total_chunks": total_chunks, "total_sources": total_sources}
        ),
        get_all_source_files=AsyncMock(return_value=list(source_files or [])),
        stored_embedding_info=stored_embedding_info,
        embedding_mismatch=embedding_mismatch,
    )
    return SimpleNamespace(
        config=Mem2MemConfig(),
        storage=storage,
        embedder=SimpleNamespace(),
    )


def _patched_cli_components(comp: SimpleNamespace):
    @asynccontextmanager
    async def fake():
        yield comp

    return fake


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


class TestStatusRegistration:
    """``mm status`` is wired into the top-level CLI group."""

    def test_status_in_top_level_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "status" in result.output

    def test_status_help_describes_command(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["status", "--help"])
        assert result.exit_code == 0
        assert "indexing statistics" in result.output
        # Cross-reference to mem_status so users learn the symmetry.
        assert "mem_status" in result.output


class TestStatusOutput:
    """Happy-path rendering matches the MCP ``mem_status`` text shape."""

    def test_basic_output_renders_all_sections(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        comp = _mock_components(total_chunks=42, total_sources=7)
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = runner.invoke(cli, ["status"])
        assert result.exit_code == 0, result.output

        # Header + stats sections must appear so users recognize the same
        # report they get from ``mem_status``.
        assert "memtomem Status" in result.output
        assert "Index stats" in result.output
        assert "Total chunks:  42" in result.output
        assert "Source files:  7" in result.output
        assert "Immutable fields (set once at init)" in result.output

    def test_orphan_count_appended_when_files_missing(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # 3 indexed sources, only 1 present on disk → 2 orphaned.
        present = tmp_path / "present.md"
        present.write_text("hi")
        missing_a = tmp_path / "missing_a.md"
        missing_b = tmp_path / "missing_b.md"
        comp = _mock_components(
            total_chunks=3,
            total_sources=3,
            source_files=[present, missing_a, missing_b],
        )
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = runner.invoke(cli, ["status"])
        assert result.exit_code == 0, result.output
        assert "2 orphaned" in result.output
        assert "mem_cleanup_orphans" in result.output

    def test_embedding_mismatch_warning_block_emitted(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        comp = _mock_components(
            embedding_mismatch={
                "stored": {"provider": "ollama", "model": "bge-m3", "dimension": 1024},
                "configured": {"provider": "ollama", "model": "nomic", "dimension": 768},
            },
        )
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = runner.invoke(cli, ["status"])
        assert result.exit_code == 0, result.output
        # Stable-schema keys monitoring probes / dashboards rely on.
        assert "Warnings" in result.output
        assert "kind:       embedding_dim_mismatch" in result.output
        assert "fix:        uv run mm embedding-reset --mode apply-current" in result.output


class TestStatusUnconfigured:
    """Without a ``~/.memtomem/config.json`` the command should fail loudly,
    not silently bootstrap a fresh DB. ``cli_components`` raises a
    ``ClickException`` in that case; the wrapper must let it propagate."""

    def test_missing_config_yields_clickexception(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Point the cached module-level config path at an empty tmp dir so
        # the existence check fails deterministically.
        monkeypatch.setattr(
            "memtomem.cli._bootstrap._CONFIG_PATH", tmp_path / "no-such-config.json"
        )

        result = runner.invoke(cli, ["status"])
        assert result.exit_code != 0
        assert "not configured" in result.output
        assert "mm init" in result.output
