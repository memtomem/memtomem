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
from unittest.mock import create_autospec
from uuid import uuid4

import pytest
from click.testing import CliRunner

from memtomem.cli import cli
from memtomem.cli import search as search_mod
from memtomem.cli.search import SearchPayload, render_search_results
from memtomem.models import Chunk, ChunkMetadata, SearchResult
from memtomem.search.pipeline import RetrievalStats
from memtomem.storage.sqlite_backend import SqliteBackend

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
    # Autospecced, so a keyword the real ``recall_chunks`` does not take fails here too.
    storage = create_autospec(SqliteBackend, instance=True)
    storage.recall_chunks.return_value = [chunk or _chunk()]
    storage.list_namespaces.return_value = [("default", 1)]
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


def _payload(chunk: Chunk | None = None) -> SearchPayload:
    return SearchPayload(
        results=[SearchResult(chunk=chunk or _chunk(), score=0.5, rank=1, source="fused")],
        stats=RetrievalStats(final_total=1),
        hints=[],
        empty_message="",
    )


class TestSearchJson:
    def test_content_is_whole_and_preview_is_the_old_cut(self, capsys):
        render_search_results("q", "json", _payload())

        [row] = json.loads(capsys.readouterr().out)
        assert row["content"] == _LONG
        assert row["content_preview"] == _LONG[:200]

    @pytest.mark.parametrize("args", [["--json"], ["--format", "json"]])
    def test_json_flag_reaches_the_renderer_as_json(self, monkeypatch, args):
        """Only retrieval is replaced; the command, ``_search`` and the
        renderer all run, so stdout is what a pipe would read."""

        @asynccontextmanager
        async def fake():
            yield SimpleNamespace()

        retrieval = create_autospec(search_mod._search_with_components, return_value=_payload())
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake)
        monkeypatch.setattr("memtomem.cli.search._search_with_components", retrieval)

        result = CliRunner().invoke(cli, ["search", "q", *args])

        assert result.exit_code == 0, result.output
        assert retrieval.call_args.kwargs["fmt"] == "json"
        [row] = json.loads(result.stdout)
        assert row["content"] == _LONG
        assert row["content_preview"] == _LONG[:200]
