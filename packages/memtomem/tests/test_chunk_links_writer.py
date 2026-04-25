"""Writer tests for ``chunk_links`` (PR-2 of the chunk_links series).

Covers the ``SqliteBackend.add_chunk_link`` surface (directly) and the
``mem_agent_share`` integration (end-to-end via the real MCP tool stack,
mirroring ``test_multi_agent_integration.TestCaseBShareTrail``). The
separate schema-level tests live in ``test_chunk_links_schema.py``.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from helpers import make_chunk
from memtomem.config import Mem2MemConfig, StorageConfig
from memtomem.constants import AGENT_NAMESPACE_PREFIX, SHARED_NAMESPACE
from memtomem.server.component_factory import close_components, create_components
from memtomem.server.context import AppContext
from memtomem.server.tools.multi_agent import (
    _SHARED_FROM_TAG_PREFIX,
    mem_agent_register,
    mem_agent_share,
)
from memtomem.storage.sqlite_backend import SqliteBackend


@pytest.fixture
async def backend(tmp_path):
    cfg = StorageConfig(sqlite_path=tmp_path / "links.db")
    be = SqliteBackend(
        config=cfg,
        dimension=0,
        embedding_provider="none",
        embedding_model="",
    )
    await be.initialize()
    yield be
    await be.close()


def _seed_chunk(backend: SqliteBackend, chunk_id: UUID, *, namespace: str = "default") -> None:
    """Insert a minimal ``chunks`` row so FK checks pass without indexing."""
    db = backend._get_db()
    db.execute(
        "INSERT INTO chunks (id, content, content_hash, source_file, namespace, "
        "tags, created_at, updated_at) "
        "VALUES (?, '', '', '', ?, '[]', '2026-01-01T00:00:00', '2026-01-01T00:00:00')",
        (str(chunk_id), namespace),
    )
    db.commit()


class TestAddChunkLinkUnit:
    """Direct unit tests for the writer — decoupled from ``mem_agent_share``."""

    @pytest.mark.asyncio
    async def test_insert_round_trip(self, backend):
        src = uuid4()
        tgt = uuid4()
        _seed_chunk(backend, src)
        _seed_chunk(backend, tgt, namespace="agent-runtime:beta")

        await backend.add_chunk_link(
            source_id=src,
            target_id=tgt,
            link_type="shared",
            namespace_target="agent-runtime:beta",
        )

        link = await backend.get_chunk_link(tgt)
        assert link is not None
        assert link.source_id == src
        assert link.target_id == tgt
        assert link.link_type == "shared"
        assert link.namespace_target == "agent-runtime:beta"

    @pytest.mark.asyncio
    async def test_reshare_overwrites_via_insert_or_replace(self, backend):
        """A second ``add_chunk_link`` on the same (target, link_type) replaces
        the row — matches the RFC §Writer idempotency contract. (Re-sharing
        the same source into a destination that was produced by a prior share
        is unusual but the writer must not raise.)
        """
        src_a = uuid4()
        src_b = uuid4()
        tgt = uuid4()
        _seed_chunk(backend, src_a)
        _seed_chunk(backend, src_b)
        _seed_chunk(backend, tgt, namespace="shared")

        await backend.add_chunk_link(
            source_id=src_a,
            target_id=tgt,
            link_type="shared",
            namespace_target="shared",
        )
        await backend.add_chunk_link(
            source_id=src_b,
            target_id=tgt,
            link_type="shared",
            namespace_target="shared",
        )

        link = await backend.get_chunk_link(tgt)
        assert link is not None
        assert link.source_id == src_b, "second write must win via INSERT OR REPLACE"

    @pytest.mark.asyncio
    async def test_null_source_id_is_accepted(self, backend):
        """Back-fill and ``ON DELETE SET NULL`` both produce NULL source rows;
        the writer must accept ``None`` without raising.
        """
        tgt = uuid4()
        _seed_chunk(backend, tgt, namespace="shared")

        await backend.add_chunk_link(
            source_id=None,
            target_id=tgt,
            link_type="shared",
            namespace_target="shared",
        )

        link = await backend.get_chunk_link(tgt)
        assert link is not None
        assert link.source_id is None

    @pytest.mark.asyncio
    async def test_invalid_link_type_raises(self, backend):
        """``_VALID_LINK_TYPES`` is the single source of truth for accepted
        values (see ``storage/sqlite_schema.py``). Extending the set is one
        PR; the writer rejecting unknowns keeps typos and accidental drift
        out of the DB.
        """
        with pytest.raises(ValueError, match="link_type="):
            await backend.add_chunk_link(
                source_id=uuid4(),
                target_id=uuid4(),
                link_type="forked_from",  # reserved future value — not enabled yet
                namespace_target="shared",
            )


# ── Integration: mem_agent_share records the link ───────────────────────

_MEMTOMEM_ENV_VARS = (
    "MEMTOMEM_EMBEDDING__PROVIDER",
    "MEMTOMEM_EMBEDDING__MODEL",
    "MEMTOMEM_EMBEDDING__DIMENSION",
    "MEMTOMEM_STORAGE__SQLITE_PATH",
    "MEMTOMEM_INDEXING__MEMORY_DIRS",
)


def _isolate_memtomem_env(monkeypatch) -> None:
    for var in _MEMTOMEM_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    import memtomem.config as _cfg

    monkeypatch.setattr(_cfg, "load_config_overrides", lambda c: None)


class _StubCtx:
    def __init__(self, app: AppContext) -> None:
        class _RC:
            pass

        self.request_context = _RC()
        self.request_context.lifespan_context = app


@pytest.fixture
async def integration_components(tmp_path, monkeypatch):
    db_path = tmp_path / "integration.db"
    mem_dir = tmp_path / "memories"
    mem_dir.mkdir()

    _isolate_memtomem_env(monkeypatch)

    config = Mem2MemConfig()
    config.storage.sqlite_path = db_path
    config.indexing.memory_dirs = [mem_dir]
    config.embedding.dimension = 1024
    config.search.enable_dense = False

    comp = await create_components(config)
    try:
        yield comp, mem_dir
    finally:
        await close_components(comp)


class TestMemAgentShareWritesLink:
    """End-to-end: ``mem_agent_share`` records a ``chunk_links`` row.

    The ``shared-from=<uuid>`` audit tag still goes into
    ``metadata.tags`` / chunk content (see ``test_multi_agent_integration``
    ``TestCaseBShareTrail`` — that path is unchanged), but structured
    provenance now also lives in the indexed table.
    """

    @pytest.mark.asyncio
    async def test_share_records_chunk_link(self, integration_components):
        comp, _ = integration_components
        app = AppContext.from_components(comp)
        ctx = _StubCtx(app)

        await mem_agent_register(agent_id="alpha", ctx=ctx)  # type: ignore[arg-type]

        source = make_chunk(
            "cache tuning strategy for the query layer",
            tags=("decision",),
            namespace=f"{AGENT_NAMESPACE_PREFIX}alpha",
        )
        await comp.storage.upsert_chunks([source])

        await mem_agent_share(  # type: ignore[arg-type]
            chunk_id=str(source.id), target=SHARED_NAMESPACE, ctx=ctx
        )

        # Find the share copy via the audit tag — same path the
        # integration tests already use.
        results, _ = await comp.search_pipeline.search(
            query="cache tuning",
            top_k=10,
            namespace=SHARED_NAMESPACE,
            tag_filter=f"{_SHARED_FROM_TAG_PREFIX}{source.id}",
        )
        assert results, "share copy must be indexed in the shared namespace"
        copy_id = results[0].chunk.id

        link = await comp.storage.get_chunk_link(copy_id)
        assert link is not None, "mem_agent_share must record a chunk_links row"
        assert link.source_id == source.id
        assert link.target_id == copy_id
        assert link.link_type == "shared"
        assert link.namespace_target == SHARED_NAMESPACE

        # Fanout lookup: the indexed source_id → targets path must
        # surface the new copy too.
        fanout = await comp.storage.get_chunks_shared_from(source.id)
        assert [link.target_id for link in fanout] == [copy_id]


class TestMemAgentShareWriterFailureNonFatal:
    """Pin the "link writer failure must not roll back the share" contract.

    The link is best-effort: the durable record is the markdown file and
    the ``shared-from=`` tag, not the ``chunk_links`` row. A failing
    writer must log and return the normal success string so callers (and
    users) don't see a confusing error for what is, from their POV, a
    completed share.
    """

    @pytest.mark.asyncio
    async def test_writer_exception_is_swallowed_and_logged(
        self, integration_components, monkeypatch, caplog
    ):
        comp, _ = integration_components
        app = AppContext.from_components(comp)
        ctx = _StubCtx(app)

        await mem_agent_register(agent_id="alpha", ctx=ctx)  # type: ignore[arg-type]

        source = make_chunk(
            "rollout plan for the next release",
            namespace=f"{AGENT_NAMESPACE_PREFIX}alpha",
        )
        await comp.storage.upsert_chunks([source])

        # Replace add_chunk_link on the storage instance with a stub that
        # raises. Using monkeypatch.setattr binds to the live instance so
        # the mem_agent_share code path picks it up at await time.
        async def _boom(*_args, **_kwargs):
            raise RuntimeError("simulated chunk_links writer failure")

        monkeypatch.setattr(comp.storage, "add_chunk_link", _boom)

        import logging

        with caplog.at_level(logging.WARNING, logger="memtomem.server.tools.multi_agent"):
            result = await mem_agent_share(  # type: ignore[arg-type]
                chunk_id=str(source.id), target=SHARED_NAMESPACE, ctx=ctx
            )

        # Contract 1: caller sees the normal success string, not an
        # error surfaced by the link writer.
        assert "Shared to namespace" in result
        assert "Error" not in result.splitlines()[0]

        # Contract 2: the warning is logged with exc_info so operators
        # can find the root cause without the share itself failing.
        assert any("chunk_links writer failed" in rec.message for rec in caplog.records), (
            f"expected warning log; got: {[r.message for r in caplog.records]}"
        )

        # Contract 3: the share copy is still indexed (the markdown file
        # was written before the link attempt; a writer failure must not
        # undo the copy).
        hits, _ = await comp.search_pipeline.search(
            query="rollout plan",
            top_k=10,
            namespace=SHARED_NAMESPACE,
            tag_filter=f"{_SHARED_FROM_TAG_PREFIX}{source.id}",
        )
        assert hits, "share copy must still be indexed after writer failure"

        # Contract 4: no chunk_links row exists for the failed copy —
        # the back-fill on the next schema-version bump can heal this.
        copy_id = hits[0].chunk.id
        # Restore a real getter by reaching through the monkeypatch'd
        # instance — monkeypatch only replaced add_chunk_link, so
        # get_chunk_link still works.
        link = await comp.storage.get_chunk_link(copy_id)
        assert link is None


class TestMemAgentShareLinkSurvivesSourceDelete:
    """When the source chunk is deleted, the link row's ``source_id`` goes
    NULL but the destination chunk and the row survive — matches the
    copy-on-share durability contract the RFC §Design calls out.
    """

    @pytest.mark.asyncio
    async def test_source_delete_nulls_link_source_id(self, integration_components):
        comp, _ = integration_components
        app = AppContext.from_components(comp)
        ctx = _StubCtx(app)

        await mem_agent_register(agent_id="alpha", ctx=ctx)  # type: ignore[arg-type]

        source = make_chunk(
            "transient rollout plan",
            namespace=f"{AGENT_NAMESPACE_PREFIX}alpha",
            source="alpha-note.md",
        )
        await comp.storage.upsert_chunks([source])

        await mem_agent_share(  # type: ignore[arg-type]
            chunk_id=str(source.id), target=SHARED_NAMESPACE, ctx=ctx
        )
        # Grab the link's target_id so we can re-query after deleting source.
        fanout = await comp.storage.get_chunks_shared_from(source.id)
        assert len(fanout) == 1
        copy_id = fanout[0].target_id

        # Delete the source chunk directly (the markdown-writer path wants
        # a real source file; for this contract test, SQL delete is the
        # cleanest way to exercise the FK trigger).
        await comp.storage.delete_chunks([source.id])

        link = await comp.storage.get_chunk_link(copy_id)
        assert link is not None, "destination must survive source delete"
        assert link.source_id is None
        assert link.target_id == copy_id
