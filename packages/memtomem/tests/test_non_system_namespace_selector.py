"""``namespace="!system"`` selects every non-system namespace (#2919).

An omitted namespace hides the system namespaces, but an active session or
agent namespace replaces it; ``"*"`` brings the system namespaces back. The
selector is an explicit value, so it survives ``namespace or
current_namespace`` and then parses to the same filter an omitted namespace
gets. These tests drive each surface that accepts it against a store holding
two ordinary namespaces and two system ones, and assert the ordinary rows are
*present* — the code before this change read ``"!system"`` as an exact
namespace name and returned nothing, so an absence-only check would pass on it.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from click.testing import CliRunner
from httpx import ASGITransport, AsyncClient

from helpers import StubCtx
from memtomem.constants import NON_SYSTEM_NAMESPACES
from memtomem.models import Chunk, ChunkMetadata, InvalidNamespaceFilterError, NamespaceFilter
from memtomem.search.pipeline import RetrievalStats

SESSION_NS = "agent-runtime:w1"
ORDINARY = {"default", "personal"}
SYSTEM = {"archive:old", SESSION_NS}
_PREFIXES = ("archive:", "agent-runtime:")


def _chunk(path: Path, namespace: str) -> Chunk:
    return Chunk(
        content=f"zephyrword fact stored in {namespace}",
        metadata=ChunkMetadata(source_file=path, namespace=namespace),
        content_hash=f"hash-{uuid4().hex}",
        embedding=[0.1] * 1024,
    )


def _file_for(namespace: str) -> str:
    return namespace.replace(":", "-") + ".md"


@pytest.fixture
async def seeded(bm25_only_components):
    comp, mem_dir = bm25_only_components
    assert tuple(comp.config.search.system_namespace_prefixes) == _PREFIXES
    await comp.storage.upsert_chunks(
        [_chunk(mem_dir / _file_for(ns), ns) for ns in sorted(ORDINARY | SYSTEM)]
    )
    return comp


def _ctx(comp, *, session_ns: str | None = SESSION_NS):
    from memtomem.server.context import AppContext

    app = AppContext.from_components(comp)
    app.current_namespace = session_ns
    return StubCtx(app)


# ---------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------


class TestParse:
    def test_the_selector_parses_like_an_omitted_namespace(self):
        assert NamespaceFilter.parse(NON_SYSTEM_NAMESPACES, _PREFIXES) == NamespaceFilter.parse(
            None, _PREFIXES
        )
        assert NamespaceFilter.parse(NON_SYSTEM_NAMESPACES, _PREFIXES) == NamespaceFilter(
            exclude_prefixes=_PREFIXES
        )

    def test_without_system_prefixes_the_selector_filters_nothing(self):
        assert NamespaceFilter.parse(NON_SYSTEM_NAMESPACES) is None
        assert NamespaceFilter.parse(NON_SYSTEM_NAMESPACES, ()) is None

    @pytest.mark.parametrize(
        "value",
        ["!system,work", "work, !system", "!system,proj:*", ["!system"], ["work", "!system"]],
    )
    def test_the_selector_inside_a_list_is_refused(self, value):
        with pytest.raises(InvalidNamespaceFilterError, match="selector in a list"):
            NamespaceFilter.parse(value, _PREFIXES)

    def test_a_glob_that_starts_with_the_selector_text_stays_a_glob(self):
        assert NamespaceFilter.parse("!sys*", _PREFIXES) == NamespaceFilter(pattern="!sys*")


# ---------------------------------------------------------------------------
# MCP tools, against a real BM25 store
# ---------------------------------------------------------------------------


class TestMcpTools:
    @pytest.mark.asyncio
    async def test_mem_search_overrides_the_session_namespace(self, seeded):
        from memtomem.server.tools.search import mem_search

        ctx = _ctx(seeded)
        selected = json.loads(
            await mem_search(
                query="zephyrword",
                namespace=NON_SYSTEM_NAMESPACES,
                output_format="structured",
                ctx=ctx,
            )
        )
        assert {r["namespace"] for r in selected["results"]} == ORDINARY
        assert any("hidden in system namespaces" in h for h in selected.get("hints", []))

        # Contrast: the omitted namespace is the session's, which is the gap.
        ambient = json.loads(
            await mem_search(query="zephyrword", output_format="structured", ctx=ctx)
        )
        assert {r["namespace"] for r in ambient["results"]} == {SESSION_NS}

    @pytest.mark.asyncio
    async def test_mem_recall_overrides_the_session_namespace(self, seeded):
        from memtomem.server.tools.recall import mem_recall

        ctx = _ctx(seeded)
        selected = json.loads(
            await mem_recall(namespace=NON_SYSTEM_NAMESPACES, output_format="structured", ctx=ctx)
        )
        assert {r["namespace"] for r in selected["results"]} == ORDINARY
        assert any("hidden in system namespaces" in h for h in selected.get("hints", []))

        ambient = json.loads(await mem_recall(output_format="structured", ctx=ctx))
        assert {r["namespace"] for r in ambient["results"]} == {SESSION_NS}
        assert "hints" not in ambient

    @pytest.mark.asyncio
    async def test_mem_ask_overrides_the_session_namespace(self, seeded):
        from memtomem.server.tools.ask import mem_ask

        out = await mem_ask(
            question="zephyrword", namespace=NON_SYSTEM_NAMESPACES, ctx=_ctx(seeded)
        )
        for ns in ORDINARY:
            assert f"zephyrword fact stored in {ns}" in out
        for ns in SYSTEM:
            assert f"zephyrword fact stored in {ns}" not in out

    @pytest.mark.asyncio
    async def test_mem_timeline_reads_every_non_system_namespace(self, seeded):
        from memtomem.server.tools.temporal import mem_timeline

        out = await mem_timeline(
            topic="zephyrword", namespace=NON_SYSTEM_NAMESPACES, ctx=_ctx(seeded)
        )
        # One period; its "Sources:" line names every file in it.
        sources = [line for line in out.splitlines() if line.startswith("Sources: ")]
        assert len(sources) == 1, out
        assert set(sources[0].removeprefix("Sources: ").split(", ")) == {
            _file_for(ns) for ns in ORDINARY
        }

    @pytest.mark.asyncio
    async def test_mem_list_lists_what_an_omitted_namespace_lists(self, seeded):
        from memtomem.server.tools.browse import mem_list

        ctx = _ctx(seeded, session_ns=None)

        def listed(output: str) -> set[str]:
            return {
                Path(line.strip().split("  — ")[0]).name
                for line in output.splitlines()
                if line.startswith("  ") and "  — " in line
            }

        selected = listed(await mem_list(namespace=NON_SYSTEM_NAMESPACES, ctx=ctx))
        assert selected == {_file_for(ns) for ns in ORDINARY}
        assert selected == listed(await mem_list(ctx=ctx))

    @pytest.mark.asyncio
    async def test_the_sources_resource_pages_through_the_selector(self, seeded):
        from memtomem.server.context import AppContext
        from memtomem.server.resources import _sources_page

        app = AppContext.from_components(seeded)
        seen: list[str] = []
        cursor = None
        for _ in range(5):
            page = json.loads(
                await _sources_page(app, cursor=cursor, limit=1, namespace=NON_SYSTEM_NAMESPACES)
            )
            seen += [Path(s["path"]).name for s in page["sources"]]
            if page["next"] is None:
                break
            # The next-page URI carries the selector back, so page two is read
            # with it as well.
            query = parse_qs(urlsplit(page["next"]).query)
            assert query["namespace"] == [NON_SYSTEM_NAMESPACES]
            cursor = query["cursor"][0]
        assert sorted(seen) == sorted(_file_for(ns) for ns in ORDINARY)


# ---------------------------------------------------------------------------
# Web
# ---------------------------------------------------------------------------


def _web_app(comp):
    from memtomem.web.app import create_app

    app = create_app(lifespan=None, mode="dev")
    app.state.csrf_enforce = False
    app.state.storage = comp.storage
    app.state.search_pipeline = comp.search_pipeline
    app.state.index_engine = comp.index_engine
    app.state.config = comp.config
    return app


async def _get(app, url: str, **params):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(url, params=params)


class TestWeb:
    @pytest.mark.asyncio
    async def test_search_reads_every_non_system_namespace(self, seeded):
        resp = await _get(_web_app(seeded), "/api/search", q="zephyrword", namespace="!system")
        assert resp.status_code == 200, resp.text
        assert {r["chunk"]["namespace"] for r in resp.json()["results"]} == ORDINARY

    @pytest.mark.asyncio
    async def test_search_refuses_the_selector_in_a_list(self, seeded):
        resp = await _get(
            _web_app(seeded), "/api/search", q="zephyrword", namespace="!system,default"
        )
        assert resp.status_code == 422
        assert "selector in a list" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_timeline_reads_every_non_system_namespace(self, seeded):
        resp = await _get(_web_app(seeded), "/api/timeline", namespace="!system")
        assert resp.status_code == 200, resp.text
        assert {c["namespace"] for c in resp.json()["chunks"]} == ORDINARY

    @pytest.mark.asyncio
    async def test_timeline_refuses_the_selector_in_a_list(self, seeded):
        # The route lets the parser's ValueError reach the app-wide handler: 400.
        resp = await _get(_web_app(seeded), "/api/timeline", namespace="!system,default")
        assert resp.status_code == 400
        assert "selector in a list" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCli:
    def test_mm_search_forwards_the_selector(self, monkeypatch):
        from memtomem.cli import cli

        pipeline = AsyncMock(return_value=([], RetrievalStats()))
        comp = SimpleNamespace(
            search_pipeline=SimpleNamespace(search=pipeline),
            storage=SimpleNamespace(list_namespaces=AsyncMock(return_value=[("default", 1)])),
            config=SimpleNamespace(indexing=SimpleNamespace(project_memory_dirs=[])),
        )

        @asynccontextmanager
        async def fake():
            yield comp

        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake)
        result = CliRunner().invoke(cli, ["search", "-n", "!system", "--format", "json", "q"])
        assert result.exit_code == 0, result.output
        assert pipeline.await_args.kwargs["namespace"] == NON_SYSTEM_NAMESPACES

    def test_mm_search_reports_the_parsers_own_reason(self):
        from memtomem.cli import cli

        result = CliRunner().invoke(cli, ["search", "-n", "!system,work", "q"])
        assert result.exit_code != 0
        assert "selector in a list" in result.output
        assert "a comma list and a glob" not in result.output

    def test_mm_recall_applies_the_default_exclusion(self, monkeypatch):
        from memtomem.cli import cli

        storage = SimpleNamespace(
            recall_chunks=AsyncMock(return_value=[]),
            list_namespaces=AsyncMock(return_value=[("default", 1)]),
        )
        comp = SimpleNamespace(
            storage=storage,
            config=SimpleNamespace(
                search=SimpleNamespace(system_namespace_prefixes=list(_PREFIXES)),
                indexing=SimpleNamespace(project_memory_dirs=[]),
            ),
        )

        @asynccontextmanager
        async def fake():
            yield comp

        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake)
        result = CliRunner().invoke(cli, ["recall", "-n", "!system", "--format", "json"])
        assert result.exit_code == 0, result.output
        assert storage.recall_chunks.await_args.kwargs["namespace_filter"] == NamespaceFilter(
            exclude_prefixes=_PREFIXES
        )


# ---------------------------------------------------------------------------
# Other parser callers
# ---------------------------------------------------------------------------


class TestOtherCallers:
    def test_eval_case_filters_accept_the_selector_but_not_in_a_list(self):
        from memtomem.errors import EvalCaseError
        from memtomem.storage.mixins.eval_cases import validate_portable_filters

        db = sqlite3.connect(":memory:")
        validate_portable_filters(db, {"namespace": NON_SYSTEM_NAMESPACES})
        with pytest.raises(EvalCaseError, match="not replayable"):
            validate_portable_filters(db, {"namespace": [NON_SYSTEM_NAMESPACES]})

    def test_an_unbound_langgraph_store_forwards_the_selector(self):
        # A bound agent replaces the caller's namespace for every value by its
        # documented contract (``MemtomemStore.search``); unbound, it passes through.
        from memtomem.integrations.langgraph import MemtomemStore

        store = MemtomemStore()
        store._current_agent_id = None
        assert (
            store._resolve_search_namespace(namespace=NON_SYSTEM_NAMESPACES, include_shared=None)
            == NON_SYSTEM_NAMESPACES
        )
