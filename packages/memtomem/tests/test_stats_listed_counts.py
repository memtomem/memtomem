"""``GET /api/stats`` counts what the Sources tree lists (#3115).

The header totals and the Home chunk-size chart used to count every stored row,
held and pending sources included, while the Home lists already left those out
(#3109). ``SqliteBackend.get_listed_stats`` counts under the path-level hold
rule the Sources tree uses (``visible_path_sql``) and keeps the store-wide
count beside it for the Reset panel. These run against a real ``SqliteBackend``.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from helpers import isolate_config_paths
from memtomem.models import CONSOLIDATED_SUFFIX, ORIGIN_CONSOLIDATION_POLICY, Chunk, ChunkMetadata
from memtomem.web import hot_reload
from memtomem.web.app import create_app
from memtomem.web.deps import require_configured

_BUCKETS = ["0-32", "32-64", "64-128", "128-256", "256-512", "512-1024", "1024+"]

# Content lengths by bucket: the estimate is ``LENGTH(content) / 3``.
_LEN = {"0-32": 30, "32-64": 150, "64-128": 300, "128-256": 600, "256-512": 1200}


def _chunk(source: Path, bucket: str, origin: str | None = None) -> Chunk:
    return Chunk(
        content="x" * _LEN[bucket],
        metadata=ChunkMetadata(source_file=source, namespace="default", origin=origin),
        content_hash=f"hash-{uuid4().hex}",
        embedding=[0.1] * 1024,
    )


def _histogram(dist: list[dict]) -> dict[str, int]:
    assert [b["bucket"] for b in dist] == _BUCKETS
    return {b["bucket"]: b["count"] for b in dist if b["count"]}


@pytest.fixture
async def store(bm25_only_components, tmp_path_factory, monkeypatch):
    """Visible and hidden sources whose chunks fall in distinct buckets.

    Visible: ``visible.md`` (one source, two chunks in two buckets),
    ``open.md`` and its policy summary, and a non-policy file at a summary-shaped
    path whose parent is held. Hidden: a held file, a pending file, the held
    parent, its policy summary, and a mixed-origin summary of a held parent
    (one policy chunk hides the whole path).
    """
    comp, mem_dir = bm25_only_components
    isolate_config_paths(monkeypatch, tmp_path_factory.mktemp("config-home"))
    m = mem_dir
    await comp.storage.upsert_chunks(
        [
            _chunk(m / "visible.md", "0-32"),
            _chunk(m / "visible.md", "32-64"),
            _chunk(m / "open.md", "0-32"),
            _chunk(m / f"open.md{CONSOLIDATED_SUFFIX}", "0-32", ORIGIN_CONSOLIDATION_POLICY),
            _chunk(m / "parent2.md", "64-128"),
            _chunk(m / f"parent2.md{CONSOLIDATED_SUFFIX}", "0-32"),
            _chunk(m / "held.md", "64-128"),
            _chunk(m / "pending.md", "128-256"),
            _chunk(m / "parent.md", "256-512"),
            _chunk(m / f"parent.md{CONSOLIDATED_SUFFIX}", "256-512", ORIGIN_CONSOLIDATION_POLICY),
            _chunk(m / "parent3.md", "128-256"),
            _chunk(m / f"parent3.md{CONSOLIDATED_SUFFIX}", "128-256"),
            _chunk(m / f"parent3.md{CONSOLIDATED_SUFFIX}", "128-256", ORIGIN_CONSOLIDATION_POLICY),
        ]
    )
    for name in ("held.md", "parent.md", "parent2.md", "parent3.md"):
        assert await comp.storage.hold_source(m / name, "test_hold")
    assert comp.storage.queue_source_check_sync(m / "pending.md")
    return comp


# Listed: visible.md (2), open.md (1), open summary (1), parent2 summary (1).
_LISTED = {"0-32": 4, "32-64": 1}
_STORED_CHUNKS = 13
_STORED_SOURCES = 11


def _web_app(comp):
    app = create_app(lifespan=None, mode="dev")
    for name in ("storage", "index_engine", "search_pipeline", "config"):
        setattr(app.state, name, getattr(comp, name))
    app.dependency_overrides[require_configured] = lambda: None
    app.state.config_signature = hot_reload.current_signature()
    app.state.last_reload_error = None
    return app


async def _stats(comp, **params) -> dict:
    app = _web_app(comp)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.get("/api/stats", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_listed_stats_count_what_the_sources_listing_lists(store):
    storage = store.storage
    hidden = {str(p) for p in await storage.get_hidden_source_files()}
    rows = await storage.get_source_files_with_counts()
    listed_rows = [r for r in rows if str(r[0]) not in hidden]

    got = await storage.get_listed_stats()

    assert got["listed_sources"] == len(listed_rows) == 4
    assert got["listed_chunks"] == sum(r[1] for r in listed_rows) == 5
    assert (got["stored_chunks"], got["stored_sources"]) == (_STORED_CHUNKS, _STORED_SOURCES)
    # One source with chunks in two buckets counts once, and once per bucket.
    assert _histogram(got["distribution"]) == _LISTED


async def test_totals_only_reads_the_same_counts_without_a_histogram(store):
    full = await store.storage.get_listed_stats()
    totals = await store.storage.get_listed_stats(distribution=False)

    assert totals["distribution"] == []
    for key in ("listed_chunks", "listed_sources", "stored_chunks", "stored_sources"):
        assert totals[key] == full[key], key


async def test_an_empty_store_has_seven_zero_buckets(bm25_only_components):
    comp, _ = bm25_only_components
    got = await comp.storage.get_listed_stats()
    assert [b["bucket"] for b in got["distribution"]] == _BUCKETS
    assert all(b["count"] == 0 for b in got["distribution"])
    assert {k: v for k, v in got.items() if k != "distribution"} == {
        "listed_chunks": 0,
        "listed_sources": 0,
        "stored_chunks": 0,
        "stored_sources": 0,
    }


async def test_an_all_hidden_store_lists_zero_and_stores_its_rows(bm25_only_components):
    comp, mem_dir = bm25_only_components
    held = mem_dir / "held.md"
    await comp.storage.upsert_chunks([_chunk(held, "64-128"), _chunk(held, "0-32")])
    assert await comp.storage.hold_source(held, "redaction_blocked")

    got = await comp.storage.get_listed_stats()

    assert _histogram(got["distribution"]) == {}
    assert (got["listed_chunks"], got["listed_sources"]) == (0, 0)
    assert (got["stored_chunks"], got["stored_sources"]) == (2, 1)


@pytest.mark.parametrize("params", [{}, {"summary_only": "true"}], ids=["full", "summary_only"])
async def test_api_stats_leaves_held_and_pending_sources_out(store, params):
    data = await _stats(store, **params)

    assert (data["total_chunks"], data["total_sources"]) == (5, 4)
    assert data["stored_chunks"] == _STORED_CHUNKS
    assert _histogram(data["chunk_size_distribution"]) == _LISTED
    assert sum(b["count"] for b in data["chunk_size_distribution"]) == data["total_chunks"]


async def test_api_stats_totals_only_is_the_header_request(store):
    data = await _stats(store, totals_only="true")

    assert (data["total_chunks"], data["total_sources"]) == (5, 4)
    assert data["stored_chunks"] == _STORED_CHUNKS
    assert data["chunk_size_distribution"] == []
    assert data["home_sources"] == []
