"""Tool/CLI-level threading tests for the per-call rerank bypass (#1766).

Pipeline-level behavior (pool collapse, cache isolation, hot-reload snapshot)
is covered in ``test_pipeline.py::TestPerCallRerankBypass``; this file pins the
plumbing above it: ``mem_search`` / ``mem_context_compose`` / ``mem_do`` /
``mm search --no-rerank`` / ``mm pinned compose --no-rerank`` must deliver the
caller's ``rerank`` decision to ``SearchPipeline.search`` unchanged, and
``mem_search`` must surface the trust-UX hint when ``rerank=true`` cannot be
honored.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from click.testing import CliRunner

from memtomem.search.pipeline import RetrievalStats


def _fake_app(*, rerank_applied: bool = True) -> MagicMock:
    app = MagicMock()
    app.search_pipeline.search = AsyncMock(
        return_value=([], RetrievalStats(rerank_applied=rerank_applied))
    )
    app.current_namespace = None
    app.webhook_manager = None
    return app


@pytest.mark.asyncio
class TestMemSearchRerankParam:
    async def test_rerank_false_reaches_pipeline(self):
        from memtomem.server.tools.search import mem_search

        app = _fake_app()
        with pytest.MonkeyPatch.context() as m:
            m.setattr(
                "memtomem.server.tools.search._get_app_initialized", AsyncMock(return_value=app)
            )
            m.setattr(
                "memtomem.server.tools.search._announce_dim_mismatch_once",
                AsyncMock(return_value=None),
            )
            await mem_search(query="hello", rerank=False, ctx=SimpleNamespace())

        assert app.search_pipeline.search.await_args.kwargs["rerank"] is False

    async def test_rerank_omitted_defaults_to_none(self):
        from memtomem.server.tools.search import mem_search

        app = _fake_app()
        with pytest.MonkeyPatch.context() as m:
            m.setattr(
                "memtomem.server.tools.search._get_app_initialized", AsyncMock(return_value=app)
            )
            m.setattr(
                "memtomem.server.tools.search._announce_dim_mismatch_once",
                AsyncMock(return_value=None),
            )
            await mem_search(query="hello", ctx=SimpleNamespace())

        assert app.search_pipeline.search.await_args.kwargs["rerank"] is None

    async def test_rerank_true_with_server_disabled_emits_hint(self):
        from memtomem.server.tools.search import mem_search

        app = _fake_app(rerank_applied=False)
        with pytest.MonkeyPatch.context() as m:
            m.setattr(
                "memtomem.server.tools.search._get_app_initialized", AsyncMock(return_value=app)
            )
            m.setattr(
                "memtomem.server.tools.search._announce_dim_mismatch_once",
                AsyncMock(return_value=None),
            )
            text = await mem_search(query="hello", rerank=True, ctx=SimpleNamespace())
            structured = await mem_search(
                query="hello", rerank=True, output_format="structured", ctx=SimpleNamespace()
            )

        assert "rerank=true requested but server reranking is disabled" in text
        hints = json.loads(structured)["hints"]
        assert any("rerank=true requested" in h for h in hints)

    async def test_rerank_true_with_server_enabled_has_no_hint(self):
        from memtomem.server.tools.search import mem_search

        app = _fake_app(rerank_applied=True)
        with pytest.MonkeyPatch.context() as m:
            m.setattr(
                "memtomem.server.tools.search._get_app_initialized", AsyncMock(return_value=app)
            )
            m.setattr(
                "memtomem.server.tools.search._announce_dim_mismatch_once",
                AsyncMock(return_value=None),
            )
            text = await mem_search(query="hello", rerank=True, ctx=SimpleNamespace())

        assert "rerank=true requested" not in text


class _FakePinnedStore:
    """Minimal PinnedContextStore stand-in for ContextAssembler.compose."""

    project_root = None

    def list(self, agent_id=None):
        return []

    def search_exclusion_roots(self):
        return ()


@pytest.mark.asyncio
class TestContextComposeRerankParam:
    async def test_compose_threads_rerank_to_pipeline(self):
        from memtomem.pinned import ContextAssembler

        pipeline = SimpleNamespace(search=AsyncMock(return_value=([], RetrievalStats())))
        assembler = ContextAssembler(_FakePinnedStore(), pipeline)

        await assembler.compose("query", rerank=False)

        assert pipeline.search.await_args.kwargs["rerank"] is False

    async def test_mem_context_compose_threads_rerank(self):
        from memtomem.server.tools import pinned as pinned_tools

        app = _fake_app()
        with pytest.MonkeyPatch.context() as m:
            m.setattr(
                "memtomem.server.tools.pinned._store",
                AsyncMock(return_value=(app, _FakePinnedStore())),
            )
            result = await pinned_tools.mem_context_compose(
                query="query", rerank=False, ctx=SimpleNamespace()
            )

        assert app.search_pipeline.search.await_args.kwargs["rerank"] is False
        assert json.loads(result)["retrieved"] == []

    async def test_mem_do_routes_rerank_param(self):
        from memtomem.server.tools.meta import mem_do

        app = _fake_app()
        with pytest.MonkeyPatch.context() as m:
            m.setattr(
                "memtomem.server.tools.pinned._store",
                AsyncMock(return_value=(app, _FakePinnedStore())),
            )
            result = await mem_do("context_compose", params={"query": "query", "rerank": False})

        assert app.search_pipeline.search.await_args.kwargs["rerank"] is False
        assert "Error" not in str(result)[:40]


class TestCliNoRerankFlag:
    def test_mm_search_no_rerank_reaches_pipeline(self):
        from memtomem.cli import cli

        pipeline = SimpleNamespace(search=AsyncMock(return_value=([], RetrievalStats())))
        comp = SimpleNamespace(search_pipeline=pipeline)

        @asynccontextmanager
        async def fake_components():
            yield comp

        with pytest.MonkeyPatch.context() as m:
            m.setattr("memtomem.cli._bootstrap.cli_components", fake_components)
            m.setattr(
                "memtomem.cli.search._resolve_project_context_root_from_cwd", lambda comp: None
            )
            result = CliRunner().invoke(cli, ["search", "hello", "--no-rerank"])

        assert result.exit_code == 0, result.output
        assert pipeline.search.await_args.kwargs["rerank"] is False

    def test_mm_search_without_flag_follows_config(self):
        from memtomem.cli import cli

        pipeline = SimpleNamespace(search=AsyncMock(return_value=([], RetrievalStats())))
        comp = SimpleNamespace(search_pipeline=pipeline)

        @asynccontextmanager
        async def fake_components():
            yield comp

        with pytest.MonkeyPatch.context() as m:
            m.setattr("memtomem.cli._bootstrap.cli_components", fake_components)
            m.setattr(
                "memtomem.cli.search._resolve_project_context_root_from_cwd", lambda comp: None
            )
            result = CliRunner().invoke(cli, ["search", "hello"])

        assert result.exit_code == 0, result.output
        assert pipeline.search.await_args.kwargs["rerank"] is None

    def test_mm_pinned_compose_no_rerank_reaches_pipeline(self):
        from memtomem.cli import cli

        pipeline = SimpleNamespace(search=AsyncMock(return_value=([], RetrievalStats())))
        comp = SimpleNamespace(search_pipeline=pipeline)

        @asynccontextmanager
        async def fake_store_context():
            yield comp, _FakePinnedStore()

        with pytest.MonkeyPatch.context() as m:
            m.setattr("memtomem.cli.pinned_cmd._store_context", fake_store_context)
            result = CliRunner().invoke(cli, ["pinned", "compose", "hello", "--no-rerank"])

        assert result.exit_code == 0, result.output
        assert pipeline.search.await_args.kwargs["rerank"] is False
