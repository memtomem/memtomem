"""Read-only storage methods use the read pool, except inside their own transaction.

These methods used to read through ``_get_db``: inside ``transaction()`` they saw
the owner's uncommitted writes, and from any other task they were refused with
``TransactionOwnedError`` until the owner finished. They now read through
``_get_txn_read_db``, which keeps the first behavior and replaces the second
with a read of the last committed state.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from pathlib import Path

import pytest

from helpers import make_chunk


async def _reads(storage) -> tuple[set[str], dict[str, int], int]:
    """One read per routed surface: a backend method, a mixin, the namespace ops.

    The mixin read (importance factors, one row per chunk) must agree with the
    namespace count, so a surface left on a different connection shows up as
    a mismatch.
    """
    sources = {p.name for p in await storage.get_all_source_files()}
    namespaces = dict(await storage.list_namespaces())
    in_ns = namespaces.get("routed", 0)
    assert len(await storage.get_chunk_factors(namespace="routed")) == in_ns
    return sources, namespaces, in_ns


def _chunk(source: str):
    return make_chunk(f"body of {source}", namespace="routed", source=source)


async def test_owner_sees_its_own_uncommitted_writes(storage):
    async with storage.transaction():
        await storage.upsert_chunks([_chunk("owned.md")])
        sources, namespaces, in_ns = await _reads(storage)
    assert "owned.md" in sources
    assert namespaces.get("routed") == 1
    assert in_ns == 1


async def test_another_task_reads_committed_state_during_a_transaction(storage):
    await storage.upsert_chunks([_chunk("committed.md")])
    owner_ready = asyncio.Event()
    release_owner = asyncio.Event()

    async def owner() -> None:
        async with storage.transaction():
            await storage.upsert_chunks([_chunk("pending.md")])
            owner_ready.set()
            await release_owner.wait()

    owner_task = asyncio.create_task(owner())
    await owner_ready.wait()
    try:
        # Before the pool routing these raised TransactionOwnedError.
        sources, namespaces, in_ns = await _reads(storage)
    finally:
        release_owner.set()
        await owner_task
    assert sources >= {"committed.md"}
    assert "pending.md" not in sources
    assert namespaces.get("routed") == 1
    assert in_ns == 1

    sources, _, in_ns = await _reads(storage)
    assert {"committed.md", "pending.md"} <= sources
    assert in_ns == 2


async def test_rolled_back_writes_are_not_read(storage):
    with pytest.raises(RuntimeError, match="roll back"):
        async with storage.transaction():
            await storage.upsert_chunks([_chunk("rolled.md")])
            raise RuntimeError("roll back")
    sources, namespaces, in_ns = await _reads(storage)
    assert "rolled.md" not in sources
    assert "routed" not in namespaces
    assert in_ns == 0


async def test_an_unowned_open_writer_transaction_is_not_read(storage):
    """A writer transaction nobody owns is not this task's: read committed state.

    Routing on ``connection.in_transaction`` instead of ownership would hand
    readers whatever a stranded writer left uncommitted.
    """
    await storage.upsert_chunks([_chunk("committed.md")])
    writer = storage._get_db()
    # Both columns: source files are read through the backend and mixins,
    # namespace counts through the namespace ops.
    writer.execute(
        "UPDATE chunks SET namespace='stranded', source_file=source_file || '.stranded' "
        "WHERE namespace='routed'"
    )
    assert writer.in_transaction
    assert storage._transaction_owner is None
    try:
        sources, namespaces, in_ns = await _reads(storage)
    finally:
        writer.rollback()
    assert "committed.md" in sources
    assert "stranded" not in namespaces
    assert in_ns == 1


async def test_reads_fall_back_to_the_writer_without_a_pool(storage, monkeypatch):
    await storage.upsert_chunks([_chunk("committed.md")])
    monkeypatch.setattr(storage, "_read_pool", [])
    sources, namespaces, in_ns = await _reads(storage)
    assert "committed.md" in sources
    assert namespaces.get("routed") == 1
    assert in_ns == 1


@contextmanager
def pool_reader_mid_statement(storage, monkeypatch):
    """Pin the pool to one connection with another reader's statement still open.

    SQLite keeps a connection's read transaction, and so its snapshot, until
    every statement on it is finished. Pool connections are shared across
    threads, so a read can land on a connection where another thread is still
    iterating a result, and see nothing committed since that statement began.
    """
    reader = storage._read_pool[0]
    monkeypatch.setattr(storage, "_read_pool", [reader])
    held = reader.execute("SELECT name FROM sqlite_master")
    assert held.fetchone() is not None
    try:
        yield
    finally:
        held.close()


async def test_rename_receipt_count_reads_past_an_open_pool_statement(storage, monkeypatch):
    """The rename receipt counts right after the rename commits: it reads the writer."""
    await storage.upsert_chunks([_chunk("one.md"), _chunk("two.md")])
    with pool_reader_mid_statement(storage, monkeypatch):
        await storage.upsert_chunks([_chunk("three.md")])
        assert await storage.count_chunks_by_namespace("routed") == 3


async def test_consolidation_groups_read_past_an_open_pool_statement(storage, monkeypatch):
    """Policies run in sequence: consolidation groups right after expiry deleted rows."""
    expiring = _chunk("expiring.md")
    await storage.upsert_chunks([expiring])
    with pool_reader_mid_statement(storage, monkeypatch):
        assert await storage.delete_by_source(expiring.metadata.source_file) == 1
        await storage.upsert_chunks([_chunk("kept.md")])
        groups = await storage.get_consolidation_groups(min_size=1, max_groups=10)
    assert [Path(g["source"]).name for g in groups] == ["kept.md"]


async def test_reads_after_an_observation_flush_see_it_past_an_open_pool_statement(
    storage, monkeypatch
):
    """Search history is read right after the pending observations are flushed.

    The web run views, the MCP history and suggestion tools, and reflection's
    knowledge gaps all flush, then read, so these reads stay on the writer.
    """
    from test_eval_cases import RUN_A, _seed_labeled_run

    with pool_reader_mid_statement(storage, monkeypatch):
        await _seed_labeled_run(storage)
        await storage.save_query_history("nothing found here", [0.1, 0.2], [], [])

        assert (await storage.get_search_run(RUN_A))["run_id"] == RUN_A
        assert len(await storage.get_search_feedback(RUN_A)) == 2
        assert [r["run_id"] for r in await storage.get_search_runs(limit=5)] == [RUN_A]
        history = [r["query_text"] for r in await storage.get_query_history(limit=5)]
        assert "quality query" in history
        assert "quality query" in await storage.suggest_queries("quality")
        gaps = [g["query"] for g in await storage.get_knowledge_gaps()]
        assert gaps == ["nothing found here"]
