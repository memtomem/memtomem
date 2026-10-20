"""Maintenance writes judge the hold under the transaction that writes (#2786).

#2781 and #2783 moved the hold screen into the statements that read held
content. Dedup merge and the agent consolidation apply also write to the rows
they checked, and used to check the hold in a separate call first, so a hold
committed between the check and the write was not seen. Both now check and
write under one ``transaction()`` (``BEGIN IMMEDIATE``), and the checks read
the writer while the caller owns that transaction, so they cannot see an older
pool snapshot either.

The race tests commit the hold from a second connection right after the check
returns. Under the old shape that commit lands and the write then hits the
held row; under the write lock the second connection cannot commit at all.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from memtomem.models import CONSOLIDATED_SUFFIX, ORIGIN_CONSOLIDATION_POLICY, Chunk, ChunkMetadata
from memtomem.storage.sqlite_helpers import norm_path
from test_storage_read_routing import pool_reader_mid_statement

pytestmark = pytest.mark.asyncio


def _chunk(text: str, source: Path, **meta) -> Chunk:
    return Chunk(
        content=text,
        metadata=ChunkMetadata(source_file=source, **meta),
        embedding=[0.1] * 1024,
    )


def _hold_from_another_connection(db_path: Path, source: Path) -> str | None:
    """Commit a hold the way another process would; return the error, if any."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = sqlite3.connect(db_path, timeout=0)
    try:
        conn.execute(
            "INSERT INTO held_sources (source_file, first_seen_at, last_checked_at, reason) "
            "VALUES (?, ?, ?, 'missing')",
            (norm_path(source), now, now),
        )
        conn.commit()
        return None
    except sqlite3.OperationalError as exc:
        return str(exc)
    finally:
        conn.close()


def _hold_after_check(monkeypatch, storage, db_path: Path, source: Path) -> list[str | None]:
    """After the first hold check on *source* returns, try to hold it.

    Both check methods are wrapped so the injection runs whichever one the
    write path asks. Returns a list that receives the injection's outcome.
    """
    outcomes: list[str | None] = []
    key = norm_path(source)
    for name in ("is_source_held", "is_source_hidden"):
        real = getattr(storage, name)

        async def checked(path, *args, _real=real, **kwargs):
            result = await _real(path, *args, **kwargs)
            if str(path) == key and not outcomes:
                outcomes.append(_hold_from_another_connection(db_path, source))
            return result

        monkeypatch.setattr(storage, name, checked)
    return outcomes


async def _held(storage, source: Path) -> bool:
    row = storage._get_db().execute(
        "SELECT 1 FROM held_sources WHERE source_file=?", (norm_path(source),)
    )
    return row.fetchone() is not None


class TestDedupMerge:
    async def test_hold_after_the_check_cannot_lose_its_row(
        self, components, memory_dir, monkeypatch
    ):
        from memtomem.search.dedup import DedupScanner

        storage = components.storage
        keep = _chunk("same body", memory_dir / "keep.md")
        dup = _chunk("same body", memory_dir / "dup.md")
        await storage.upsert_chunks([keep, dup])
        outcomes = _hold_after_check(
            monkeypatch, storage, components.config.storage.sqlite_path, memory_dir / "dup.md"
        )

        deleted = await DedupScanner(storage).merge(keep.id, [dup.id])

        assert len(outcomes) == 1, "the hold was never attempted"
        assert outcomes[0] is not None and "locked" in outcomes[0]
        assert not await _held(storage, memory_dir / "dup.md")
        assert deleted == 1
        assert dup.id not in await storage.get_chunks_batch([dup.id])

    async def test_a_source_emptied_by_the_merge_is_cleaned_up(self, components, memory_dir):
        from memtomem.search.dedup import DedupScanner
        from memtomem.storage.sqlite_backend import _AI_SUMMARY_KEY_PREFIX

        storage = components.storage
        dup_path = memory_dir / "dup.md"
        keep = _chunk("same body", memory_dir / "keep.md")
        dup = _chunk("same body", dup_path)
        summary = _chunk(
            "derived",
            Path(norm_path(dup_path) + CONSOLIDATED_SUFFIX),
            origin=ORIGIN_CONSOLIDATION_POLICY,
        )
        await storage.upsert_chunks([keep, dup, summary])
        db = storage._get_db()
        meta_key = f"{_AI_SUMMARY_KEY_PREFIX}{norm_path(dup_path)}"
        db.execute("INSERT INTO _memtomem_meta (key, value) VALUES (?, '{}')", (meta_key,))
        db.commit()

        assert await DedupScanner(storage).merge(keep.id, [dup.id]) == 1

        assert summary.id not in await storage.get_chunks_batch([summary.id])
        assert (
            db.execute("SELECT 1 FROM _memtomem_meta WHERE key=?", (meta_key,)).fetchone() is None
        )

    async def test_policy_summary_of_a_held_parent_cannot_be_merged_away(
        self, components, memory_dir
    ):
        from memtomem.search.dedup import DedupScanner

        storage = components.storage
        parent = memory_dir / "parent.md"
        keep = _chunk("same body", memory_dir / "keep.md")
        summary = _chunk(
            "same body",
            Path(norm_path(parent) + CONSOLIDATED_SUFFIX),
            origin=ORIGIN_CONSOLIDATION_POLICY,
        )
        await storage.upsert_chunks([keep, _chunk("parent body", parent), summary])
        await storage.hold_source(parent, "missing")

        with pytest.raises(ValueError, match="held source"):
            await DedupScanner(storage).merge(keep.id, [summary.id])
        with pytest.raises(ValueError, match="held source"):
            await DedupScanner(storage).merge(keep.id, [summary.id], dry_run=True)
        assert summary.id in await storage.get_chunks_batch([summary.id])


class TestReadsUnderTheCallersTransaction:
    """Each read sees a commit that an open pool statement would have hidden."""

    async def test_hold_checks(self, storage, tmp_path, monkeypatch):
        path = tmp_path / "held.md"
        await storage.upsert_chunks([_chunk("body", path)])
        with pool_reader_mid_statement(storage, monkeypatch):
            await storage.hold_source(path, "missing")
            async with storage.transaction():
                assert await storage.is_source_held(Path(norm_path(path)))
                assert await storage.is_source_hidden(Path(norm_path(path)))

    async def test_batch_read(self, storage, tmp_path, monkeypatch):
        chunk = _chunk("body", tmp_path / "a.md")
        await storage.upsert_chunks([chunk])
        with pool_reader_mid_statement(storage, monkeypatch):
            await storage.upsert_chunks(
                [
                    Chunk(
                        content="body",
                        metadata=ChunkMetadata(source_file=tmp_path / "a.md", tags=("fresh",)),
                        id=chunk.id,
                        embedding=[0.1] * 1024,
                    )
                ]
            )
            async with storage.transaction():
                got = await storage.get_chunks_batch([chunk.id])
        assert got[chunk.id].metadata.tags == ("fresh",)


class TestConsolidateApply:
    async def _setup(self, components, memory_dir, monkeypatch, after_publish):
        from memtomem.server.tools import memory_crud
        from memtomem.server.tools.consolidation import mem_consolidate
        from tests.test_tools_logic import _fake_ctx

        path = memory_dir / "notes.md"
        originals = [_chunk(f"original note {i}", path) for i in range(2)]
        await components.storage.upsert_chunks(originals)
        await components.storage.update_importance_scores({str(c.id): 1.0 for c in originals})
        ctx = _fake_ctx(components)
        assert "original note" in await mem_consolidate(min_group_size=2, ctx=ctx)

        real = memory_crud._mem_add_core

        async def publish_then(*args, **kwargs):
            result, stats = await real(*args, **kwargs)
            await after_publish(path, stats)
            return result, stats

        monkeypatch.setattr(memory_crud, "_mem_add_core", publish_then)
        return ctx, originals

    async def _edges(self, storage, originals) -> int:
        edges = 0
        for c in originals:
            related = await storage.get_related(c.id)
            edges += sum(1 for _, kind in related if kind == "consolidated_into")
        return edges

    async def test_hold_after_publish_gets_no_edges_or_decay(
        self, components, memory_dir, monkeypatch
    ):
        from memtomem.server.tools.consolidation import mem_consolidate_apply

        storage = components.storage

        async def hold(path, stats):
            await storage.hold_source(path, "missing")

        ctx, originals = await self._setup(components, memory_dir, monkeypatch, hold)

        out = await mem_consolidate_apply(
            group_id=0, summary="the summary", keep_originals=False, ctx=ctx
        )

        assert "became unavailable after the summary was written" in out
        assert await self._edges(storage, originals) == 0
        scores = await storage.get_importance_scores([str(c.id) for c in originals])
        assert set(scores.values()) == {1.0}
        [run] = await storage.maintenance_run_latest(kind="consolidate_apply")
        assert run["status"] == "ok"
        assert run["summary"]["warning"] == "source_unavailable"
        assert run["summary"]["summary_id"] is not None

    async def test_a_failed_cleanup_keeps_the_applied_row(
        self, components, memory_dir, monkeypatch
    ):
        """The row says the summary was published even when dropping the preview fails."""
        from memtomem.server.tools import consolidation
        from memtomem.server.tools.consolidation import mem_consolidate_apply

        storage = components.storage

        async def hold(path, stats):
            await storage.hold_source(path, "missing")

        ctx, originals = await self._setup(components, memory_dir, monkeypatch, hold)

        async def failing_drop(app, entry):
            raise sqlite3.OperationalError("injected: scratch delete failed")

        monkeypatch.setattr(consolidation, "_drop_preview", failing_drop)

        out = await mem_consolidate_apply(
            group_id=0, summary="the summary", keep_originals=False, ctx=ctx
        )

        assert "injected: scratch delete failed" in out
        [run] = await storage.maintenance_run_latest(kind="consolidate_apply")
        assert run["status"] == "ok"
        assert run["error"] is None
        assert run["summary"]["warning"] == "source_unavailable"
        assert run["summary"]["summary_id"] is not None

    async def test_hold_after_the_run_opens_finishes_the_row(
        self, components, memory_dir, monkeypatch
    ):
        """The re-check after the run row opens refuses, and the row does not stay running."""
        from memtomem.server.tools.consolidation import mem_consolidate_apply

        storage = components.storage
        published: list[object] = []

        async def record(path, stats):
            published.append(stats)

        ctx, originals = await self._setup(components, memory_dir, monkeypatch, record)
        real_start = storage.maintenance_run_start

        async def start_then_hold(*args, **kwargs):
            run_id = await real_start(*args, **kwargs)
            await storage.hold_source(memory_dir / "notes.md", "missing")
            return run_id

        monkeypatch.setattr(storage, "maintenance_run_start", start_then_hold)

        out = await mem_consolidate_apply(
            group_id=0, summary="the summary", keep_originals=False, ctx=ctx
        )

        assert out == "Error: source is unavailable. Run mem_consolidate after it returns."
        assert published == []
        assert await self._edges(storage, originals) == 0
        [run] = await storage.maintenance_run_latest(kind="consolidate_apply")
        assert run["status"] == "error"
        assert run["error"] == out
        assert run["completed_at"] is not None

    async def test_hold_after_the_final_check_cannot_commit_before_the_writes(
        self, components, memory_dir, monkeypatch
    ):
        """The re-check and the writes share the lock, so a hold cannot land between them."""
        from memtomem.server.tools.consolidation import mem_consolidate_apply

        storage = components.storage
        armed: list[list[str | None]] = []

        async def arm(path, stats):
            # Armed only now: the checks before publication must not trigger it.
            armed.append(
                _hold_after_check(monkeypatch, storage, components.config.storage.sqlite_path, path)
            )

        ctx, originals = await self._setup(components, memory_dir, monkeypatch, arm)

        out = await mem_consolidate_apply(
            group_id=0, summary="the summary", keep_originals=False, ctx=ctx
        )

        [[outcome]] = armed
        assert outcome is not None and "locked" in outcome
        assert not await _held(storage, memory_dir / "notes.md")
        assert "Originals linked: 2/2" in out
        assert await self._edges(storage, originals) == 2
        scores = await storage.get_importance_scores([str(c.id) for c in originals])
        assert set(scores.values()) == {0.5}

    @pytest.mark.parametrize("fail_in", ["add_relation", "update_importance_scores"])
    async def test_a_write_that_ends_the_transaction_leaves_nothing(
        self, components, memory_dir, monkeypatch, fail_in
    ):
        from memtomem.server.tools.consolidation import mem_consolidate_apply

        storage = components.storage

        async def nothing(path, stats):
            return None

        ctx, originals = await self._setup(components, memory_dir, monkeypatch, nothing)
        real = getattr(storage, fail_in)
        injected: list[bool] = []

        async def rollback_then_raise(*args, **kwargs):
            if not injected:
                injected.append(True)
                storage._get_db().rollback()
                raise sqlite3.OperationalError("injected: transaction ended")
            return await real(*args, **kwargs)

        monkeypatch.setattr(storage, fail_in, rollback_then_raise)

        await mem_consolidate_apply(
            group_id=0, summary="the summary", keep_originals=False, ctx=ctx
        )

        assert injected == [True]
        assert await self._edges(storage, originals) == 0
        scores = await storage.get_importance_scores([str(c.id) for c in originals])
        assert set(scores.values()) == {1.0}
        [run] = await storage.maintenance_run_latest(kind="consolidate_apply")
        assert run["status"] == "error"

    async def test_summary_gone_before_linking_is_an_error_run(
        self, components, memory_dir, monkeypatch
    ):
        """The edge's target is the summary; strict linking fails rather than skipping it."""
        from memtomem.server.tools.consolidation import mem_consolidate_apply

        storage = components.storage

        async def drop_summary(path, stats):
            await storage.delete_chunks(stats.new_chunk_ids)

        ctx, originals = await self._setup(components, memory_dir, monkeypatch, drop_summary)

        await mem_consolidate_apply(
            group_id=0, summary="the summary", keep_originals=False, ctx=ctx
        )

        scores = await storage.get_importance_scores([str(c.id) for c in originals])
        assert set(scores.values()) == {1.0}
        [run] = await storage.maintenance_run_latest(kind="consolidate_apply")
        assert run["status"] == "error"
