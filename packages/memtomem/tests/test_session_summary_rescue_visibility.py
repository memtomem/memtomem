"""The session-summary rescue widens ranking, never visibility (#3108).

Real SQLite, BM25 only. A summary in ``archive:session:*`` links to target
chunks with ``summarizes``; an unpinned search that matches the summary pulls in
the targets' source files as a third RRF list. Two ways that used to reach past
the search's own visibility:

* the rescue legs ran with no namespace filter, so a target in ``archive:*`` or
  ``agent-runtime:*`` came back from a search that hides those namespaces;
* every target contributed its path, so a target the search cannot see (another
  project, a system namespace, a held or pending source) still pulled in a
  visible chunk sharing that path, flagged it ``via_session_summary`` and turned
  BM25 passthrough into fusion.

Each negative asserts on a visible chunk that the search does return, so an
empty result cannot pass for a screened one, and each screening case has a
positive twin where the same layout is visible and the rescue does fire.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from memtomem.config import SearchConfig, SessionSummaryConfig
from memtomem.models import (
    CONSOLIDATED_SUFFIX,
    ORIGIN_CONSOLIDATION_POLICY,
    Chunk,
    ChunkMetadata,
)
from memtomem.search.pipeline import SearchPipeline
from memtomem.storage.source_holds import REDACTION_BLOCKED_HOLD

pytestmark = pytest.mark.asyncio

DIM = 1024
ROOT_P = Path("/projects/p")
ROOT_Q = Path("/projects/q")
QUERY = "zephyr rollout"
SYSTEM_PREFIXES = ("archive:", "agent-runtime:")


class _Embedder:
    dimension = DIM
    model_name = "none"


def _chunk(
    source: Path,
    content: str,
    *,
    namespace: str = "default",
    scope: str = "user",
    root: Path | None = None,
    origin: str | None = None,
) -> Chunk:
    return Chunk(
        content=content,
        metadata=ChunkMetadata(
            source_file=source,
            namespace=namespace,
            scope=scope,
            project_root=root,
            origin=origin,
        ),
        content_hash=f"hash-{uuid4().hex}",
        embedding=[0.1] * DIM,
    )


@pytest.fixture
async def store(bm25_only_components, tmp_path):
    comp, _ = bm25_only_components
    return comp.storage, tmp_path


async def _summarize(storage, summary: Chunk, targets: list[Chunk]) -> None:
    await storage.upsert_chunks([summary, *targets])
    for target in targets:
        await storage.add_chunk_link(
            source_id=summary.id,
            target_id=target.id,
            link_type="summarizes",
            namespace_target=target.metadata.namespace,
        )


def _summary(base: Path, *, scope: str = "user", root: Path | None = None) -> Chunk:
    return _chunk(
        base / f"summary-{uuid4().hex[:6]}.md",
        f"{QUERY} session summary",
        namespace="archive:session:s1",
        scope=scope,
        root=root,
    )


def _pipeline(storage, *, system_prefixes: tuple[str, ...] = SYSTEM_PREFIXES) -> SearchPipeline:
    return SearchPipeline(
        storage=storage,
        embedder=_Embedder(),
        config=SearchConfig(
            enable_bm25=True,
            enable_dense=False,
            system_namespace_prefixes=list(system_prefixes),
        ),
        session_summary_config=SessionSummaryConfig(expansion_score_threshold=0.0),
    )


def _by_id(results):
    return {r.chunk.id: r for r in results}


# ---------------------------------------------------------------------------
# The rescue legs carry the caller's namespace filter
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("namespace", [None, [], "!system"], ids=["none", "empty-list", "!system"])
async def test_unpinned_search_does_not_rescue_system_namespace_targets(store, namespace):
    storage, base = store
    archived = _chunk(base / "archived.md", f"{QUERY} archived", namespace="archive:2026-01")
    agent = _chunk(base / "agent.md", f"{QUERY} agent write", namespace="agent-runtime:bot")
    visible = _chunk(base / "visible.md", f"{QUERY} default write")
    await _summarize(storage, _summary(base), [archived, agent, visible])

    results, _ = await _pipeline(storage).search(QUERY, top_k=10, namespace=namespace)

    found = _by_id(results)
    assert archived.id not in found
    assert agent.id not in found
    # The rescue itself still runs: the visible target is flagged.
    assert found[visible.id].via_session_summary is True


async def test_visible_target_is_rescued_beside_an_excluded_sibling(store):
    """The target's path is shared with an archived row: the target is rescued,
    the archived row is not. Pins the leg filter on its own — target screening
    keeps this path, so only the leg's namespace filter can drop the sibling."""
    storage, base = store
    path = base / "shared.md"
    target = _chunk(path, f"{QUERY} live note")
    archived_sibling = _chunk(path, f"{QUERY} archived copy", namespace="archive:2026-01")
    await storage.upsert_chunks([archived_sibling])
    await _summarize(storage, _summary(base), [target])

    results, _ = await _pipeline(storage).search(QUERY, top_k=10)

    found = _by_id(results)
    assert archived_sibling.id not in found
    assert found[target.id].via_session_summary is True


async def test_no_system_prefixes_means_archive_targets_are_rescued(store):
    storage, base = store
    archived = _chunk(base / "archived.md", f"{QUERY} archived", namespace="archive:2026-01")
    await _summarize(storage, _summary(base), [archived])

    results, _ = await _pipeline(storage, system_prefixes=()).search(QUERY, top_k=10)

    assert _by_id(results)[archived.id].via_session_summary is True


# ---------------------------------------------------------------------------
# A target the search cannot see does not boost a visible chunk sharing its path
# ---------------------------------------------------------------------------


async def _assert_not_boosted(storage, sibling: Chunk, **search_kwargs) -> None:
    results, stats = await _pipeline(storage).search(QUERY, top_k=10, **search_kwargs)
    found = _by_id(results)
    assert sibling.id in found, "the sibling is visible and matches organically"
    assert found[sibling.id].via_session_summary is False
    assert stats.score_scale == "bm25"


async def _assert_boosted(storage, sibling: Chunk, **search_kwargs) -> None:
    results, stats = await _pipeline(storage).search(QUERY, top_k=10, **search_kwargs)
    assert _by_id(results)[sibling.id].via_session_summary is True
    assert stats.score_scale == "rrf"


@pytest.mark.parametrize("visible", [False, True], ids=["other-project", "same-project"])
async def test_out_of_scope_target(store, visible):
    storage, base = store
    path = base / "mixed.md"
    target = _chunk(
        path, f"{QUERY} project row", scope="project_local", root=ROOT_P if visible else ROOT_Q
    )
    sibling = _chunk(path, f"{QUERY} user row")
    await storage.upsert_chunks([sibling])
    await _summarize(storage, _summary(base), [target])

    check = _assert_boosted if visible else _assert_not_boosted
    await check(storage, sibling, project_context_root=ROOT_P)


@pytest.mark.parametrize("namespace", ["agent-runtime:bot", "archive:old"])
async def test_system_namespace_target(store, namespace):
    storage, base = store
    path = base / "mixed.md"
    target = _chunk(path, f"{QUERY} hidden row", namespace=namespace)
    sibling = _chunk(path, f"{QUERY} default row")
    await storage.upsert_chunks([sibling])
    await _summarize(storage, _summary(base), [target])

    await _assert_not_boosted(storage, sibling)


async def _policy_layout(storage, base: Path) -> tuple[Path, Chunk]:
    """A consolidated file holding a policy summary (the target) and a
    non-policy row (the sibling). Hiding the parent hides only the policy row."""
    parent = base / "notes.md"
    derived = Path(f"{parent}{CONSOLIDATED_SUFFIX}")
    target = _chunk(derived, f"{QUERY} policy summary", origin=ORIGIN_CONSOLIDATION_POLICY)
    sibling = _chunk(derived, f"{QUERY} hand-written row")
    # The parent must be indexed: a hold or pending check on an unindexed path
    # records nothing.
    await storage.upsert_chunks([_chunk(parent, "parent body"), sibling])
    await _summarize(storage, _summary(base), [target])
    return parent, sibling


@pytest.mark.parametrize("reason", [REDACTION_BLOCKED_HOLD, "scan_missing"])
async def test_policy_target_of_a_held_parent(store, reason):
    storage, base = store
    parent, sibling = await _policy_layout(storage, base)
    await _assert_boosted(storage, sibling)

    assert await storage.hold_source(parent, reason)

    await _assert_not_boosted(storage, sibling)


async def test_policy_target_of_a_pending_parent(store):
    storage, base = store
    parent, sibling = await _policy_layout(storage, base)
    await _assert_boosted(storage, sibling)

    assert storage.queue_source_check_sync(parent)

    await _assert_not_boosted(storage, sibling)


@pytest.mark.parametrize("target_scope", ["user", "project_shared"])
async def test_explicit_scope_screens_targets_like_retrieval(store, target_scope):
    """Out of a project, ``scope=project_shared`` selects project-shared rows
    only. A user-scope target is outside that selection even though the
    id-access boundary would admit it; a project-shared one is inside."""
    storage, base = store
    path = base / "mixed.md"
    target_root = ROOT_Q if target_scope == "project_shared" else None
    target = _chunk(path, f"{QUERY} target row", scope=target_scope, root=target_root)
    sibling = _chunk(path, f"{QUERY} shared row", scope="project_shared", root=ROOT_P)
    await storage.upsert_chunks([sibling])
    # The summary itself is inside the selection, so the lookup does find it.
    await _summarize(storage, _summary(base, scope="project_shared", root=ROOT_P), [target])

    check = _assert_boosted if target_scope == "project_shared" else _assert_not_boosted
    await check(storage, sibling, scope="project_shared")
