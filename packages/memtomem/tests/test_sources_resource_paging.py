"""``memtomem://sources`` returns one page with ``mem_list``'s visibility (#2753).

The resource used to dump every indexed source, held and pending ones
included. It now serves the first page of the same listing ``mem_list`` pages
through, and a ``memtomem://sources{?cursor,limit,namespace}`` template serves
the rest. The oracle is ``test_mem_list_pagination``'s: the pre-pagination
Python filters over the unpaged ``get_source_files_with_counts``.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from memtomem.server import mcp, resources
from memtomem.server.context import AppContext
from test_mem_list_pagination import _chunk, _oracle
from test_mem_list_pagination import matrix as _shared_matrix  # noqa: F401 — the fixture

TEMPLATE = "memtomem://sources{?cursor,limit,namespace}"


@pytest.fixture
async def matrix(_shared_matrix):  # noqa: F811 — the parameter requests the imported fixture
    """The visibility matrix ``mem_list``'s pagination tests are judged against."""
    return _shared_matrix


def _serve(monkeypatch, comp) -> AppContext:
    """Point both resource handlers at ``comp`` without a running lifespan."""
    app = AppContext.from_components(comp)
    monkeypatch.setattr(resources, "_get_active_app_initialized", AsyncMock(return_value=app))
    monkeypatch.setattr(resources, "_get_app_initialized", AsyncMock(return_value=app))
    return app


async def _read(uri: str) -> dict:
    """Read a resource the way a client does: through the server's registry."""
    contents = list(await mcp.read_resource(uri))  # type: ignore[arg-type]
    assert len(contents) == 1
    return json.loads(contents[0].content)


async def _follow(uri: str, *, max_pages: int = 100) -> tuple[list[str], list[dict]]:
    """Every page from ``uri`` on; returns (paths in order, pages)."""
    paths: list[str] = []
    pages: list[dict] = []
    for _ in range(max_pages):
        page = await _read(uri)
        pages.append(page)
        paths += [s["path"] for s in page["sources"]]
        if page["next"] is None:
            return paths, pages
        uri = page["next"]
    raise AssertionError("traversal did not end")


async def _expected(storage, **kwargs) -> list[str]:
    rows = _oracle(
        await storage.get_source_files_with_counts(),
        await storage.get_hidden_source_files(),
        **kwargs,
    )
    return [str(r[0]) for r in rows]


class TestVisibilityParity:
    @pytest.mark.asyncio
    async def test_static_resource_is_the_first_page_of_mem_lists_listing(
        self, matrix, monkeypatch
    ):
        _serve(monkeypatch, matrix)
        page = json.loads(await resources.sources_resource())
        assert [s["path"] for s in page["sources"]] == await _expected(matrix.storage)
        assert page["next"] is None
        assert page["page_size"] == 50
        assert set(page["sources"][0]) == {"path", "chunks", "updated", "namespaces", "avg_tokens"}

    @pytest.mark.asyncio
    async def test_held_pending_and_system_only_sources_are_hidden(self, matrix, monkeypatch):
        _serve(monkeypatch, matrix)
        listed = {Path(s["path"]).name for s in (await _read("memtomem://sources"))["sources"]}
        assert "plain.md" in listed
        for hidden in ("held.md", "pending.md", "archived.md", "agent.md"):
            assert hidden not in listed, hidden
        every = {Path(r[0]).name for r in await matrix.storage.get_source_files_with_counts()}
        assert {"held.md", "pending.md", "archived.md", "agent.md"} <= every

    @pytest.mark.asyncio
    @pytest.mark.parametrize("namespace", ["work", "archive:2026", "work,personal", "default"])
    async def test_namespace_parameter_matches_the_tools_filter(
        self, matrix, monkeypatch, namespace
    ):
        _serve(monkeypatch, matrix)
        paths, pages = await _follow(f"memtomem://sources?limit=1&namespace={namespace}")
        assert paths == await _expected(matrix.storage, namespace=namespace)
        # Every continuation carries the filter, not just the first page.
        for page in pages[:-1]:
            assert f"namespace={namespace}" in page["next"].replace("%3A", ":").replace("%2C", ",")
            assert "limit=1" in page["next"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("namespace", ["a:b", "/work", "../work", "C:\\work"])
    async def test_namespace_values_mem_list_accepts_read_through_the_registry(
        self, bm25_only_components, monkeypatch, namespace
    ):
        """A namespace is a stored string, not a path: the SDK's path checks on
        template parameters must not reject what ``mem_list`` lists."""
        from urllib.parse import quote

        from memtomem.server.tools.browse import mem_list
        from test_mem_list_pagination import _ctx

        comp, mem_dir = bm25_only_components
        await comp.storage.upsert_chunks(
            [_chunk(mem_dir / "in.md", namespace), _chunk(mem_dir / "out.md", "default")]
        )
        _serve(monkeypatch, comp)
        listed = await mem_list(namespace=namespace, ctx=_ctx(comp))  # type: ignore[arg-type]
        assert "in.md" in listed and "out.md" not in listed
        page = await _read(f"memtomem://sources?namespace={quote(namespace, safe='')}")
        assert [Path(s["path"]).name for s in page["sources"]] == ["in.md"]


class TestTraversal:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("limit", [1, 2, 3])
    async def test_pages_cover_the_visible_listing_once_in_order(self, matrix, monkeypatch, limit):
        """Through the server registry: template match, percent-decoding of an
        absolute-path cursor, the security exemption and ``limit`` coercion."""
        _serve(monkeypatch, matrix)
        paths, pages = await _follow(f"memtomem://sources?limit={limit}")
        assert paths == await _expected(matrix.storage)
        assert len(paths) == len(set(paths))
        assert all(len(page["sources"]) <= limit for page in pages)
        assert all(page["page_size"] == limit for page in pages)

    @pytest.mark.asyncio
    async def test_the_static_uri_and_the_template_are_both_advertised(self):
        assert "memtomem://sources" in {str(r.uri) for r in await mcp.list_resources()}
        assert TEMPLATE in {t.uri_template for t in await mcp.list_resource_templates()}

    @pytest.mark.asyncio
    async def test_response_size_does_not_follow_the_store(self, bm25_only_components, monkeypatch):
        comp, mem_dir = bm25_only_components
        await comp.storage.upsert_chunks([_chunk(mem_dir / f"f{i:03d}.md") for i in range(120)])
        _serve(monkeypatch, comp)
        first = json.loads(await resources.sources_resource())
        assert len(first["sources"]) == 50
        assert first["next"] is not None
        paths, pages = await _follow("memtomem://sources")
        assert len(paths) == 120 and len(set(paths)) == 120
        assert [len(p["sources"]) for p in pages] == [50, 50, 20]


class TestPageBoundary:
    @pytest.fixture
    async def files(self, bm25_only_components, monkeypatch):
        comp, mem_dir = bm25_only_components
        await comp.storage.upsert_chunks([_chunk(mem_dir / f"{n}.md") for n in ("a", "b", "c")])
        _serve(monkeypatch, comp)
        return comp, mem_dir

    @pytest.mark.asyncio
    async def test_exactly_one_page_ends_without_a_next(self, files):
        page = await _read("memtomem://sources?limit=3")
        assert len(page["sources"]) == 3
        assert page["next"] is None

    @pytest.mark.asyncio
    async def test_one_more_file_than_the_page_continues(self, files):
        comp, mem_dir = files
        await comp.storage.upsert_chunks([_chunk(mem_dir / "d.md")])
        first = await _read("memtomem://sources?limit=3")
        assert [Path(s["path"]).name for s in first["sources"]] == ["a.md", "b.md", "c.md"]
        second = await _read(first["next"])
        assert [Path(s["path"]).name for s in second["sources"]] == ["d.md"]
        assert second["next"] is None

    @pytest.mark.asyncio
    async def test_cursor_past_the_end_is_an_empty_page(self, files):
        comp, mem_dir = files
        page = await _read(f"memtomem://sources?cursor={mem_dir / 'z.md'}")
        assert page == {"sources": [], "page_size": 50, "next": None}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("limit", [0, 501])
    async def test_limit_out_of_range_is_an_error_object(self, files, limit):
        page = await _read(f"memtomem://sources?limit={limit}")
        assert page == {"error": f"limit must be between 1 and 500, got {limit}"}


class TestOtherConsumersUntouched:
    @pytest.mark.asyncio
    async def test_unpaged_aggregate_still_lists_hidden_sources(self, matrix):
        """Web routes, consolidation and the index engine keep the complete aggregate."""
        names = {Path(r[0]).name for r in await matrix.storage.get_source_files_with_counts()}
        assert {"held.md", "pending.md", "archived.md"} <= names
