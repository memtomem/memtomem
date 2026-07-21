"""Regression coverage for task-affine SQLite transactions (#1896)."""

import asyncio
import sqlite3

import pytest

from helpers import make_chunk
from memtomem.errors import StorageError


async def test_transaction_takes_immediate_write_lock_on_entry(storage):
    contender = sqlite3.connect(str(storage._config.sqlite_path), timeout=0)
    try:
        async with storage.transaction():
            assert storage._get_db().in_transaction is True
            with pytest.raises(sqlite3.OperationalError, match="(?i)(locked|busy)"):
                contender.execute("BEGIN IMMEDIATE")

        assert storage._get_db().in_transaction is False
    finally:
        if contender.in_transaction:
            contender.rollback()
        contender.close()


async def test_foreign_task_write_fails_closed_then_can_retry(storage):
    owner_ready = asyncio.Event()
    release_owner = asyncio.Event()

    async def owner() -> None:
        with pytest.raises(RuntimeError, match="roll back owner"):
            async with storage.transaction():
                await storage.create_session("rolled-back", "owner", "default")
                owner_ready.set()
                await release_owner.wait()
                raise RuntimeError("roll back owner")

    owner_task = asyncio.create_task(owner())
    await owner_ready.wait()
    try:
        with pytest.raises(StorageError, match="owned by another task"):
            await storage.create_session("survivor", "other", "default")
        with pytest.raises(StorageError, match="owned by another task"):
            await storage.get_session("rolled-back")
    finally:
        release_owner.set()
        await owner_task

    assert await storage.get_session("rolled-back") is None
    assert await storage.get_session("survivor") is None

    await storage.create_session("survivor", "other", "default")
    survivor = await storage.get_session("survivor")
    assert survivor is not None
    assert survivor["agent_id"] == "other"
    assert storage._get_db().in_transaction is False


async def test_foreign_unconditional_committer_cannot_flush_owner(storage):
    owner_ready = asyncio.Event()
    release_owner = asyncio.Event()

    async def owner() -> None:
        with pytest.raises(RuntimeError, match="roll back owner"):
            async with storage.transaction():
                await storage.create_session("owner-row", "owner", "default")
                owner_ready.set()
                await release_owner.wait()
                raise RuntimeError("roll back owner")

    owner_task = asyncio.create_task(owner())
    await owner_ready.wait()
    try:
        with pytest.raises(StorageError, match="owned by another task"):
            await storage.set_namespace_meta("foreign", description="other task")
    finally:
        release_owner.set()
        await owner_task

    assert await storage.get_session("owner-row") is None
    assert await storage.get_namespace_meta("foreign") is None

    await storage.set_namespace_meta("foreign", description="retry")
    metadata = await storage.get_namespace_meta("foreign")
    assert metadata is not None
    assert metadata["description"] == "retry"


async def test_foreign_namespace_writers_do_not_borrow_owner_transaction(storage):
    """Namespace writers must not report success inside another task's txn.

    These three methods suppress their own commit when they borrow an outer
    transaction. A backend-wide ownership flag used to let a foreign task take
    that branch, after which the owner's rollback silently discarded the
    foreign task's successful result.
    """
    delete_me = make_chunk(content="delete candidate", namespace="delete-ns")
    assign_me = make_chunk(content="assign candidate", namespace="assign-src")
    await storage.upsert_chunks([delete_me, assign_me])

    owner_ready = asyncio.Event()
    release_owner = asyncio.Event()

    async def owner() -> None:
        with pytest.raises(RuntimeError, match="roll back owner"):
            async with storage.transaction():
                await storage.create_session("owner-row", "owner", "default")
                owner_ready.set()
                await release_owner.wait()
                raise RuntimeError("roll back owner")

    owner_task = asyncio.create_task(owner())
    await owner_ready.wait()
    try:
        with pytest.raises(StorageError, match="owned by another task"):
            await storage.delete_by_namespace("delete-ns")
        with pytest.raises(StorageError, match="owned by another task"):
            await storage.set_namespace_meta("foreign-ns", description="foreign")
        with pytest.raises(StorageError, match="owned by another task"):
            await storage.assign_namespace("assign-dst", old_namespace="assign-src")
    finally:
        release_owner.set()
        await owner_task

    assert await storage.get_session("owner-row") is None
    assert await storage.get_chunk(delete_me.id) is not None
    assigned = await storage.get_chunk(assign_me.id)
    assert assigned is not None
    assert assigned.metadata.namespace == "assign-src"
    assert await storage.get_namespace_meta("foreign-ns") is None


async def test_pooled_reader_sees_only_committed_state(storage):
    chunk = make_chunk(content="uncommitted transaction row")
    owner_ready = asyncio.Event()
    release_owner = asyncio.Event()

    async def owner() -> None:
        with pytest.raises(RuntimeError, match="roll back owner"):
            async with storage.transaction():
                await storage.upsert_chunks([chunk])
                embeddings = await storage.get_embeddings_for_chunks([str(chunk.id)])
                assert str(chunk.id) in embeddings
                owner_ready.set()
                await release_owner.wait()
                raise RuntimeError("roll back owner")

    owner_task = asyncio.create_task(owner())
    await owner_ready.wait()
    try:
        assert await storage.get_chunk(chunk.id) is None
        assert await storage.get_embeddings_for_chunks([str(chunk.id)]) == {}
    finally:
        release_owner.set()
        await owner_task

    assert await storage.get_chunk(chunk.id) is None


async def test_writer_connection_bypasses_refuse_active_transaction(storage):
    async with storage.transaction():
        with pytest.raises(StorageError, match="rebuild_fts.*transaction is active"):
            await storage.rebuild_fts()
        with pytest.raises(StorageError, match="close.*transaction is active"):
            await storage.close()

    assert storage._get_db().in_transaction is False


async def test_self_managed_operations_reject_without_rolling_back_owner(storage):
    async with storage.transaction():
        await storage.create_session("owner-row", "owner", "default")

        with pytest.raises(
            StorageError,
            match="recover_stale_memory_candidates.*transaction is active",
        ):
            await storage.recover_stale_memory_candidates(stale_before="2026-01-01T00:00:00+00:00")
        assert storage._get_db().in_transaction is True

        with pytest.raises(
            StorageError,
            match="sweep_orphan_project_root.*transaction is active",
        ):
            await storage.sweep_orphan_project_root("/missing/project")
        assert storage._get_db().in_transaction is True

    assert await storage.get_session("owner-row") is not None


async def test_cancellation_rolls_back_and_releases_owner(storage):
    owner_ready = asyncio.Event()
    wait_forever = asyncio.Event()

    async def owner() -> None:
        async with storage.transaction():
            await storage.create_session("cancelled", "owner", "default")
            owner_ready.set()
            await wait_forever.wait()

    owner_task = asyncio.create_task(owner())
    await owner_ready.wait()
    owner_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner_task

    assert storage._transaction_owner is None
    assert storage._get_db().in_transaction is False
    assert await storage.get_session("cancelled") is None

    await storage.create_session("after-cancel", "other", "default")
    assert await storage.get_session("after-cancel") is not None


async def test_same_task_nested_transaction_raises_and_cleans_up(storage):
    with pytest.raises(StorageError, match="Nested transactions"):
        async with storage.transaction():
            async with storage.transaction():
                pass

    assert storage._transaction_owner is None
    assert storage._get_db().in_transaction is False


async def test_preexisting_connection_transaction_is_not_adopted(storage):
    db = storage._get_db()
    db.execute("BEGIN")
    try:
        with pytest.raises(StorageError, match="already has an open transaction"):
            async with storage.transaction():
                pass
        assert db.in_transaction is True
        assert storage._transaction_owner is None
    finally:
        db.rollback()
