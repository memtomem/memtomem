"""Concurrency hardening tests for SqliteBackend's reader pool.

Covers:
1. Same-task nested ``transaction()`` fails immediately with ``StorageError``.
2. Unowned ``db.in_transaction`` on the writer connection fails immediately
   with ``StorageError``.
3. Concurrent ``bm25_search`` and ``dense_search`` calls lease distinct pool
   connections, queue when concurrency exceeds pool size, and run in worker
   threads without sharing a single ``sqlite3.Connection`` concurrently.
4. A sync reader never receives a leased pool connection.
5. ``close()`` waits for in-flight leased readers to drain before closing pool
   connections, and rejects new read/write requests during shutdown with
   ``StorageError("Database is closing")``.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from memtomem.config import StorageConfig
from memtomem.errors import StorageError
from memtomem.models import Chunk, ChunkMetadata, ChunkType
from memtomem.storage.sqlite_backend import SqliteBackend


def _make_chunk(
    content: str,
    *,
    source: Path,
    line: int = 1,
    embedding: list[float] | None = None,
) -> Chunk:
    return Chunk(
        id=uuid4(),
        content=content,
        embedding=embedding or [0.1, 0.2, 0.3, 0.4],
        metadata=ChunkMetadata(
            source_file=source,
            heading_hierarchy=("Test",),
            chunk_type=ChunkType.RAW_TEXT,
            start_line=line,
            end_line=line + 2,
            namespace="default",
        ),
    )


@pytest.fixture
async def storage(tmp_path: Path):
    cfg = StorageConfig(sqlite_path=str(tmp_path / "phase2_concurrency.db"))
    backend = SqliteBackend(cfg, dimension=4)
    await backend.initialize()
    try:
        yield backend
    finally:
        await backend.close()


async def test_nested_same_task_transaction_refused_immediately(storage: SqliteBackend):
    """Same-task nested transaction() raises StorageError immediately without queuing."""
    async with storage.transaction():
        with pytest.raises(StorageError, match="Nested transactions are not supported"):
            async with storage.transaction():
                pass


async def test_unowned_open_transaction_refused_immediately(storage: SqliteBackend):
    """An open SQLite transaction not owned by transaction() is refused immediately."""
    assert storage._db is not None
    storage._db.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(StorageError, match="already has an open transaction"):
            async with storage.transaction():
                pass
    finally:
        storage._db.rollback()


async def test_concurrent_bm25_and_dense_search_never_share_reader_conn(
    storage: SqliteBackend, tmp_path: Path
):
    """Concurrent searches lease exclusive readers and queue safely when concurrency > pool size."""
    chunks = [
        _make_chunk(
            f"concurrency search target {i}",
            source=tmp_path / f"s{i}.md",
            line=1,
            embedding=[0.1 * (i + 1), 0.2, 0.3, 0.4],
        )
        for i in range(5)
    ]
    await storage.upsert_chunks(chunks)

    active_conn_ids: set[int] = set()
    overlap_detected = False
    orig_run_read = storage._run_read_in_thread

    async def tracked_run_read(conn, fn):
        nonlocal overlap_detected
        cid = id(conn)
        if cid in active_conn_ids:
            overlap_detected = True
        active_conn_ids.add(cid)
        try:
            await asyncio.sleep(0.01)
            return await orig_run_read(conn, fn)
        finally:
            active_conn_ids.discard(cid)

    storage._run_read_in_thread = tracked_run_read  # type: ignore[method-assign]

    # Run 8 concurrent searches (4 BM25 + 4 Dense) against a 2-connection read pool
    tasks = []
    for _ in range(4):
        tasks.append(
            asyncio.create_task(
                storage.bm25_search("concurrency search target", project_context_root=None)
            )
        )
        tasks.append(
            asyncio.create_task(
                storage.dense_search([0.1, 0.2, 0.3, 0.4], top_k=5, project_context_root=None)
            )
        )

    results = await asyncio.gather(*tasks)
    assert not overlap_detected, "Two concurrent searches shared the same reader connection!"
    for res in results:
        assert len(res) >= 1
    assert storage._active_readers == 0
    assert not storage._in_use_readers


async def test_sync_lease_never_shares_a_leased_reader(storage: SqliteBackend):
    """With every pooled reader leased, a sync reader gets a connection of its own.

    The async leases may be held by worker threads mid-query; handing the sync
    caller one of them would run two threads on one ``sqlite3.Connection``.
    """
    pool = list(storage._read_pool)
    assert pool, "fixture must build a read pool"
    async with contextlib.AsyncExitStack() as stack:
        leased = [await stack.enter_async_context(storage._lease_read_db()) for _ in pool]
        assert {id(c) for c in leased} == {id(c) for c in pool}
        with storage._lease_read_db_sync() as db:
            assert all(db is not c for c in pool)
            assert db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
            private = db
        with pytest.raises(sqlite3.ProgrammingError):
            private.execute("SELECT 1")
        assert id(private) not in storage._schema_checked_at
    assert storage._active_readers == 0
    assert not storage._in_use_readers


async def test_close_waits_for_active_readers_and_rejects_new_requests(tmp_path: Path):
    """close() waits for in-flight leased readers to finish and rejects new reads/writes."""
    cfg = StorageConfig(sqlite_path=str(tmp_path / "phase2_close_drain.db"))
    backend = SqliteBackend(cfg, dimension=4)
    await backend.initialize()

    reader_leased = asyncio.Event()
    release_reader = asyncio.Event()
    close_finished = False

    async def slow_reader() -> int:
        async with backend._lease_read_db() as db:
            reader_leased.set()
            await release_reader.wait()
            return db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]

    reader_task = asyncio.create_task(slow_reader())
    await reader_leased.wait()

    async def run_close() -> None:
        nonlocal close_finished
        await backend.close()
        close_finished = True

    close_task = asyncio.create_task(run_close())
    await asyncio.sleep(0.02)
    # close() must still be waiting for slow_reader to release its lease
    assert not close_finished

    # While closing, new read or write requests must fail closed
    with pytest.raises(StorageError, match="Database is closing"):
        async with backend._lease_read_db():
            pass
    with pytest.raises(StorageError, match="Database is closing"):
        backend._get_db()

    release_reader.set()
    count = await reader_task
    assert count == 0
    await close_task
    assert close_finished
