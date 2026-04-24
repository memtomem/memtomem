"""End-to-end multi-agent scenario tests.

Pins the four contracts the multi-agent guide promises by exercising
the real MCP tool surface (``mem_agent_register`` / ``mem_search`` /
``mem_agent_search`` / ``mem_agent_share`` / ``mem_session_start``)
and the LangGraph adapter against a real BM25-only component stack.
The unit tests in ``test_multi_agent.py`` and ``test_sessions.py``
already pin each helper's contract in isolation; this file checks that
the four pieces compose end-to-end without silently regressing.

Cases:

* **A — namespace isolation** (PR-1, #457): chunks in
  ``agent-runtime:alpha`` are excluded from ``mem_search`` results
  for an agent-blind caller; ``mem_agent_search`` (which pins the
  namespace) reaches them.
* **B — share trail** (PR-3, #458): ``mem_agent_share(target="shared")``
  copies the chunk into the shared namespace with a
  ``shared-from=<source-uuid>`` audit tag, and the receiving agent's
  ``mem_agent_search(include_shared=True)`` surfaces the copy.
* **C — session→agent_id inheritance** (PR-2, #459):
  ``mem_session_start(agent_id="planner")`` lets a subsequent
  ``mem_agent_search(agent_id=None)`` resolve to
  ``agent-runtime:planner,shared`` without the caller repeating the
  identity.
* **D — LangGraph adapter** (PR-4, #460):
  ``MemtomemStore.start_agent_session("planner")`` binds the agent
  scope so ``add()`` defaults to ``agent-runtime:planner`` and
  ``search(include_shared=True)`` returns planner+shared only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from memtomem.config import Mem2MemConfig
from memtomem.constants import AGENT_NAMESPACE_PREFIX, SHARED_NAMESPACE
from memtomem.server.component_factory import close_components, create_components
from memtomem.server.context import AppContext
from memtomem.server.tools.multi_agent import (
    _SHARED_FROM_TAG_PREFIX,
    mem_agent_register,
    mem_agent_search,
    mem_agent_share,
)
from memtomem.server.tools.search import mem_search
from memtomem.server.tools.session import mem_session_end, mem_session_start

from helpers import make_chunk


class _StubCtx:
    """Minimal stand-in for MCP ``Context`` so MCP tools can be invoked
    directly. Mirrors the helper in ``test_sessions`` /
    ``test_server_degraded_mode``.
    """

    def __init__(self, app: AppContext) -> None:
        class _RC:
            pass

        self.request_context = _RC()
        self.request_context.lifespan_context = app


@pytest.fixture
async def integration_components(tmp_path, monkeypatch):
    """Real BM25-only component stack with a tmp DB + memory_dir.

    Bypasses ``~/.memtomem/config.json`` and any developer ``MEMTOMEM_*``
    env vars so the test is hermetic. Dense search is off so we don't
    pull an embedder; ``chunks_vec`` still needs a non-zero dimension
    to satisfy ``upsert_chunks``.
    """
    db_path = tmp_path / "integration.db"
    mem_dir = tmp_path / "memories"
    mem_dir.mkdir()

    for var in (
        "MEMTOMEM_EMBEDDING__PROVIDER",
        "MEMTOMEM_EMBEDDING__MODEL",
        "MEMTOMEM_EMBEDDING__DIMENSION",
        "MEMTOMEM_STORAGE__SQLITE_PATH",
        "MEMTOMEM_INDEXING__MEMORY_DIRS",
    ):
        monkeypatch.delenv(var, raising=False)

    config = Mem2MemConfig()
    config.storage.sqlite_path = db_path
    config.indexing.memory_dirs = [mem_dir]
    config.embedding.dimension = 1024
    config.search.enable_dense = False  # BM25-only — no embedder needed

    import memtomem.config as _cfg

    monkeypatch.setattr(_cfg, "load_config_overrides", lambda c: None)

    comp = await create_components(config)
    try:
        yield comp, mem_dir
    finally:
        await close_components(comp)


# ── Case A — namespace isolation (PR-1) ─────────────────────────────────


class TestCaseAIsolation:
    """End-to-end: register two agents, seed alpha's private chunk, and
    verify that the agent-blind ``mem_search`` cannot see it while
    ``mem_agent_search(agent_id="alpha")`` (or beta) reaches it as
    expected.
    """

    @pytest.mark.asyncio
    async def test_default_search_hides_other_agents_private_chunks(self, integration_components):
        comp, _ = integration_components
        app = AppContext.from_components(comp)
        ctx = _StubCtx(app)

        await mem_agent_register(agent_id="alpha", description="planner role", ctx=ctx)  # type: ignore[arg-type]
        await mem_agent_register(agent_id="beta", description="coder role", ctx=ctx)  # type: ignore[arg-type]

        # Seed a chunk in alpha's private namespace and a public one.
        await comp.storage.upsert_chunks(
            [
                make_chunk(
                    "alpha private secret architecture decision",
                    namespace=f"{AGENT_NAMESPACE_PREFIX}alpha",
                ),
                make_chunk("public note about architecture", namespace="default"),
            ]
        )

        # An agent-blind ``mem_search`` (namespace=None) must skip the
        # alpha-private chunk because ``agent-runtime:`` is in
        # ``system_namespace_prefixes``.
        out = await mem_search(query="architecture", top_k=10, ctx=ctx)  # type: ignore[arg-type]
        assert "public note about architecture" in out
        assert "alpha private secret" not in out

    @pytest.mark.asyncio
    async def test_explicit_agent_search_reaches_private_chunks(self, integration_components):
        comp, _ = integration_components
        app = AppContext.from_components(comp)
        ctx = _StubCtx(app)

        await mem_agent_register(agent_id="alpha", ctx=ctx)  # type: ignore[arg-type]
        await comp.storage.upsert_chunks(
            [
                make_chunk(
                    "alpha private architecture decision",
                    namespace=f"{AGENT_NAMESPACE_PREFIX}alpha",
                ),
            ]
        )

        # ``mem_agent_search`` pins the namespace and bypasses the
        # default isolation gate.
        out = await mem_agent_search(query="architecture", agent_id="alpha", ctx=ctx)  # type: ignore[arg-type]
        assert "alpha private architecture" in out


# ── Case B — share trail (PR-3) ─────────────────────────────────────────


class TestCaseBShareTrail:
    """End-to-end: alpha's chunk is shared into ``shared``, beta's
    ``mem_agent_search(include_shared=True)`` finds the copy, and the
    copy carries the ``shared-from=<alpha-uuid>`` audit tag.
    """

    @pytest.mark.asyncio
    async def test_share_copies_chunk_with_audit_tag(self, integration_components):
        comp, _ = integration_components
        app = AppContext.from_components(comp)
        ctx = _StubCtx(app)

        await mem_agent_register(agent_id="alpha", ctx=ctx)  # type: ignore[arg-type]
        await mem_agent_register(agent_id="beta", ctx=ctx)  # type: ignore[arg-type]

        # Seed alpha's chunk and capture its UUID for the audit assertion.
        source = make_chunk(
            "shared knowledge about our cache strategy",
            tags=("cache", "decision"),
            namespace=f"{AGENT_NAMESPACE_PREFIX}alpha",
        )
        await comp.storage.upsert_chunks([source])
        source_uuid = str(source.id)

        share_out = await mem_agent_share(  # type: ignore[arg-type]
            chunk_id=source_uuid, target=SHARED_NAMESPACE, ctx=ctx
        )
        assert SHARED_NAMESPACE in share_out

        # Inspect the shared namespace directly: there should be a copy
        # with the audit tag and original tags carried over.
        shared_chunks = []
        for ns, _count in await comp.storage.list_namespaces():
            if ns == SHARED_NAMESPACE:
                # Pull all chunks under the shared namespace via search
                # pipeline (BM25 on the shared content).
                results, _ = await comp.search_pipeline.search(
                    query="cache strategy",
                    top_k=10,
                    namespace=SHARED_NAMESPACE,
                )
                shared_chunks = [r.chunk for r in results]
        assert len(shared_chunks) >= 1, "expected at least one shared copy"
        copy = shared_chunks[0]
        assert copy.id != source.id, "share must produce a fresh UUID, not reuse the source"
        # ``mem_add`` writes tags into the markdown blockquote header
        # rather than promoting them to ``ChunkMetadata.tags`` — the
        # indexer only extracts YAML frontmatter, not the ``> tags:
        # [...]`` syntax. The audit trail therefore lives in the chunk
        # *content* (still BM25-searchable). Promoting the blockquote
        # header to first-class ``metadata.tags`` so ``tag_filter`` can
        # match ``shared-from=<id>`` is tracked as a follow-up RFC.
        assert f"{_SHARED_FROM_TAG_PREFIX}{source_uuid}" in copy.content
        assert "cache" in copy.content
        assert "decision" in copy.content

    @pytest.mark.asyncio
    async def test_receiving_agent_sees_shared_copy(self, integration_components):
        comp, _ = integration_components
        app = AppContext.from_components(comp)
        ctx = _StubCtx(app)

        await mem_agent_register(agent_id="alpha", ctx=ctx)  # type: ignore[arg-type]
        await mem_agent_register(agent_id="beta", ctx=ctx)  # type: ignore[arg-type]

        source = make_chunk(
            "alpha discovers the database connection pool tuning",
            namespace=f"{AGENT_NAMESPACE_PREFIX}alpha",
        )
        await comp.storage.upsert_chunks([source])
        await mem_agent_share(  # type: ignore[arg-type]
            chunk_id=str(source.id), target=SHARED_NAMESPACE, ctx=ctx
        )

        # Beta searches with include_shared=True (default). The shared
        # copy must surface; beta's own private chunks (none) do not.
        out = await mem_agent_search(  # type: ignore[arg-type]
            query="connection pool", agent_id="beta", include_shared=True, ctx=ctx
        )
        assert "connection pool" in out

        # Without include_shared, beta sees nothing — its private
        # namespace is empty.
        out_no_shared = await mem_agent_search(  # type: ignore[arg-type]
            query="connection pool", agent_id="beta", include_shared=False, ctx=ctx
        )
        assert "No results found" in out_no_shared


# ── Case C — session→agent_id inheritance (PR-2) ────────────────────────


class TestCaseCSessionInheritance:
    """End-to-end: after ``mem_session_start(agent_id="planner")``,
    ``mem_agent_search(agent_id=None)`` resolves to the planner's
    namespace + shared without the caller repeating the identity.
    """

    @pytest.mark.asyncio
    async def test_search_inherits_agent_id_from_session(self, integration_components):
        comp, _ = integration_components
        app = AppContext.from_components(comp)
        ctx = _StubCtx(app)

        await mem_agent_register(agent_id="planner", ctx=ctx)  # type: ignore[arg-type]

        # Seed planner's private chunk and a chunk in someone else's
        # namespace; the second must NOT surface even though
        # include_shared=True (it isn't shared).
        await comp.storage.upsert_chunks(
            [
                make_chunk(
                    "planner roadmap for Q3 release",
                    namespace=f"{AGENT_NAMESPACE_PREFIX}planner",
                ),
                make_chunk(
                    "coder Q3 implementation notes",
                    namespace=f"{AGENT_NAMESPACE_PREFIX}coder",
                ),
            ]
        )

        await mem_session_start(agent_id="planner", ctx=ctx)  # type: ignore[arg-type]
        try:
            assert app.current_agent_id == "planner"

            # No agent_id passed — the search must inherit "planner"
            # from the session context.
            out = await mem_agent_search(query="Q3", agent_id=None, ctx=ctx)  # type: ignore[arg-type]
            assert "planner roadmap" in out
            assert "coder Q3" not in out, "coder's namespace must not leak in"
        finally:
            await mem_session_end(ctx=ctx)  # type: ignore[arg-type]
        # End resets the binding.
        assert app.current_agent_id is None

    @pytest.mark.asyncio
    async def test_explicit_agent_id_overrides_session_binding(self, integration_components):
        """Explicit ``agent_id`` arg wins over the session-bound id."""
        comp, _ = integration_components
        app = AppContext.from_components(comp)
        ctx = _StubCtx(app)

        await mem_agent_register(agent_id="planner", ctx=ctx)  # type: ignore[arg-type]
        await mem_agent_register(agent_id="coder", ctx=ctx)  # type: ignore[arg-type]

        await comp.storage.upsert_chunks(
            [
                make_chunk("planner private only", namespace=f"{AGENT_NAMESPACE_PREFIX}planner"),
                make_chunk("coder private only", namespace=f"{AGENT_NAMESPACE_PREFIX}coder"),
            ]
        )

        await mem_session_start(agent_id="planner", ctx=ctx)  # type: ignore[arg-type]
        try:
            # Explicit agent_id="coder" overrides the planner session.
            out = await mem_agent_search(query="private", agent_id="coder", ctx=ctx)  # type: ignore[arg-type]
            assert "coder private" in out
            assert "planner private" not in out
        finally:
            await mem_session_end(ctx=ctx)  # type: ignore[arg-type]


# ── Case D — LangGraph adapter (PR-4) ───────────────────────────────────


class TestCaseDLangGraphAdapter:
    """End-to-end: ``MemtomemStore.start_agent_session("planner")``
    binds the agent so ``add()`` defaults to ``agent-runtime:planner``
    and ``search(include_shared=True)`` returns planner-private +
    shared only — never another agent's private namespace.
    """

    @pytest.mark.asyncio
    async def test_start_agent_session_binds_namespace_for_add_and_search(
        self, tmp_path, monkeypatch
    ):
        from memtomem.integrations.langgraph import MemtomemStore

        db_path = tmp_path / "lg.db"
        mem_dir = tmp_path / "lg_memories"
        mem_dir.mkdir()

        for var in (
            "MEMTOMEM_EMBEDDING__PROVIDER",
            "MEMTOMEM_EMBEDDING__MODEL",
            "MEMTOMEM_EMBEDDING__DIMENSION",
            "MEMTOMEM_STORAGE__SQLITE_PATH",
            "MEMTOMEM_INDEXING__MEMORY_DIRS",
        ):
            monkeypatch.delenv(var, raising=False)

        import memtomem.config as _cfg

        monkeypatch.setattr(_cfg, "load_config_overrides", lambda c: None)

        store = MemtomemStore(
            config_overrides={
                "storage": {"sqlite_path": db_path},
                "indexing": {"memory_dirs": [Path(mem_dir)]},
                "embedding": {"dimension": 1024},
                "search": {"enable_dense": False},
            }
        )
        try:
            await store.start_agent_session("planner")
            assert store._current_agent_id == "planner"

            # Seed a competing agent's private chunk via raw storage so
            # we can verify the search filter excludes it. Going through
            # ``store.add`` would write to planner's namespace by default.
            comp = await store._ensure_init()  # type: ignore[attr-defined]
            await comp.storage.upsert_chunks(
                [
                    make_chunk(
                        "coder unrelated private notes about caching",
                        namespace=f"{AGENT_NAMESPACE_PREFIX}coder",
                    ),
                    make_chunk(
                        "shared best-practice about caching",
                        namespace=SHARED_NAMESPACE,
                    ),
                ]
            )

            # ``add()`` with namespace=None defaults to the bound agent
            # namespace — verify by inspecting where the chunk lands.
            await store.add("planner notes about caching pipeline", tags=["cache"])
            ns_counts = dict(await comp.storage.list_namespaces())
            assert ns_counts.get(f"{AGENT_NAMESPACE_PREFIX}planner", 0) >= 1, (
                "store.add should default to the bound agent's private namespace"
            )

            # ``search(include_shared=True)`` returns planner private
            # plus shared, but NOT coder private.
            results = await store.search("caching", include_shared=True)
            namespaces = {r["namespace"] for r in results}
            assert f"{AGENT_NAMESPACE_PREFIX}planner" in namespaces
            assert SHARED_NAMESPACE in namespaces
            assert f"{AGENT_NAMESPACE_PREFIX}coder" not in namespaces
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_search_include_shared_true_without_agent_session_raises(
        self, tmp_path, monkeypatch
    ):
        """Pin the explicit ``ValueError`` for the silent-fallback guard."""
        from memtomem.integrations.langgraph import MemtomemStore

        db_path = tmp_path / "lg2.db"
        mem_dir = tmp_path / "lg2_memories"
        mem_dir.mkdir()

        for var in (
            "MEMTOMEM_EMBEDDING__PROVIDER",
            "MEMTOMEM_EMBEDDING__MODEL",
            "MEMTOMEM_EMBEDDING__DIMENSION",
            "MEMTOMEM_STORAGE__SQLITE_PATH",
            "MEMTOMEM_INDEXING__MEMORY_DIRS",
        ):
            monkeypatch.delenv(var, raising=False)

        import memtomem.config as _cfg

        monkeypatch.setattr(_cfg, "load_config_overrides", lambda c: None)

        store = MemtomemStore(
            config_overrides={
                "storage": {"sqlite_path": db_path},
                "indexing": {"memory_dirs": [Path(mem_dir)]},
                "embedding": {"dimension": 1024},
                "search": {"enable_dense": False},
            }
        )
        try:
            # No start_agent_session call → _current_agent_id is None.
            assert store._current_agent_id is None

            with pytest.raises(ValueError, match="active agent session"):
                await store.search("anything", include_shared=True)
        finally:
            await store.close()
