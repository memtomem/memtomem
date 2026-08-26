"""``mm search`` surfaces the hints the shared search service derives.

Before #2063 the CLI called ``search_pipeline.search`` directly and so
inherited none of them — including the notice that dense retrieval had
dropped out of the ranking.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner

from memtomem.cli import cli
from memtomem.models import SearchResult
from memtomem.search.pipeline import RetrievalStats

from helpers import make_chunk

_MISMATCH = {
    "dimension_mismatch": True,
    "stored": {"provider": "none", "model": "", "dimension": 0},
    "configured": {"provider": "onnx", "model": "bge-small-en-v1.5", "dimension": 384},
}

_DEGRADED = RetrievalStats(
    bm25_candidates=1,
    dense_candidates=0,
    final_total=1,
    dense_suppressed_mismatch=True,
    mismatch_detail=_MISMATCH,
)


def _result() -> SearchResult:
    chunk = make_chunk("notes about pipelines", source="notes.md")
    return SearchResult(chunk=chunk, score=1.5, rank=1, source="bm25")


def _mock_components(results, stats):
    pipeline_mock = AsyncMock(return_value=(results, stats))
    comp = SimpleNamespace(
        search_pipeline=SimpleNamespace(search=pipeline_mock),
        config=SimpleNamespace(indexing=SimpleNamespace(project_memory_dirs=[])),
    )

    @asynccontextmanager
    async def fake():
        yield comp

    return fake


def _invoke(monkeypatch, *args, results=None, stats=_DEGRADED):
    fake = _mock_components([_result()] if results is None else results, stats)
    monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake)
    return CliRunner().invoke(cli, ["search", *args, "pipelines"])


class TestDenseDegradedHint:
    @pytest.mark.parametrize("fmt", ["table", "plain", "json", "context", "smart"])
    def test_every_format_reports_the_degradation_on_stderr(self, monkeypatch, fmt: str) -> None:
        result = _invoke(monkeypatch, "--format", fmt)

        assert result.exit_code == 0
        assert "dense retrieval did not contribute" in result.stderr

    @pytest.mark.parametrize("fmt", ["context", "smart"])
    def test_empty_results_still_report_it(self, monkeypatch, fmt: str) -> None:
        """``context`` and ``smart`` return early on an empty result set —
        the branch where a silent degradation is hardest to notice."""
        result = _invoke(monkeypatch, "--format", fmt, results=[])

        assert result.exit_code == 0
        assert "dense retrieval did not contribute" in result.stderr

    def test_json_stdout_stays_a_bare_list(self, monkeypatch) -> None:
        """Machine consumers pipe stdout; the notice must not land in it."""
        result = _invoke(monkeypatch, "--format", "json")

        payload = json.loads(result.stdout)
        assert isinstance(payload, list)
        assert payload[0]["source"].endswith("notes.md")
        assert "dense retrieval" not in result.stdout

    def test_table_footer_marks_the_suppressed_leg(self, monkeypatch) -> None:
        result = _invoke(monkeypatch, "--format", "table")

        assert "0 dense (suppressed: embedding mismatch)" in result.stdout

    def test_healthy_search_leaves_the_footer_and_stderr_alone(self, monkeypatch) -> None:
        stats = RetrievalStats(bm25_candidates=1, dense_candidates=2, final_total=1)

        result = _invoke(monkeypatch, "--format", "table", stats=stats)

        assert "1 BM25 + 2 dense → 1 results" in result.stdout
        assert "suppressed" not in result.stdout
        assert result.stderr == ""


class TestAsOfValidation:
    def test_message_still_names_the_cli_flag(self, monkeypatch) -> None:
        """The service's own message says ``as_of``; the CLI must keep saying
        ``--as-of`` so the remediation matches what the user typed."""
        result = _invoke(monkeypatch, "--as-of", "2025-Q5")

        assert result.exit_code != 0
        assert "invalid --as-of value '2025-Q5'" in result.stderr

    def test_a_bad_bound_is_rejected_before_components_open(self, monkeypatch) -> None:
        opened = False

        @asynccontextmanager
        async def fake():
            nonlocal opened
            opened = True
            yield SimpleNamespace()

        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake)

        result = CliRunner().invoke(cli, ["search", "--as-of", "not-a-date", "q"])

        assert result.exit_code != 0
        assert opened is False


class TestFilterSyntaxValidation:
    """A comma list mixed with a glob parses as one pattern matching nothing,
    so without this rejection the CLI prints "No results found" for a query
    that was never runnable."""

    def test_namespace_message_names_the_cli_flag(self, monkeypatch) -> None:
        result = _invoke(monkeypatch, "--namespace", "archive:*,work")

        assert result.exit_code != 0
        assert "invalid --namespace value 'archive:*,work'" in result.stderr

    def test_scope_message_names_the_cli_flag(self, monkeypatch) -> None:
        result = _invoke(monkeypatch, "--scope", "project_*,user")

        assert result.exit_code != 0
        assert "invalid --scope value 'project_*,user'" in result.stderr

    @pytest.mark.parametrize(
        ("flag", "value"),
        [("--namespace", "archive:*,work"), ("--scope", "project_*,user")],
    )
    def test_rejected_before_components_open(self, monkeypatch, flag: str, value: str) -> None:
        opened = False

        @asynccontextmanager
        async def fake():
            nonlocal opened
            opened = True
            yield SimpleNamespace()

        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake)

        result = CliRunner().invoke(cli, ["search", flag, value, "q"])

        assert result.exit_code != 0
        assert opened is False

    @pytest.mark.parametrize(
        ("flag", "value"),
        [
            ("--namespace", "work,personal"),
            ("--namespace", "proj:*"),
            ("--scope", "user,project_local"),
            ("--scope", "project_*"),
        ],
    )
    def test_each_spelling_on_its_own_still_runs(self, monkeypatch, flag: str, value: str) -> None:
        result = _invoke(monkeypatch, flag, value)

        assert result.exit_code == 0
