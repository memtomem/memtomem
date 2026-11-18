"""Query expansion and the hidden-row count stay inside the search's boundary (#3039).

Expansion appends tag names and heading words to the query before retrieval, and
the expanded query is embedded (the heading probe and the dense leg) and handed to
the reranker. Sampled from outside the search's scope, project or namespace, those
terms would reach a remote embedder or an external reranker on a search that
excludes the rows they came from. The hidden-row hint has the same shape: it
counts rows the search could return once it named the namespace.

Every expansion case asserts that the allowed terms *were* added and that the
expansion did not fail: both helpers return the plain query when they fail, so the
absence of a forbidden term alone would prove nothing.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from memtomem.config import QueryExpansionConfig, RerankConfig, SearchConfig
from memtomem.models import (
    CONSOLIDATED_SUFFIX,
    ORIGIN_CONSOLIDATION_POLICY,
    Chunk,
    ChunkMetadata,
    ScopeFilter,
    SearchResult,
)
from memtomem.search.pipeline import SearchPipeline

pytestmark = pytest.mark.asyncio

ROOT_P = Path("/projects/p")
ROOT_Q = Path("/projects/q")
DIM = 1024

# One row per region. The tag contains a query word ("zephyr") but not the whole
# query, which tag expansion would treat as already present; the heading word is
# what heading expansion appends.
REGIONS = {
    "user": dict(scope="user", root=None, namespace="default", tag="zephyr-u", word="ucedar"),
    "p": dict(
        scope="project_local", root=ROOT_P, namespace="default", tag="zephyr-p", word="pmaple"
    ),
    "q": dict(
        scope="project_local", root=ROOT_Q, namespace="default", tag="zephyr-q", word="qbirch"
    ),
    "archive": dict(
        scope="user", root=None, namespace="archive:old", tag="zephyr-a", word="aelmtree"
    ),
}


def _chunk(region: str, *, namespace: str | None = None, scope: str | None = None) -> Chunk:
    spec = REGIONS[region]
    root = spec["root"]
    base = root if root is not None else Path("/home/u")
    return Chunk(
        content=f"zephyr rollout notes for {region}",
        metadata=ChunkMetadata(
            source_file=base / ".memtomem" / "memories" / f"{region}-{uuid4().hex[:6]}.md",
            tags=(spec["tag"],),
            namespace=namespace or spec["namespace"],
            heading_hierarchy=(f"## {spec['word'].capitalize()}",),
            scope=scope or spec["scope"],
            project_root=root,
        ),
        content_hash=f"hash-{uuid4().hex}",
        embedding=[0.1] * DIM,
    )


@pytest.fixture
async def storage(bm25_only_components):
    comp, _ = bm25_only_components
    await comp.storage.upsert_chunks([_chunk(region) for region in REGIONS])
    return comp.storage


class _RecordingEmbedder:
    dimension = DIM

    def __init__(self) -> None:
        self.texts: list[str] = []

    async def embed_query(self, query: str) -> list[float]:
        self.texts.append(query)
        return [0.1] * DIM


class _RecordingReranker:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def rerank(self, query: str, results: list[SearchResult], top_k: int):
        self.queries.append(query)
        return results[:top_k]


def _pipeline(storage, embedder, reranker) -> SearchPipeline:
    return SearchPipeline(
        storage=storage,
        embedder=embedder,
        config=SearchConfig(enable_bm25=True, enable_dense=True),
        expansion_config=QueryExpansionConfig(enabled=True, strategy="both", max_terms=10),
        reranker=reranker,
        rerank_config=RerankConfig(enabled=True),
    )


async def _expanded(storage, monkeypatch, **search_kwargs) -> tuple[str, list[str]]:
    """Run one search; return the BM25 query and every text a provider received."""
    embedder, reranker = _RecordingEmbedder(), _RecordingReranker()
    bm25_queries: list[str] = []
    real_bm25 = storage.bm25_search

    async def recording_bm25(query, *args, **kwargs):
        bm25_queries.append(query)
        return await real_bm25(query, *args, **kwargs)

    monkeypatch.setattr(storage, "bm25_search", recording_bm25)
    _results, stats = await _pipeline(storage, embedder, reranker).search(
        "zephyr rollout", top_k=10, **search_kwargs
    )
    assert not stats.expansion_failed
    assert len(bm25_queries) == 1, bm25_queries
    # The heading probe embeds the tag-expanded query, the dense leg the final one.
    assert len(embedder.texts) == 2, embedder.texts
    assert reranker.queries, "the reranker did not run"
    return bm25_queries[0], [*embedder.texts, *reranker.queries, *bm25_queries]


def _terms(region: str) -> tuple[str, str]:
    return REGIONS[region]["tag"], REGIONS[region]["word"]


@pytest.mark.parametrize(
    ("search_kwargs", "allowed", "forbidden"),
    [
        pytest.param(
            {"scope": "user", "project_context_root": ROOT_P},
            {"user"},
            {"p", "q", "archive"},
            id="user-scope-inside-a-project",
        ),
        pytest.param(
            {"project_context_root": ROOT_P},
            {"user", "p"},
            {"q", "archive"},
            id="default-inside-a-project",
        ),
        pytest.param(
            {"scope": "project_local"},
            {"p", "q"},
            {"user", "archive"},
            id="explicit-project-scope-out-of-project",
        ),
        pytest.param(
            {"namespace": "archive:*", "project_context_root": ROOT_P},
            {"archive"},
            {"user", "p", "q"},
            id="explicit-hidden-namespace",
        ),
    ],
)
async def test_expansion_samples_only_the_search_boundary(
    storage, monkeypatch, search_kwargs, allowed, forbidden
):
    final_query, provider_texts = await _expanded(storage, monkeypatch, **search_kwargs)

    for region in allowed:
        for term in _terms(region):
            assert term in final_query.split(), (region, term, final_query)
    for region in forbidden:
        for term in _terms(region):
            for text in provider_texts:
                assert term not in text, (region, term, text)


async def test_tag_counts_without_filters_keep_the_project_boundary(storage):
    """The tag listings (web, MCP resource, CLI) pass no filter and are unchanged."""
    inside = dict(await storage.get_tag_counts(project_context_root=ROOT_P))
    outside = dict(await storage.get_tag_counts())

    assert set(inside) == {"zephyr-u", "zephyr-p", "zephyr-a"}
    assert set(outside) == {"zephyr-u", "zephyr-a"}


async def test_tag_counts_apply_the_scope_and_namespace_filters(storage):
    from memtomem.models import NamespaceFilter

    hidden = NamespaceFilter.parse(None, system_prefixes=("archive:", "agent-runtime:"))
    user_only = dict(
        await storage.get_tag_counts(
            project_context_root=ROOT_P,
            scope_filter=ScopeFilter.parse("user"),
            namespace_filter=hidden,
        )
    )
    cross_project = dict(
        await storage.get_tag_counts(scope_filter=ScopeFilter.parse("project_local"))
    )

    assert set(user_only) == {"zephyr-u"}
    assert set(cross_project) == {"zephyr-p", "zephyr-q"}


# ---------------------------------------------------------------------------
# Hidden-row count
# ---------------------------------------------------------------------------

PREFIXES = ["archive:", "agent-runtime:"]


@pytest.fixture
async def hidden_rows(bm25_only_components):
    """One row under each default system prefix in each of three regions.

    Two prefixes, so a scope clause that bound only to the last OR-ed prefix
    would still count the first prefix's out-of-scope rows.
    """
    comp, _ = bm25_only_components
    rows = []
    for region in ("user", "p", "q"):
        rows.append(_chunk(region, namespace="archive:x"))
        rows.append(_chunk(region, namespace="agent-runtime:bot"))
    await comp.storage.upsert_chunks(rows)
    return comp.storage


@pytest.mark.parametrize(
    ("boundary", "total", "each"),
    [
        pytest.param((ScopeFilter.parse("user"), ROOT_P), 2, 1, id="user-scope-in-p"),
        pytest.param((None, ROOT_P), 4, 2, id="default-in-p"),
        pytest.param((None, None), 2, 1, id="default-out-of-project"),
        pytest.param((ScopeFilter.parse("project_local"), None), 4, 2, id="explicit-project"),
    ],
)
async def test_hidden_count_covers_only_the_boundary(hidden_rows, boundary, total, each):
    count, by_prefix = await hidden_rows.count_chunks_by_ns_prefix_detail(
        PREFIXES, boundary=boundary
    )

    assert count == total
    assert by_prefix == {"archive:": each, "agent-runtime:": each}


async def test_hidden_count_without_a_boundary_counts_every_row(hidden_rows):
    count, by_prefix = await hidden_rows.count_chunks_by_ns_prefix_detail(PREFIXES)

    assert count == 6
    assert by_prefix == {"archive:": 3, "agent-runtime:": 3}


async def test_search_reports_hidden_rows_of_its_own_boundary(hidden_rows):
    pipeline = SearchPipeline(
        storage=hidden_rows,
        embedder=_RecordingEmbedder(),
        config=SearchConfig(enable_bm25=True, enable_dense=False),
    )

    _results, stats = await pipeline.search("zephyr", scope="user", project_context_root=ROOT_P)

    assert stats.hidden_system_ns == 2
    assert stats.hidden_by_prefix == {"archive:": 1, "agent-runtime:": 1}


async def _hide(storage, source: Path, how: str) -> None:
    if how == "pending":
        assert storage.queue_source_check_sync(source)
    else:
        assert await storage.hold_source(source, how)


@pytest.mark.parametrize("how", ["redaction_blocked", "scan_missing", "pending"])
async def test_hidden_count_leaves_out_rows_search_hides_by_source(
    bm25_only_components, tmp_path, how
):
    """#3108: a held or pending source's rows are not returned by a search that
    names their namespace, so the hint must not advertise them. The raw count
    (no boundary) still sees them."""
    comp, _ = bm25_only_components
    kept = _chunk("user", namespace="archive:x")
    hidden = _chunk("user", namespace="archive:x")
    # A real directory: indexing stores the resolved path, while the pending
    # journal deliberately does not resolve, so a path through a symlink (macOS
    # ``/home``) never matches its row and nothing is queued.
    hidden.metadata = replace(hidden.metadata, source_file=tmp_path / "hidden.md")
    await comp.storage.upsert_chunks([kept, hidden])
    await _hide(comp.storage, hidden.metadata.source_file, how)

    assert await comp.storage.count_chunks_by_ns_prefix_detail(PREFIXES, boundary=(None, None)) == (
        1,
        {"archive:": 1},
    )
    assert await comp.storage.count_chunks_by_ns_prefix_detail(PREFIXES) == (2, {"archive:": 2})

    pipeline = SearchPipeline(
        storage=comp.storage,
        embedder=_RecordingEmbedder(),
        config=SearchConfig(enable_bm25=True, enable_dense=False),
    )
    named, _ = await pipeline.search("zephyr", namespace="archive:*")
    assert {r.chunk.id for r in named} == {kept.id}


async def test_hidden_count_leaves_out_policy_rows_of_a_held_parent(bm25_only_components):
    """A consolidation-policy summary is hidden with its parent's hold; the
    hand-written row in the same consolidated file is not."""
    comp, _ = bm25_only_components
    parent = _chunk("user", namespace="default")
    derived = Path(f"{parent.metadata.source_file}{CONSOLIDATED_SUFFIX}")
    policy = _chunk("user", namespace="archive:x")
    policy.metadata = replace(
        policy.metadata, source_file=derived, origin=ORIGIN_CONSOLIDATION_POLICY
    )
    manual = _chunk("user", namespace="archive:x")
    manual.metadata = replace(manual.metadata, source_file=derived)
    await comp.storage.upsert_chunks([parent, policy, manual])
    assert await comp.storage.hold_source(parent.metadata.source_file, "scan_missing")

    assert await comp.storage.count_chunks_by_ns_prefix_detail(PREFIXES, boundary=(None, None)) == (
        1,
        {"archive:": 1},
    )


# ---------------------------------------------------------------------------
# The heading probe's cost on a sparse boundary
# ---------------------------------------------------------------------------


class _KnnRecordingDB:
    """Delegates to the read connection and records every KNN query's inner K
    (the pattern of ``test_dense_knn_k_cap.py``)."""

    def __init__(self, inner, ks: list[int]):
        self._inner = inner
        self._ks = ks

    def execute(self, sql, params=None):
        if params is not None and "embedding MATCH" in sql:
            self._ks.append(params[1])
        return self._inner.execute(sql) if params is None else self._inner.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.mark.parametrize(
    ("escalate", "exhaustive", "inner_ks"),
    [
        pytest.param(True, False, [100, 151], id="escalating"),
        pytest.param(False, False, [100], id="first-attempt-only"),
        pytest.param(False, True, [151], id="exhaustive-ignores-it"),
    ],
)
async def test_a_sparse_boundary_escalates_only_when_asked(
    bm25_only_components, escalate, exhaustive, inner_ks
):
    """150 hidden rows and one visible: fewer than ``top_k`` rows survive the filter
    whatever the first KNN pool (K=100) holds, so an escalating search always tries
    the next K, here the whole table (151 rows). The heading probe asks for the
    first attempt only; an exhaustive search is one full pass either way."""
    from memtomem.models import NamespaceFilter

    comp, _ = bm25_only_components
    rows = [_chunk("archive", namespace="archive:bulk") for _ in range(150)]
    rows.append(_chunk("user"))
    await comp.storage.upsert_chunks(rows)

    ks: list[int] = []
    inner = comp.storage._get_read_db()
    comp.storage._get_read_db = lambda: _KnnRecordingDB(inner, ks)  # type: ignore[method-assign]
    hidden = NamespaceFilter.parse(None, system_prefixes=("archive:",))

    results = await comp.storage.dense_search(
        [0.1] * DIM,
        top_k=3,
        namespace_filter=hidden,
        exhaustive=exhaustive,
        escalate=escalate,
    )

    assert ks == inner_ks
    assert all(r.chunk.metadata.namespace == "default" for r in results)
