"""Transaction ownership and recovery tests for namespace writers (#1888)."""

from __future__ import annotations

import pytest

from helpers import make_chunk
from memtomem.errors import StorageError


class TestDeleteByNamespaceTransactions:
    async def test_outer_transaction_commits_successful_delete(self, storage):
        await storage.upsert_chunks([make_chunk(content="delete me", namespace="doomed")])
        caller = make_chunk(content="commit with delete", namespace="caller-ns")

        async with storage.transaction():
            await storage.upsert_chunks([caller])
            assert await storage.delete_by_namespace("doomed") == 1

        assert dict(await storage.list_namespaces()) == {"caller-ns": 1}

    async def test_outer_transaction_abort_restores_successful_delete(self, storage):
        await storage.upsert_chunks([make_chunk(content="delete me", namespace="doomed")])
        caller = make_chunk(content="abort with delete", namespace="caller-ns")

        with pytest.raises(RuntimeError, match="caller aborts"):
            async with storage.transaction():
                await storage.upsert_chunks([caller])
                assert await storage.delete_by_namespace("doomed") == 1
                raise RuntimeError("caller aborts")

        assert dict(await storage.list_namespaces()) == {"doomed": 1}

    async def test_failed_delete_restores_rows_and_preserves_callers_write(self, storage):
        doomed = make_chunk(content="delete me", namespace="doomed")
        caller = make_chunk(content="keep caller write", namespace="caller-ns")
        await storage.upsert_chunks([doomed])
        await storage.set_namespace_meta("doomed", description="keep after failure")

        db = storage._get_db()
        rowid = db.execute("SELECT rowid FROM chunks WHERE id=?", (str(doomed.id),)).fetchone()[0]
        db.execute(
            "CREATE TRIGGER _test_delete_meta_boom "
            "BEFORE DELETE ON namespace_metadata "
            "WHEN OLD.namespace = 'doomed' "
            "BEGIN SELECT RAISE(ABORT, 'boom'); END"
        )
        db.commit()
        try:
            async with storage.transaction():
                await storage.upsert_chunks([caller])
                with pytest.raises(StorageError, match="delete_by_namespace failed"):
                    await storage.delete_by_namespace("doomed")
        finally:
            db.execute("DROP TRIGGER IF EXISTS _test_delete_meta_boom")
            db.commit()

        assert dict(await storage.list_namespaces()) == {"caller-ns": 1, "doomed": 1}
        assert await storage.get_namespace_meta("doomed") is not None
        assert db.execute("SELECT COUNT(*) FROM chunks_fts WHERE rowid=?", (rowid,)).fetchone()[0] == 1
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunks_vec'"
        ).fetchone():
            assert (
                db.execute("SELECT COUNT(*) FROM chunks_vec WHERE rowid=?", (rowid,)).fetchone()[0]
                == 1
            )

    async def test_begin_immediate_precedes_first_select(self, storage):
        await storage.upsert_chunks([make_chunk(content="delete me", namespace="doomed")])
        db = storage._get_db()
        trace: list[str] = []
        db.set_trace_callback(lambda sql: trace.append(" ".join(sql.split())))
        try:
            await storage.delete_by_namespace("doomed")
        finally:
            db.set_trace_callback(None)

        begin_idx = next(i for i, sql in enumerate(trace) if "BEGIN IMMEDIATE" in sql.upper())
        select_idx = next(i for i, sql in enumerate(trace) if sql.upper().startswith("SELECT"))
        assert begin_idx < select_idx, trace

    async def test_foreign_transaction_is_refused_and_left_untouched(self, storage):
        await storage.upsert_chunks([make_chunk(content="delete me", namespace="doomed")])
        db = storage._get_db()
        db.execute(
            "INSERT INTO namespace_metadata "
            "(namespace, description, color, created_at, updated_at) "
            "VALUES ('foreign-ns', '', '', 't', 't')"
        )

        with pytest.raises(StorageError, match="open transaction"):
            await storage.delete_by_namespace("doomed")

        assert db.in_transaction
        assert db.execute(
            "SELECT 1 FROM namespace_metadata WHERE namespace='foreign-ns'"
        ).fetchone()
        assert db.execute("SELECT 1 FROM chunks WHERE namespace='doomed'").fetchone()
        db.rollback()


class TestSetNamespaceMetaTransactions:
    async def test_outer_transaction_commits_successful_upsert(self, storage):
        caller = make_chunk(content="commit with metadata", namespace="caller-ns")
        async with storage.transaction():
            await storage.upsert_chunks([caller])
            await storage.set_namespace_meta("meta-ns", description="committed")

        assert dict(await storage.list_namespaces())["caller-ns"] == 1
        meta = await storage.get_namespace_meta("meta-ns")
        assert meta is not None
        assert meta["description"] == "committed"

    async def test_outer_transaction_abort_restores_successful_upsert(self, storage):
        caller = make_chunk(content="abort with metadata", namespace="caller-ns")
        with pytest.raises(RuntimeError, match="caller aborts"):
            async with storage.transaction():
                await storage.upsert_chunks([caller])
                await storage.set_namespace_meta("meta-ns", description="rolled back")
                raise RuntimeError("caller aborts")

        assert "caller-ns" not in dict(await storage.list_namespaces())
        assert await storage.get_namespace_meta("meta-ns") is None

    async def test_failed_fresh_upsert_is_removed_and_preserves_callers_write(self, storage):
        caller = make_chunk(content="keep caller write", namespace="caller-ns")
        db = storage._get_db()
        db.execute(
            "CREATE TRIGGER _test_set_meta_boom "
            "BEFORE UPDATE ON namespace_metadata "
            "WHEN NEW.namespace = 'fresh-meta' "
            "BEGIN SELECT RAISE(ABORT, 'boom'); END"
        )
        db.commit()
        try:
            async with storage.transaction():
                await storage.upsert_chunks([caller])
                with pytest.raises(StorageError, match="set_namespace_meta failed"):
                    await storage.set_namespace_meta("fresh-meta", description="must vanish")
        finally:
            db.execute("DROP TRIGGER IF EXISTS _test_set_meta_boom")
            db.commit()

        assert dict(await storage.list_namespaces())["caller-ns"] == 1
        assert await storage.get_namespace_meta("fresh-meta") is None

    async def test_begin_immediate_precedes_upsert(self, storage):
        db = storage._get_db()
        trace: list[str] = []
        db.set_trace_callback(lambda sql: trace.append(" ".join(sql.split())))
        try:
            await storage.set_namespace_meta("meta-ns", description="locked")
        finally:
            db.set_trace_callback(None)

        begin_idx = next(i for i, sql in enumerate(trace) if "BEGIN IMMEDIATE" in sql.upper())
        insert_idx = next(i for i, sql in enumerate(trace) if sql.upper().startswith("INSERT"))
        assert begin_idx < insert_idx, trace

    async def test_foreign_transaction_is_refused_and_left_untouched(self, storage):
        db = storage._get_db()
        db.execute(
            "INSERT INTO namespace_metadata "
            "(namespace, description, color, created_at, updated_at) "
            "VALUES ('foreign-ns', '', '', 't', 't')"
        )

        with pytest.raises(StorageError, match="open transaction"):
            await storage.set_namespace_meta("meta-ns", description="must not be written")

        assert db.in_transaction
        assert db.execute(
            "SELECT 1 FROM namespace_metadata WHERE namespace='foreign-ns'"
        ).fetchone()
        assert db.execute(
            "SELECT 1 FROM namespace_metadata WHERE namespace='meta-ns'"
        ).fetchone() is None
        db.rollback()
