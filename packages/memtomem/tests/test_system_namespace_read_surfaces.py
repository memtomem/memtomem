"""Read surfaces that list chunks hide the system namespaces by default (#3117).

``mem_search``, ``mem_recall``, ``mm recall`` and the web timeline hide
``search.system_namespace_prefixes`` (``archive:*``, ``agent-runtime:*``) when
no namespace is named. The interactive shell's ``recall``, ``GET
/api/procedures`` and ``GET /api/chunks/{id}/similar`` called storage with no
namespace filter and listed those rows next to live ones.

Each test drives the real surface against a real store holding two chunks in
each of two ordinary and two system namespaces, and compares the namespaces
(or ids) that come back with the expected set. Present-and-absent both: an
absence-only check would also pass on a surface that returned nothing.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from memtomem.models import Chunk, ChunkMetadata

ORDINARY = {"default", "personal"}
SYSTEM = {"archive:old", "agent-runtime:w1"}
_PREFIXES = ("archive:", "agent-runtime:")
_VECTOR = [0.1] * 1024


def _chunk(path: Path, namespace: str, n: int) -> Chunk:
    return Chunk(
        content=f"zephyrword step {n} stored in {namespace}",
        metadata=ChunkMetadata(
            source_file=path, namespace=namespace, tags=("procedure",), start_line=n
        ),
        content_hash=f"hash-{uuid4().hex}",
        embedding=list(_VECTOR),
    )


@pytest.fixture
async def seeded(bm25_only_components):
    comp, mem_dir = bm25_only_components
    assert tuple(comp.config.search.system_namespace_prefixes) == _PREFIXES
    chunks = [
        _chunk(mem_dir / (ns.replace(":", "-") + ".md"), ns, n)
        for ns in sorted(ORDINARY | SYSTEM)
        for n in (1, 2)
    ]
    await comp.storage.upsert_chunks(chunks)
    by_ns: dict[str, list[Chunk]] = {}
    for c in chunks:
        by_ns.setdefault(c.metadata.namespace, []).append(c)
    return comp, by_ns


def _no_system_prefixes(comp) -> None:
    comp.config.search.system_namespace_prefixes = []
    assert comp.config.search.system_namespace_prefixes == []


# ---------------------------------------------------------------------------
# mm shell → recall
# ---------------------------------------------------------------------------


def _recalled_namespaces(out: str) -> list[str]:
    # Each row prints ``  <source> <snippet>``; the snippet ends with the namespace.
    return [line.rsplit("stored in ", 1)[1] for line in out.splitlines() if "stored in " in line]


class TestShellRecall:
    @pytest.mark.asyncio
    async def test_hides_the_system_namespaces(self, seeded, capsys):
        from memtomem.cli.shell import _cmd_recall

        comp, _ = seeded
        await _cmd_recall(comp, [])

        namespaces = _recalled_namespaces(capsys.readouterr().out)
        assert sorted(namespaces) == sorted([ns for ns in ORDINARY for _ in (1, 2)])

    @pytest.mark.asyncio
    async def test_an_empty_prefix_list_hides_nothing(self, seeded, capsys):
        from memtomem.cli.shell import _cmd_recall

        comp, _ = seeded
        _no_system_prefixes(comp)
        await _cmd_recall(comp, [])

        assert set(_recalled_namespaces(capsys.readouterr().out)) == ORDINARY | SYSTEM


# ---------------------------------------------------------------------------
# Web
# ---------------------------------------------------------------------------


def _web_app(comp):
    from memtomem.web.app import create_app

    # ``procedures`` is a dev-only router.
    app = create_app(lifespan=None, mode="dev")
    app.state.csrf_enforce = False
    app.state.storage = comp.storage
    app.state.search_pipeline = comp.search_pipeline
    app.state.index_engine = comp.index_engine
    app.state.embedder = comp.embedder
    app.state.config = comp.config
    return app


async def _get(app, url: str, **params):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(url, params=params)


def _ids(chunks) -> set[str]:
    return {str(c.id) for c in chunks}


class TestProcedures:
    @pytest.mark.asyncio
    async def test_hides_the_system_namespaces(self, seeded):
        comp, by_ns = seeded
        resp = await _get(_web_app(comp), "/api/procedures")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert {p["id"] for p in body["procedures"]} == _ids(
            c for ns in ORDINARY for c in by_ns[ns]
        )
        assert body["total"] == 4

    @pytest.mark.asyncio
    async def test_a_named_agent_namespace_reads_it(self, seeded):
        comp, by_ns = seeded
        resp = await _get(_web_app(comp), "/api/procedures", namespace="agent-runtime:w1")

        assert resp.status_code == 200, resp.text
        assert {p["id"] for p in resp.json()["procedures"]} == _ids(by_ns["agent-runtime:w1"])

    @pytest.mark.asyncio
    async def test_a_blank_namespace_is_the_default_view(self, seeded):
        # Parsed as given, ``""`` is an exact namespace that matches no row.
        comp, _ = seeded
        resp = await _get(_web_app(comp), "/api/procedures", namespace="")

        assert resp.status_code == 200, resp.text
        assert {p["namespace"] for p in resp.json()["procedures"]} == ORDINARY

    @pytest.mark.asyncio
    async def test_a_malformed_namespace_is_422(self, seeded):
        comp, _ = seeded
        resp = await _get(_web_app(comp), "/api/procedures", namespace="a,b*")

        assert resp.status_code == 422
        assert "comma list" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_an_empty_prefix_list_hides_nothing(self, seeded):
        comp, _ = seeded
        _no_system_prefixes(comp)
        resp = await _get(_web_app(comp), "/api/procedures")

        assert resp.status_code == 200, resp.text
        assert {p["namespace"] for p in resp.json()["procedures"]} == ORDINARY | SYSTEM


class TestSimilarChunks:
    @pytest.mark.asyncio
    async def test_neighbours_hide_the_system_namespaces(self, seeded):
        comp, by_ns = seeded
        source = by_ns["default"][0]
        resp = await _get(_web_app(comp), f"/api/chunks/{source.id}/similar", top_k=10)

        assert resp.status_code == 200, resp.text
        expected = _ids(c for ns in ORDINARY for c in by_ns[ns]) - {str(source.id)}
        assert {r["chunk"]["id"] for r in resp.json()["results"]} == expected

    @pytest.mark.asyncio
    async def test_a_system_source_still_gets_only_live_neighbours(self, seeded):
        # The chunk asked about is read by its id (ADR-0036); its archived
        # sibling is a search result, so it stays hidden.
        comp, by_ns = seeded
        source = by_ns["archive:old"][0]
        resp = await _get(_web_app(comp), f"/api/chunks/{source.id}/similar", top_k=10)

        assert resp.status_code == 200, resp.text
        assert {r["chunk"]["id"] for r in resp.json()["results"]} == _ids(
            c for ns in ORDINARY for c in by_ns[ns]
        )

    @pytest.mark.asyncio
    async def test_a_named_namespace_reads_it(self, seeded):
        comp, by_ns = seeded
        source = by_ns["default"][0]
        resp = await _get(
            _web_app(comp),
            f"/api/chunks/{source.id}/similar",
            top_k=10,
            namespace="agent-runtime:w1",
        )

        assert resp.status_code == 200, resp.text
        assert {r["chunk"]["id"] for r in resp.json()["results"]} == _ids(by_ns["agent-runtime:w1"])

    @pytest.mark.asyncio
    async def test_a_blank_namespace_is_the_default_view(self, seeded):
        comp, by_ns = seeded
        source = by_ns["default"][0]
        resp = await _get(
            _web_app(comp), f"/api/chunks/{source.id}/similar", top_k=10, namespace=""
        )

        assert resp.status_code == 200, resp.text
        assert {r["chunk"]["namespace"] for r in resp.json()["results"]} == ORDINARY

    @pytest.mark.asyncio
    async def test_a_malformed_namespace_is_422(self, seeded):
        comp, by_ns = seeded
        source = by_ns["default"][0]
        resp = await _get(_web_app(comp), f"/api/chunks/{source.id}/similar", namespace="a,b*")

        assert resp.status_code == 422
        assert "comma list" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_an_empty_prefix_list_hides_nothing(self, seeded):
        comp, by_ns = seeded
        _no_system_prefixes(comp)
        source = by_ns["default"][0]
        resp = await _get(_web_app(comp), f"/api/chunks/{source.id}/similar", top_k=10)

        assert resp.status_code == 200, resp.text
        assert {r["chunk"]["namespace"] for r in resp.json()["results"]} == ORDINARY | SYSTEM

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("source_ns", "namespace", "requested"),
        [
            # The source can come back, so one extra row makes room for it.
            ("default", None, 6),
            # The filter drops the source: the extra row could only be filled
            # by another neighbour and would widen the KNN pass for nothing.
            ("archive:old", None, 5),
            ("default", "agent-runtime:w1", 5),
            ("archive:old", "archive:old", 6),
        ],
    )
    async def test_requests_a_row_for_the_source_only_when_it_can_come_back(
        self, seeded, source_ns, namespace, requested
    ):
        comp, by_ns = seeded
        source = by_ns[source_ns][0]
        seen: list[int] = []
        real = comp.storage.dense_search

        async def spy(embedding, top_k=20, **kwargs):
            seen.append(top_k)
            return await real(embedding, top_k=top_k, **kwargs)

        comp.storage.dense_search = spy
        params = {"top_k": 5} | ({"namespace": namespace} if namespace else {})
        resp = await _get(_web_app(comp), f"/api/chunks/{source.id}/similar", **params)

        assert resp.status_code == 200, resp.text
        assert seen == [requested]
        assert str(source.id) not in {r["chunk"]["id"] for r in resp.json()["results"]}
