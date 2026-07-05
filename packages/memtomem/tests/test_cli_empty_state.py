from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner

from memtomem.cli import cli
from memtomem.search.pipeline import RetrievalStats


def _mock_empty_search() -> tuple:
    """Mock cli_components returning empty search results."""
    pipeline_mock = AsyncMock(return_value=([], RetrievalStats()))
    config = SimpleNamespace(indexing=SimpleNamespace(project_memory_dirs=[]))
    comp = SimpleNamespace(
        search_pipeline=SimpleNamespace(search=pipeline_mock),
        config=config,
    )

    @asynccontextmanager
    async def fake():
        yield comp

    return fake, pipeline_mock


def _mock_empty_recall() -> tuple:
    """Mock cli_components returning empty recall results."""
    storage = SimpleNamespace(recall_chunks=AsyncMock(return_value=[]))
    config = SimpleNamespace(
        search=SimpleNamespace(system_namespace_prefixes=()),
        indexing=SimpleNamespace(project_memory_dirs=[]),
    )
    comp = SimpleNamespace(storage=storage, config=config)

    @asynccontextmanager
    async def fake():
        yield comp

    return fake, storage


HINT = "No results found. See `mm status` to confirm your index has chunks."


class TestSearchEmptyState:
    """mm search prints a friendly hint on stderr when results are empty."""

    @pytest.mark.parametrize("fmt", ["table", "plain"])
    def test_non_json_formats_print_hint_to_stderr(self, monkeypatch, fmt: str) -> None:
        fake, _ = _mock_empty_search()
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake)

        result = CliRunner().invoke(cli, ["search", "--format", fmt, "hello"])

        assert result.exit_code == 0
        assert HINT in result.stderr

    def test_json_format_stdout_unchanged(self, monkeypatch) -> None:
        fake, _ = _mock_empty_search()
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake)

        result = CliRunner().invoke(cli, ["search", "--format", "json", "hello"])

        assert result.exit_code == 0
        assert result.output.strip() == "[]"
        assert result.stderr == ""


class TestRecallEmptyState:
    """mm recall prints a friendly hint on stderr when results are empty."""

    @pytest.mark.parametrize("fmt", ["table", "plain"])
    def test_non_json_formats_print_hint_to_stderr(self, monkeypatch, fmt: str) -> None:
        fake, _ = _mock_empty_recall()
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake)

        result = CliRunner().invoke(cli, ["recall", "--format", fmt])

        assert result.exit_code == 0
        assert HINT in result.stderr

    def test_json_format_stdout_unchanged(self, monkeypatch) -> None:
        fake, _ = _mock_empty_recall()
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake)

        result = CliRunner().invoke(cli, ["recall", "--format", "json"])

        assert result.exit_code == 0
        assert result.output.strip() == "[]"
        assert result.stderr == ""
