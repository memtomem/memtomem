"""``mm search`` / ``mm recall`` JSON carries the whole chunk.

``content`` used to be cut at 200 characters, so a script reading the JSON
could not get at the text it had just found. It is now the full chunk, with
the old cut kept as ``content_preview``. ``--json`` is a shorthand for
``--format json`` on both commands.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from click.testing import CliRunner

from memtomem.cli import cli
from memtomem.cli.search import SearchPayload, render_search_results
from memtomem.models import Chunk, ChunkMetadata, SearchResult
from memtomem.search.pipeline import RetrievalStats

_LONG = "x" * 150 + " middle " + "y" * 150 + " tail-marker"


def _chunk() -> Chunk:
    return Chunk(
        content=_LONG,
        metadata=ChunkMetadata(source_file=Path("/tmp/long.md")),
        content_hash=f"hash-{uuid4().hex}",
        embedding=[],
        created_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )


def _recall(monkeypatch, *args: str, chunk: Chunk | None = None):
    storage = SimpleNamespace(
        recall_chunks=AsyncMock(return_value=[chunk or _chunk()]),
        list_namespaces=AsyncMock(return_value=[("default", 1)]),
    )
    comp = SimpleNamespace(
        storage=storage,
        config=SimpleNamespace(
            search=SimpleNamespace(system_namespace_prefixes=()),
            indexing=SimpleNamespace(project_memory_dirs=[]),
        ),
    )

    @asynccontextmanager
    async def fake():
        yield comp

    monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake)
    return CliRunner().invoke(cli, ["recall", *args])


class TestRecallJson:
    def test_content_is_whole_and_preview_is_the_old_cut(self, monkeypatch):
        result = _recall(monkeypatch, "--format", "json")

        assert result.exit_code == 0, result.stderr
        [row] = json.loads(result.stdout)
        assert row["content"] == _LONG
        assert row["content_preview"] == _LONG[:200]

    def test_json_flag_matches_format_json(self, monkeypatch):
        chunk = _chunk()
        flag = _recall(monkeypatch, "--json", chunk=chunk)
        long_form = _recall(monkeypatch, "--format", "json", chunk=chunk)

        assert flag.exit_code == 0, flag.stderr
        assert json.loads(flag.stdout) == json.loads(long_form.stdout)


class TestSearchJson:
    def test_content_is_whole_and_preview_is_the_old_cut(self, capsys):
        payload = SearchPayload(
            results=[SearchResult(chunk=_chunk(), score=0.5, rank=1, source="fused")],
            stats=RetrievalStats(final_total=1),
            hints=[],
            empty_message="",
        )

        render_search_results("q", "json", payload)

        [row] = json.loads(capsys.readouterr().out)
        assert row["content"] == _LONG
        assert row["content_preview"] == _LONG[:200]

    @pytest.mark.parametrize("args", [["--json"], ["--format", "json"]])
    def test_json_flag_reaches_the_renderer_as_json(self, monkeypatch, args):
        search = AsyncMock()
        monkeypatch.setattr("memtomem.cli.search._search", search)

        result = CliRunner().invoke(cli, ["search", "q", *args])

        assert result.exit_code == 0, result.output
        assert search.await_args.args[7] == "json"
