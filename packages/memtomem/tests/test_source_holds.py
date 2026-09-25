"""Unavailable source preservation and retrieval visibility (#2498)."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from memtomem.models import Chunk, ChunkMetadata, IndexingStats

from memtomem.indexing.watcher import FileWatcher, _STOP_SENTINEL
from memtomem.config import IndexingConfig
from memtomem.scheduler.jobs import _run_compaction


async def _indexed(components, path: Path, word: str = "mountword") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# Note\n\n{word} content.\n", encoding="utf-8")
    embedder = AsyncMock()
    embedder.embed_texts = AsyncMock(side_effect=lambda texts, **_: [[0.1] * 1024 for _ in texts])
    embedder.dimension = 1024
    components.index_engine._embedder = embedder
    result = await components.index_engine.index_file(path)
    assert result.indexed_chunks > 0


async def test_pending_delete_hidden_and_replayed_after_restart(components, memory_dir):
    path = memory_dir / "pending.md"
    await _indexed(components, path)
    old_ids = await components.storage.get_chunk_hashes(path)
    assert old_ids
    assert (await components.search_pipeline.search("mountword"))[0]

    path.unlink()
    assert components.storage.queue_source_check_sync(path)
    assert path in await components.storage.get_pending_source_checks()
    # A warmed result cache must see the committed visibility epoch.
    assert (await components.search_pipeline.search("mountword"))[0] == []
    assert await components.storage.bm25_search("mountword") == []
    assert await components.storage.dense_search([0.1] * 1024) == []
    assert await components.storage.recall_chunks(source_filter="pending.md") == []
    assert await components.storage.get_chunk(UUID(next(iter(old_ids)))) is not None

    restarted = FileWatcher(components.index_engine, components.config.indexing)
    await restarted._replay_pending_checks()
    restarted._queue.put_nowait(_STOP_SENTINEL)
    await restarted._process_events()
    assert path not in await components.storage.get_pending_source_checks()
    assert path in await components.storage.get_held_sources()
    assert await components.storage.get_chunk_hashes(path) == old_ids


async def test_delete_journal_keeps_nfd_path_on_nonfolding_platform(
    components, memory_dir, monkeypatch
):
    from memtomem.storage import sqlite_helpers
    from memtomem.storage.sqlite_helpers import norm_path

    indexed = memory_dir / "indexed.md"
    await _indexed(components, indexed)
    nfd_path = memory_dir / "cafe\u0301.md"
    db = components.storage._get_db()
    db.execute(
        "UPDATE chunks SET source_file=? WHERE source_file=?",
        (str(nfd_path), norm_path(indexed)),
    )
    db.commit()
    monkeypatch.setattr(sqlite_helpers, "FOLDS_UNICODE_FORMS", False)

    assert components.storage.queue_source_check_sync(nfd_path)
    assert nfd_path in await components.storage.get_pending_source_checks()


async def test_periodic_recheck_recovers_pending_without_a_new_event(components, memory_dir):
    path = memory_dir / "pending-returned.md"
    await _indexed(components, path)
    assert components.storage.queue_source_check_sync(path)
    assert path in await components.storage.get_pending_source_checks()

    watcher = FileWatcher(components.index_engine, components.config.indexing)
    task = asyncio.create_task(watcher._recheck_held_loop())
    try:
        for _ in range(100):
            if path not in await components.storage.get_pending_source_checks():
                break
            await asyncio.sleep(0.01)
        assert path not in await components.storage.get_pending_source_checks()
        assert not await components.storage.is_source_held(path)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_periodic_recheck_holds_missing_pending_without_a_new_event(components, memory_dir):
    path = memory_dir / "pending-missing.md"
    await _indexed(components, path)
    path.unlink()
    assert components.storage.queue_source_check_sync(path)
    assert path in await components.storage.get_pending_source_checks()

    watcher = FileWatcher(components.index_engine, components.config.indexing)
    task = asyncio.create_task(watcher._recheck_held_loop())
    try:
        for _ in range(100):
            if path in await components.storage.get_held_sources():
                break
            await asyncio.sleep(0.01)
        assert path in await components.storage.get_held_sources()
        assert path not in await components.storage.get_pending_source_checks()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_held_source_recovers_after_successful_reindex(components, memory_dir):
    path = memory_dir / "return.md"
    await _indexed(components, path)
    path.unlink()
    await components.index_engine.index_file(path)
    assert path in await components.storage.get_held_sources()

    path.write_text("# Note\n\nmountword content.\n", encoding="utf-8")
    result = await components.index_engine.index_file(path)
    assert not result.errors
    assert path not in await components.storage.get_held_sources()
    assert await components.storage.bm25_search("mountword")


@pytest.mark.parametrize("delete_method", ["chunks", "namespace"])
async def test_last_chunk_delete_clears_hold_before_direct_upsert(
    components, memory_dir, delete_method
):
    path = memory_dir / f"delete-{delete_method}.md"
    await _indexed(components, path)
    chunk_id = UUID(next(iter(await components.storage.get_chunk_hashes(path))))
    chunk = await components.storage.get_chunk(chunk_id)
    assert chunk is not None
    await components.storage.hold_source(path, "source_missing")

    if delete_method == "chunks":
        assert await components.storage.delete_chunks([chunk_id]) == 1
    else:
        assert await components.storage.delete_by_namespace(chunk.metadata.namespace) >= 1

    assert not await components.storage.is_source_held(path)
    await components.storage.upsert_chunks([chunk])
    assert await components.storage.bm25_search("mountword")


async def test_release_lock_contention_keeps_successful_index_result(
    components, memory_dir, monkeypatch
):
    path = memory_dir / "release-locked.md"
    await _indexed(components, path)
    await components.storage.hold_source(path, "missing")
    monkeypatch.setattr(
        components.storage,
        "release_source_hold",
        AsyncMock(side_effect=sqlite3.OperationalError("database is locked")),
    )

    result = await components.index_engine.index_file(path)
    assert not result.errors
    assert await components.storage.is_source_held(path)


async def test_periodic_recheck_restores_unchanged_source(components, memory_dir):
    path = memory_dir / "periodic.md"
    await _indexed(components, path)
    path.unlink()
    await components.index_engine.index_file(path)
    assert path in await components.storage.get_held_sources()
    path.write_text("# Note\n\nmountword content.\n", encoding="utf-8")

    watcher = FileWatcher(components.index_engine, components.config.indexing)
    task = asyncio.create_task(watcher._recheck_held_loop())
    try:
        for _ in range(100):
            if path not in await components.storage.get_held_sources():
                break
            await asyncio.sleep(0.01)
        assert path not in await components.storage.get_held_sources()
        assert await components.storage.bm25_search("mountword")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_recheck_waits_when_other_process_clears_startup_holds(
    components, memory_dir, monkeypatch
):
    import memtomem.indexing.watcher as watcher_module

    path = memory_dir / "gone.md"
    monkeypatch.setattr(watcher_module, "_HELD_RECHECK_BATCH", 1)
    getter = AsyncMock(side_effect=[[path, memory_dir / "other.md"], []])
    monkeypatch.setattr(components.storage, "get_held_sources", getter)
    watcher = FileWatcher(components.index_engine, components.config.indexing)
    task = asyncio.create_task(watcher._recheck_held_loop())
    try:
        await asyncio.sleep(0.05)
        assert getter.await_count == 2
        assert not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_explicit_source_delete_clears_hold_and_pending(components, memory_dir):
    path = memory_dir / "purge.md"
    await _indexed(components, path)
    path.unlink()
    components.storage.queue_source_check_sync(path)
    await components.storage.hold_source(path, "source_missing")
    assert path in await components.storage.get_held_sources()

    assert await components.storage.delete_by_source(path) > 0
    assert path not in await components.storage.get_held_sources()
    assert path not in await components.storage.get_pending_source_checks()


async def test_unheld_receipt_release_does_not_take_sqlite_writer_lock(components, memory_dir):
    path = memory_dir / "unchanged.md"
    await _indexed(components, path)
    writer = components.storage._get_db()
    writer.execute("PRAGMA busy_timeout=100")
    other = sqlite3.connect(str(components.config.storage.sqlite_path), timeout=0.1)
    try:
        other.execute("BEGIN IMMEDIATE")
        assert await components.storage.release_source_hold(path) is False
    finally:
        other.rollback()
        other.close()


async def test_repeated_hold_with_same_reason_does_not_take_writer_lock(components, memory_dir):
    path = memory_dir / "already-held.md"
    await _indexed(components, path)
    await components.storage.hold_source(path, "missing")
    writer = components.storage._get_db()
    writer.execute("PRAGMA busy_timeout=100")
    other = sqlite3.connect(str(components.config.storage.sqlite_path), timeout=0.1)
    try:
        other.execute("BEGIN IMMEDIATE")
        assert await components.storage.hold_source(path, "missing") is False
    finally:
        other.rollback()
        other.close()


async def test_status_orphans_exclude_held_sources(components, memory_dir):
    from memtomem.server.context import AppContext
    from memtomem.server.tools.status_config import collect_status_report

    held = memory_dir / "held-status.md"
    unheld = memory_dir / "unheld-status.md"
    await _indexed(components, held)
    await _indexed(components, unheld)
    held.unlink()
    unheld.unlink()
    await components.storage.hold_source(held, "missing")

    report = await collect_status_report(AppContext.from_components(components))
    assert report["index"]["held_sources"] == 1
    assert report["index"]["orphaned_sources"] == 1


async def test_new_delete_journal_cannot_be_released_by_older_index_pass(components, memory_dir):
    path = memory_dir / "raced.md"
    await _indexed(components, path)
    await components.storage.hold_source(path, "missing")
    generation_before_read = await components.storage.source_hold_generation(path)
    assert components.storage.queue_source_check_sync(path)
    assert not await components.storage.release_source_hold(
        path, expected_generation=generation_before_read
    )
    assert await components.storage.is_source_held(path)
    # A second delete while the first check is still pending must also fence
    # an index pass that started after the first one.
    generation_after_first_delete = await components.storage.source_hold_generation(path)
    assert components.storage.queue_source_check_sync(path)
    assert not await components.storage.release_source_hold(
        path, expected_generation=generation_after_first_delete
    )


async def test_release_refusal_preserves_unowned_pending_transaction(components, memory_dir):
    from memtomem.errors import TransactionOwnedError

    path = memory_dir / "pending-transaction.md"
    await _indexed(components, path)
    await components.storage.hold_source(path, "missing")
    generation = await components.storage.source_hold_generation(path)
    db = components.storage._get_db()
    db.execute("INSERT INTO _memtomem_meta(key, value) VALUES ('hold_probe', 'kept')")
    try:
        with pytest.raises(TransactionOwnedError):
            await components.storage.release_source_hold(path, expected_generation=generation)
        assert db.in_transaction
        assert db.execute("SELECT value FROM _memtomem_meta WHERE key='hold_probe'").fetchone() == (
            "kept",
        )
    finally:
        db.rollback()


async def test_other_source_visibility_change_does_not_block_release(components, memory_dir):
    first, second = memory_dir / "first.md", memory_dir / "second.md"
    await _indexed(components, first)
    await _indexed(components, second)
    await components.storage.hold_source(first, "missing")
    generation = await components.storage.source_hold_generation(first)
    await components.storage.hold_source(second, "missing")
    assert await components.storage.release_source_hold(first, expected_generation=generation)
    assert not await components.storage.is_source_held(first)
    assert await components.storage.is_source_held(second)


async def test_bulk_index_releases_all_held_sources(components, memory_dir):
    paths = [memory_dir / f"bulk-{i}.md" for i in range(16)]
    for i, path in enumerate(paths):
        path.write_text(f"# Note\n\nbulkword{i} content.\n", encoding="utf-8")
    embedder = AsyncMock()
    embedder.embed_texts = AsyncMock(side_effect=lambda texts, **_: [[0.1] * 1024 for _ in texts])
    embedder.dimension = 1024
    components.index_engine._embedder = embedder
    await components.index_engine.index_path(memory_dir)
    for path in paths:
        await components.storage.hold_source(path, "missing")
    assert len(await components.storage.get_held_sources()) == len(paths)

    result = await components.index_engine.index_path(memory_dir)
    assert not result.errors
    assert await components.storage.get_held_sources() == []


async def test_unresolved_watcher_check_becomes_periodically_rechecked_hold(
    components, memory_dir, monkeypatch
):
    path = memory_dir / "unreadable.md"
    await _indexed(components, path)
    assert components.storage.queue_source_check_sync(path)
    watcher = FileWatcher(components.index_engine, components.config.indexing)
    monkeypatch.setattr(
        components.index_engine,
        "index_file",
        AsyncMock(return_value=IndexingStats(0, 0, 0, 0, 0, 0.0)),
    )
    await watcher._reindex(path)
    assert not await components.storage.is_source_pending(path)
    assert path in await components.storage.get_held_sources()


async def test_held_source_hidden_from_entities_and_consolidation(components, memory_dir):
    path = memory_dir / "private.md"
    chunks = [
        Chunk(
            content=f"secret person {i}",
            metadata=ChunkMetadata(source_file=path),
            embedding=[0.1] * 1024,
        )
        for i in range(2)
    ]
    await components.storage.upsert_chunks(chunks)
    await components.storage.upsert_entities(
        str(chunks[0].id), [{"entity_type": "person", "entity_value": "Secret"}]
    )
    assert await components.storage.search_entities(value="Secret")
    assert await components.storage.get_consolidation_groups(min_size=2)

    await components.storage.hold_source(path, "missing")
    assert await components.storage.search_entities(value="Secret") == []
    assert await components.storage.get_consolidation_groups(min_size=2) == []


async def test_held_dense_neighbours_do_not_crowd_out_visible_match(
    components, memory_dir, monkeypatch
):
    held_path = memory_dir / "held-neighbours.md"
    visible_path = memory_dir / "visible-neighbour.md"
    held = [
        Chunk(
            content=f"held neighbour {i}",
            metadata=ChunkMetadata(source_file=held_path),
            embedding=[0.1] * 1024,
        )
        for i in range(120)
    ]
    visible = Chunk(
        content="visible neighbour",
        metadata=ChunkMetadata(source_file=visible_path),
        embedding=[0.2] * 1024,
    )
    await components.storage.upsert_chunks([*held, visible])
    await components.storage.hold_source(held_path, "missing")

    decoded: list[Chunk] = []
    original = components.storage._row_to_chunk

    def count_decoded(row):
        chunk = original(row)
        decoded.append(chunk)
        return chunk

    monkeypatch.setattr(components.storage, "_row_to_chunk", count_decoded)

    results = await components.storage.dense_search([0.1] * 1024, top_k=1)
    assert [result.chunk.id for result in results] == [visible.id]
    assert [chunk.id for chunk in decoded] == [visible.id]


async def test_held_source_hidden_from_consolidation_preview_and_apply(components, memory_dir):
    from memtomem.errors import StorageError
    from memtomem.server.tools.consolidation import mem_consolidate, mem_consolidate_apply
    from memtomem.tools.consolidation_engine import apply_consolidation
    from tests.test_tools_logic import _fake_ctx

    path = memory_dir / "summary.md"
    chunks = [
        Chunk(
            content=f"secret content {i}",
            metadata=ChunkMetadata(source_file=path),
            embedding=[0.1] * 1024,
        )
        for i in range(2)
    ]
    await components.storage.upsert_chunks(chunks)
    ctx = _fake_ctx(components)
    assert "secret content" in await mem_consolidate(min_group_size=2, ctx=ctx)

    await components.storage.hold_source(path, "missing")
    assert "unavailable" in await mem_consolidate_apply(group_id=0, summary="summary", ctx=ctx)
    assert await components.storage.maintenance_run_latest(kind="consolidate_apply") == []
    assert "secret content" not in await mem_consolidate(min_group_size=2, ctx=ctx)
    with pytest.raises(StorageError, match="became unavailable"):
        await apply_consolidation(
            components.storage,
            {"source": str(path), "chunk_ids": [str(c.id) for c in chunks]},
            "summary",
        )


async def test_mem_export_warns_when_held_sources_are_omitted(components, memory_dir):
    from memtomem.server.tools.export_import import mem_export
    from tests.test_tools_logic import _fake_ctx

    path = memory_dir / "held-export.md"
    chunk = Chunk(
        content="backup hidden content",
        metadata=ChunkMetadata(source_file=path),
        embedding=[0.1] * 1024,
    )
    await components.storage.upsert_chunks([chunk])
    await components.storage.hold_source(path, "missing")
    output = memory_dir / "backup.json"

    message = await mem_export(str(output), ctx=_fake_ctx(components))
    assert "1 held or pending source(s) omitted" in message
    assert '"omitted_held_sources": 1' in output.read_text(encoding="utf-8")


async def test_pending_path_outside_watch_roots_settles_and_recovers(components, memory_dir):
    path = memory_dir / "removed-root.md"
    await _indexed(components, path)
    assert components.storage.queue_source_check_sync(path)
    watcher = FileWatcher(
        components.index_engine,
        IndexingConfig(memory_dirs=[str(memory_dir / "different-root")]),
    )
    await watcher._reindex(path)
    assert path not in await components.storage.get_pending_source_checks()
    assert path in await components.storage.get_held_sources()

    task = asyncio.create_task(watcher._recheck_held_loop())
    try:
        for _ in range(100):
            if path not in await components.storage.get_held_sources():
                break
            await asyncio.sleep(0.01)
        assert path not in await components.storage.get_held_sources()
        assert await components.storage.bm25_search("mountword")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("count", range(1, 10))
async def test_small_missing_batch_under_unregistered_nested_mount_is_held(
    components, memory_dir, monkeypatch, count
):
    from memtomem.storage import orphan_detect

    monkeypatch.setattr(orphan_detect, "ORPHAN_RECHECK_DELAY_SECONDS", 0)
    nested = memory_dir / "unregistered_mount"
    paths = [nested / f"note-{i}.md" for i in range(count)]
    for i, path in enumerate(paths):
        await _indexed(components, path, word=f"mountword{i}")
        path.unlink()
    assert nested.is_dir()  # A detached mount can leave an empty mountpoint.

    app = SimpleNamespace(storage=components.storage, search_pipeline=components.search_pipeline)
    result = await _run_compaction(app)
    assert result["chunks_deleted"] == 0
    assert result["held_sources"] == count
    assert set(paths) == set(await components.storage.get_held_sources())
    for path in paths:
        assert await components.storage.get_chunk_hashes(path)
