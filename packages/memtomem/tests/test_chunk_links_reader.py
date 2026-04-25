"""Reader-API tests for ``chunk_links`` (PR-2 of the chunk_links series).

Covers ``get_chunk_link``, ``get_chunks_shared_from``, and the
``walk_share_chain`` provenance walker — the three methods the RFC §Reader
ships. The walker gets its own block for cycle defence and max_depth.

All tests drive ``SqliteBackend`` directly (no MCP layer) and seed the
``chunks`` table with minimal rows so the FKs line up without running the
full indexer.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from memtomem.config import StorageConfig
from memtomem.storage.sqlite_backend import SqliteBackend


@pytest.fixture
async def backend(tmp_path):
    cfg = StorageConfig(sqlite_path=tmp_path / "reader.db")
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
    db = backend._get_db()
    db.execute(
        "INSERT INTO chunks (id, content, content_hash, source_file, namespace, "
        "tags, created_at, updated_at) "
        "VALUES (?, '', '', '', ?, '[]', '2026-01-01T00:00:00', '2026-01-01T00:00:00')",
        (str(chunk_id), namespace),
    )
    db.commit()


def _raw_insert_link(
    backend: SqliteBackend,
    *,
    source_id: UUID | None,
    target_id: UUID,
    link_type: str = "shared",
    namespace_target: str = "shared",
    created_at: str = "2026-01-01T00:00:00",
) -> None:
    """Insert a ``chunk_links`` row bypassing the writer.

    Needed for tests that build cycles (A→B→A) or stress worst-case
    walker input — shapes the writer would reject because the writer is
    a one-way funnel from ``mem_agent_share``.
    """
    db = backend._get_db()
    db.execute(
        "INSERT INTO chunk_links "
        "(source_id, target_id, link_type, namespace_target, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            str(source_id) if source_id is not None else None,
            str(target_id),
            link_type,
            namespace_target,
            created_at,
        ),
    )
    db.commit()


class TestGetChunkLink:
    @pytest.mark.asyncio
    async def test_missing_target_returns_none(self, backend):
        """Unknown target UUID → ``None`` (not an error)."""
        assert await backend.get_chunk_link(uuid4()) is None

    @pytest.mark.asyncio
    async def test_link_type_narrows_lookup(self, backend):
        """The PK is ``(target_id, link_type)`` — same target with different
        types does not collide. ``get_chunk_link`` defaults to ``'shared'``.
        """
        src = uuid4()
        tgt = uuid4()
        _seed_chunk(backend, src)
        _seed_chunk(backend, tgt)
        _raw_insert_link(
            backend, source_id=src, target_id=tgt, link_type="shared", namespace_target="shared"
        )
        # No shared-type row? Lookup with default returns the one above.
        link = await backend.get_chunk_link(tgt)
        assert link is not None
        assert link.link_type == "shared"

        # An unknown link_type yields None even though a row with another
        # type exists for the same target.
        assert await backend.get_chunk_link(tgt, link_type="consolidated_from") is None


class TestGetChunksSharedFrom:
    @pytest.mark.asyncio
    async def test_empty_fanout_returns_empty_list(self, backend):
        assert await backend.get_chunks_shared_from(uuid4()) == []

    @pytest.mark.asyncio
    async def test_multiple_targets_returned_ordered(self, backend):
        """Sharing one source into N namespaces produces N link rows, all
        discoverable via the ``source_id`` index."""
        src = uuid4()
        tgt_a = uuid4()
        tgt_b = uuid4()
        tgt_c = uuid4()
        _seed_chunk(backend, src)
        for t in (tgt_a, tgt_b, tgt_c):
            _seed_chunk(backend, t, namespace="shared")

        # Insert with ascending created_at so we can assert order.
        _raw_insert_link(
            backend,
            source_id=src,
            target_id=tgt_a,
            created_at="2026-01-01T00:00:00",
        )
        _raw_insert_link(
            backend,
            source_id=src,
            target_id=tgt_b,
            created_at="2026-01-01T00:00:01",
        )
        _raw_insert_link(
            backend,
            source_id=src,
            target_id=tgt_c,
            created_at="2026-01-01T00:00:02",
        )

        rows = await backend.get_chunks_shared_from(src)
        assert [r.target_id for r in rows] == [tgt_a, tgt_b, tgt_c]

    @pytest.mark.asyncio
    async def test_link_type_filter_narrows_fanout(self, backend):
        """Passing ``link_type`` restricts the fanout to that type."""
        src = uuid4()
        tgt_a = uuid4()
        _seed_chunk(backend, src)
        _seed_chunk(backend, tgt_a, namespace="shared")

        _raw_insert_link(backend, source_id=src, target_id=tgt_a, link_type="shared")

        assert await backend.get_chunks_shared_from(src, link_type="shared") != []
        assert await backend.get_chunks_shared_from(src, link_type="consolidated_from") == []


class TestWalkShareChain:
    """Provenance walker — follows ``target_id → source_id`` back toward the
    root. Three properties pinned:

    * happy path: ``C → B → A`` with ``A`` as the original.
    * source-deleted terminal: the last row's ``source_id`` is NULL and
      the walk stops there (the NULL-terminal row is included).
    * cycle + max_depth: hand-crafted ``A → B → A`` does not deadloop.
    """

    @pytest.mark.asyncio
    async def test_walks_back_to_root(self, backend):
        a = uuid4()
        b = uuid4()
        c = uuid4()
        for cid in (a, b, c):
            _seed_chunk(backend, cid)

        # B was shared from A; C was shared from B. Walking from C gives
        # [C←B, B←A] — two rows, closest first.
        _raw_insert_link(
            backend,
            source_id=a,
            target_id=b,
            created_at="2026-01-01T00:00:00",
        )
        _raw_insert_link(
            backend,
            source_id=b,
            target_id=c,
            created_at="2026-01-01T00:00:01",
        )

        chain = await backend.walk_share_chain(c)
        assert [link.target_id for link in chain] == [c, b]
        assert chain[0].source_id == b
        assert chain[1].source_id == a

    @pytest.mark.asyncio
    async def test_stops_at_null_source_id(self, backend):
        """Back-fill + source-delete both produce NULL source rows. The
        walk must include that terminal row and stop there — the next
        step has no UUID to follow.
        """
        a = uuid4()
        b = uuid4()
        c = uuid4()
        for cid in (a, b, c):
            _seed_chunk(backend, cid)

        # A was "shared from deleted source" (source_id=NULL). B was shared
        # from A. C was shared from B.
        _raw_insert_link(backend, source_id=None, target_id=a)
        _raw_insert_link(backend, source_id=a, target_id=b)
        _raw_insert_link(backend, source_id=b, target_id=c)

        chain = await backend.walk_share_chain(c)
        assert [link.target_id for link in chain] == [c, b, a]
        assert chain[-1].source_id is None

    @pytest.mark.asyncio
    async def test_walk_from_unknown_target_is_empty(self, backend):
        assert await backend.walk_share_chain(uuid4()) == []

    @pytest.mark.asyncio
    async def test_cycle_defence(self, backend):
        """Hand-crafted cycle ``A ↔ B``: the walker must not deadloop.

        The writer cannot produce this shape (each ``mem_agent_share``
        allocates a fresh UUID) but raw SQL can, and the walker is
        defensive either way.
        """
        a = uuid4()
        b = uuid4()
        _seed_chunk(backend, a)
        _seed_chunk(backend, b)
        _raw_insert_link(backend, source_id=a, target_id=b)
        _raw_insert_link(backend, source_id=b, target_id=a)

        chain = await backend.walk_share_chain(a, max_depth=100)
        # Exits via visited set, not max_depth — bounded output.
        target_ids = [link.target_id for link in chain]
        # The visited set stops re-entry, so each chunk appears once.
        assert len(target_ids) == len(set(target_ids))
        assert len(target_ids) <= 2

    @pytest.mark.asyncio
    async def test_max_depth_caps_walk_length(self, backend):
        """Long legitimate chains still respect ``max_depth`` as a guardrail.

        10-deep chain ``c0 ← c1 ← … ← c9`` walked with ``max_depth=3``
        yields at most 3 rows.
        """
        chain_ids = [uuid4() for _ in range(10)]
        for cid in chain_ids:
            _seed_chunk(backend, cid)

        # c_i has source_id=c_{i-1} for i>=1 (c0 has source=None).
        _raw_insert_link(backend, source_id=None, target_id=chain_ids[0])
        for i in range(1, 10):
            _raw_insert_link(
                backend,
                source_id=chain_ids[i - 1],
                target_id=chain_ids[i],
                created_at=f"2026-01-01T00:00:{i:02d}",
            )

        result = await backend.walk_share_chain(chain_ids[-1], max_depth=3)
        assert len(result) == 3
        # Closest first: c9, c8, c7.
        assert [link.target_id for link in result] == chain_ids[9:6:-1]

    @pytest.mark.asyncio
    async def test_max_depth_zero_returns_empty(self, backend):
        """Degenerate input: ``max_depth=0`` yields nothing (no walk started)."""
        a = uuid4()
        _seed_chunk(backend, a)
        _raw_insert_link(backend, source_id=None, target_id=a)

        assert await backend.walk_share_chain(a, max_depth=0) == []
