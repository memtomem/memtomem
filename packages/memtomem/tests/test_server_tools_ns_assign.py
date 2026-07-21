"""Collision, transaction, and MCP contracts for namespace assignment (#1886)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from helpers import StubCtx, make_chunk
from memtomem.errors import NamespaceConflictError, StorageError
from memtomem.server.context import AppContext
from memtomem.server.tool_registry import ACTIONS
from memtomem.server.tools.namespace import mem_ns_assign


def _duplicate_pair():
    source = make_chunk(content="same", namespace="source-ns", source="shared.md")
    target = make_chunk(content="same", namespace="target-ns", source="shared.md")
    target.content_hash = source.content_hash
    return source, target


class TestAssignNamespaceStorage:
    async def test_no_overlap_preserves_assignment(self, storage):
        first = make_chunk(content="one", namespace="source-ns", source="one.md")
        second = make_chunk(content="two", namespace="source-ns", source="two.md")
        existing = make_chunk(content="existing", namespace="target-ns", source="existing.md")
        await storage.upsert_chunks([first, second, existing])

        result = await storage.assign_namespace("target-ns", old_namespace="source-ns")

        assert (result.chunks_moved, result.duplicates_dropped) == (2, 0)
        assert dict(await storage.list_namespaces()) == {"target-ns": 3}

    async def test_overlap_refuses_before_writing(self, storage):
        source, target = _duplicate_pair()
        await storage.upsert_chunks([source, target])

        with pytest.raises(NamespaceConflictError) as excinfo:
            await storage.assign_namespace("target-ns", old_namespace="source-ns")

        assert excinfo.value.reason_code == "chunk_overlap"
        assert "1 chunk(s) overlap" in str(excinfo.value)
        assert await storage.get_chunk(source.id) is not None
        assert await storage.get_chunk(target.id) is not None
        assert storage._get_db().in_transaction is False

    async def test_merge_keeps_target_and_reports_moved_and_dropped(self, storage):
        source, target = _duplicate_pair()
        unique = make_chunk(content="unique", namespace="source-ns", source="unique.md")
        await storage.upsert_chunks([source, target, unique])

        result = await storage.assign_namespace("target-ns", old_namespace="source-ns", merge=True)

        assert (result.chunks_moved, result.duplicates_dropped) == (1, 1)
        assert await storage.get_chunk(source.id) is None
        assert await storage.get_chunk(target.id) is not None
        assert (await storage.get_chunk(unique.id)).metadata.namespace == "target-ns"

    async def test_source_filter_excludes_matching_target_rows(self, storage):
        source, target = _duplicate_pair()
        selected = make_chunk(content="selected", namespace="other-ns", source="shared-two.md")
        unselected = make_chunk(content="stay", namespace="other-ns", source="outside.txt")
        await storage.upsert_chunks([source, target, selected, unselected])

        result = await storage.assign_namespace("target-ns", source_filter="shared", merge=True)

        assert (result.chunks_moved, result.duplicates_dropped) == (1, 1)
        assert await storage.get_chunk(target.id) is not None
        assert (await storage.get_chunk(selected.id)).metadata.namespace == "target-ns"
        assert (await storage.get_chunk(unselected.id)).metadata.namespace == "other-ns"

    async def test_two_filters_keep_and_semantics_and_scope_the_preflight(self, storage):
        target = make_chunk(content="same", namespace="target-ns", source="shared.md")
        source = make_chunk(content="same", namespace="outside-ns", source="shared.md")
        source.content_hash = target.content_hash
        selected = make_chunk(content="selected", namespace="source-ns", source="shared-new.md")
        wrong_path = make_chunk(content="wrong path", namespace="source-ns", source="other.md")
        await storage.upsert_chunks([source, target, selected, wrong_path])

        result = await storage.assign_namespace(
            "target-ns",
            source_filter="shared",
            old_namespace="source-ns",
        )

        assert (result.chunks_moved, result.duplicates_dropped) == (1, 0)
        assert (await storage.get_chunk(selected.id)).metadata.namespace == "target-ns"
        assert (await storage.get_chunk(source.id)).metadata.namespace == "outside-ns"
        assert (await storage.get_chunk(wrong_path.id)).metadata.namespace == "source-ns"

    async def test_source_to_source_collision_uses_691_survivor_order(self, storage):
        preferred = make_chunk(content="same", namespace="source-a", source="shared.md")
        loser = make_chunk(content="same", namespace="source-b", source="shared.md")
        loser.content_hash = preferred.content_hash
        await storage.upsert_chunks([preferred, loser])
        db = storage._get_db()
        db.execute("UPDATE chunks SET access_count=5 WHERE id=?", (str(preferred.id),))
        db.commit()

        with pytest.raises(NamespaceConflictError):
            await storage.assign_namespace("target-ns", source_filter="shared.md")
        result = await storage.assign_namespace("target-ns", source_filter="shared.md", merge=True)

        assert (result.chunks_moved, result.duplicates_dropped) == (1, 1)
        assert await storage.get_chunk(preferred.id) is not None
        assert await storage.get_chunk(loser.id) is None
        assert (await storage.get_chunk(preferred.id)).metadata.namespace == "target-ns"

    async def test_same_target_is_a_zero_row_noop(self, storage):
        chunk = make_chunk(content="same", namespace="target-ns")
        await storage.upsert_chunks([chunk])

        result = await storage.assign_namespace("target-ns", old_namespace="target-ns")

        assert (result.chunks_moved, result.duplicates_dropped) == (0, 0)
        assert await storage.get_chunk(chunk.id) is not None

    async def test_merge_remaps_references_and_deletes_loser_sidecars(self, storage):
        source, target = _duplicate_pair()
        other = make_chunk(content="other", namespace="target-ns", source="other.md")
        await storage.upsert_chunks([source, target, other])
        await storage.add_relation(source.id, other.id, "related")
        db = storage._get_db()
        source_rowid = db.execute(
            "SELECT rowid FROM chunks WHERE id=?", (str(source.id),)
        ).fetchone()[0]
        target_rowid = db.execute(
            "SELECT rowid FROM chunks WHERE id=?", (str(target.id),)
        ).fetchone()[0]

        await storage.assign_namespace("target-ns", old_namespace="source-ns", merge=True)

        assert await storage.get_related(target.id) == [(other.id, "related")]
        for table in ("chunks_fts", "chunks_vec"):
            assert (
                db.execute(f"SELECT 1 FROM {table} WHERE rowid=?", (source_rowid,)).fetchone()
                is None
            )
            assert db.execute(f"SELECT 1 FROM {table} WHERE rowid=?", (target_rowid,)).fetchone()

    async def test_multi_loser_edges_do_not_become_self_edges(self, storage):
        survivor = make_chunk(content="same", namespace="source-a", source="shared.md")
        loser_b = make_chunk(content="same", namespace="source-b", source="shared.md")
        loser_c = make_chunk(content="same", namespace="source-c", source="shared.md")
        loser_b.content_hash = survivor.content_hash
        loser_c.content_hash = survivor.content_hash
        await storage.upsert_chunks([survivor, loser_b, loser_c])
        db = storage._get_db()
        db.execute("UPDATE chunks SET access_count=5 WHERE id=?", (str(survivor.id),))
        db.commit()
        await storage.add_relation(loser_b.id, loser_c.id, "related")

        result = await storage.assign_namespace("target-ns", source_filter="shared.md", merge=True)

        assert (result.chunks_moved, result.duplicates_dropped) == (1, 2)
        assert await storage.get_related(survivor.id) == []


class TestAssignNamespaceAtomicity:
    async def test_failure_after_duplicate_cleanup_rolls_everything_back(self, storage):
        source, target = _duplicate_pair()
        unique = make_chunk(content="unique", namespace="source-ns", source="unique.md")
        await storage.upsert_chunks([source, target, unique])
        await storage.add_relation(source.id, unique.id, "related")
        db = storage._get_db()
        source_rowid = db.execute(
            "SELECT rowid FROM chunks WHERE id=?", (str(source.id),)
        ).fetchone()[0]
        db.execute(
            "CREATE TRIGGER _test_assign_boom BEFORE UPDATE OF namespace ON chunks "
            f"WHEN OLD.id = '{unique.id}' "
            "BEGIN SELECT RAISE(ABORT, 'boom'); END"
        )
        db.commit()

        with pytest.raises(StorageError, match="transaction rolled back"):
            await storage.assign_namespace("target-ns", old_namespace="source-ns", merge=True)

        assert await storage.get_chunk(source.id) is not None
        assert await storage.get_chunk(target.id) is not None
        assert (await storage.get_chunk(unique.id)).metadata.namespace == "source-ns"
        assert await storage.get_related(source.id) == [(unique.id, "related")]
        assert db.execute("SELECT 1 FROM chunks_fts WHERE rowid=?", (source_rowid,)).fetchone()

    async def test_successful_assign_follows_outer_transaction_rollback(self, storage):
        chunk = make_chunk(content="one", namespace="source-ns")
        caller = make_chunk(content="caller write", namespace="caller-ns")
        await storage.upsert_chunks([chunk])

        with pytest.raises(RuntimeError):
            async with storage.transaction():
                await storage.upsert_chunks([caller])
                await storage.assign_namespace("target-ns", old_namespace="source-ns")
                raise RuntimeError("abort caller")

        assert (await storage.get_chunk(chunk.id)).metadata.namespace == "source-ns"
        assert await storage.get_chunk(caller.id) is None

    async def test_successful_assign_follows_outer_transaction_commit(self, storage):
        chunk = make_chunk(content="one", namespace="source-ns")
        caller = make_chunk(content="caller write", namespace="caller-ns")
        await storage.upsert_chunks([chunk])

        async with storage.transaction():
            await storage.upsert_chunks([caller])
            await storage.assign_namespace("target-ns", old_namespace="source-ns")

        assert (await storage.get_chunk(chunk.id)).metadata.namespace == "target-ns"
        assert await storage.get_chunk(caller.id) is not None

    async def test_failed_assign_preserves_callers_earlier_write(self, storage):
        source, target = _duplicate_pair()
        unique = make_chunk(content="unique", namespace="source-ns", source="unique.md")
        caller = make_chunk(content="caller write", namespace="caller-ns")
        await storage.upsert_chunks([source, target, unique])
        db = storage._get_db()
        db.execute(
            "CREATE TRIGGER _test_assign_outer_boom BEFORE UPDATE OF namespace ON chunks "
            f"WHEN OLD.id = '{unique.id}' "
            "BEGIN SELECT RAISE(ABORT, 'boom'); END"
        )
        db.commit()
        try:
            async with storage.transaction():
                await storage.upsert_chunks([caller])
                with pytest.raises(StorageError, match="transaction rolled back"):
                    await storage.assign_namespace(
                        "target-ns", old_namespace="source-ns", merge=True
                    )
        finally:
            db.execute("DROP TRIGGER IF EXISTS _test_assign_outer_boom")
            db.commit()

        assert await storage.get_chunk(caller.id) is not None
        assert await storage.get_chunk(source.id) is not None
        assert await storage.get_chunk(target.id) is not None
        assert (await storage.get_chunk(unique.id)).metadata.namespace == "source-ns"

    async def test_lock_precedes_collision_preflight(self, storage):
        chunk = make_chunk(content="one", namespace="source-ns")
        await storage.upsert_chunks([chunk])
        db = storage._get_db()
        trace: list[str] = []
        db.set_trace_callback(lambda sql: trace.append(" ".join(sql.split())))
        try:
            await storage.assign_namespace("target-ns", old_namespace="source-ns")
        finally:
            db.set_trace_callback(None)

        begin = next(i for i, sql in enumerate(trace) if "BEGIN IMMEDIATE" in sql.upper())
        preflight = next(i for i, sql in enumerate(trace) if "WITH SELECTED AS" in sql.upper())
        assert begin < preflight

    async def test_foreign_transaction_is_refused_and_left_untouched(self, storage):
        chunk = make_chunk(content="one", namespace="source-ns")
        await storage.upsert_chunks([chunk])
        db = storage._get_db()
        db.execute(
            "INSERT INTO namespace_metadata "
            "(namespace, description, color, created_at, updated_at) "
            "VALUES ('foreign-ns', '', '', 't', 't')"
        )

        with pytest.raises(StorageError, match="open transaction"):
            await storage.assign_namespace("target-ns", old_namespace="source-ns")

        assert db.in_transaction
        assert db.execute(
            "SELECT 1 FROM namespace_metadata WHERE namespace='foreign-ns'"
        ).fetchone()
        assert (await storage.get_chunk(chunk.id)).metadata.namespace == "source-ns"
        db.rollback()


@pytest.fixture
def ctx(components):
    return StubCtx(AppContext.from_components(components))


class TestAssignNamespaceTool:
    async def test_conflict_names_count_and_merge_remedy(self, ctx, storage):
        source, target = _duplicate_pair()
        await storage.upsert_chunks([source, target])

        output = await mem_ns_assign(namespace="target-ns", old_namespace="source-ns", ctx=ctx)

        assert output.startswith("Error:")
        assert "1 chunk(s) overlap" in output
        assert "merge=True" in output

    async def test_merge_reports_moved_and_dropped_counts(self, ctx, storage):
        source, target = _duplicate_pair()
        unique = make_chunk(content="unique", namespace="source-ns", source="unique.md")
        await storage.upsert_chunks([source, target, unique])

        output = await mem_ns_assign(
            namespace="target-ns",
            old_namespace="source-ns",
            merge=True,
            ctx=ctx,
        )

        assert "Assigned 1 chunks" in output
        assert "1 duplicate chunk(s) dropped" in output

    @pytest.mark.parametrize("value", ["true", "false", 1, 0])
    async def test_non_literal_merge_is_refused(self, ctx, value):
        output = await mem_ns_assign(
            namespace="target-ns",
            old_namespace="source-ns",
            merge=value,
            ctx=ctx,
        )
        assert output.startswith("Error:") and "literal boolean" in output

    def test_help_catalog_exposes_merge(self):
        assert "merge" in ACTIONS["ns_assign"].params

    async def test_forwards_merge_as_keyword(self, ctx):
        ctx.request_context.lifespan_context.storage.assign_namespace = AsyncMock(
            return_value=type("Result", (), {"chunks_moved": 0, "duplicates_dropped": 0})()
        )
        await mem_ns_assign(namespace="target-ns", old_namespace="source-ns", merge=True, ctx=ctx)
        ctx.request_context.lifespan_context.storage.assign_namespace.assert_awaited_once_with(
            "target-ns",
            source_filter=None,
            old_namespace="source-ns",
            merge=True,
        )
