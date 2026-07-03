"""Watcher delete/move handling: deleting or renaming a watched file must
remove its chunks from the index (regression coverage for #1566).

Two layers:

* ``_MarkdownEventHandler`` — the watchdog handler correctly enqueues the
  right path(s) for ``on_deleted`` / ``on_moved`` (with the suffix filter).
* ``IndexEngine.index_file`` — a path that no longer exists on disk purges
  its stale chunks via ``delete_by_source``, gated on the containing index
  root still existing (mount-blip brake) and without resurrecting a deleted
  parent directory.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from unittest import mock

from watchdog.events import (
    DirDeletedEvent,
    FileDeletedEvent,
    FileMovedEvent,
)

from memtomem.indexing.watcher import _STOP_SENTINEL, FileWatcher, _MarkdownEventHandler


def _mock_embedder(components):
    """Wire a deterministic in-memory embedder onto the engine (no ONNX)."""
    embedder = mock.AsyncMock()
    embedder.embed_texts = mock.AsyncMock(
        side_effect=lambda texts, **_: [[0.1] * 1024 for _ in texts]
    )
    embedder.dimension = 1024
    components.index_engine._embedder = embedder
    return embedder


async def _index_content(components, path: Path, body: str = "# Note\n\nSome content.\n") -> None:
    path.write_text(body, encoding="utf-8")
    _mock_embedder(components)
    await components.index_engine.index_file(path)


# ===========================================================================
# Handler-level: which path(s) get enqueued
# ===========================================================================


class TestMarkdownEventHandlerDeleteMove:
    """``_MarkdownEventHandler`` enqueue behavior for delete/move events."""

    def _handler(self):
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Path] = asyncio.Queue()
        handler = _MarkdownEventHandler(queue, loop, frozenset({".md"}))
        return handler, queue

    def _drain(self, queue: asyncio.Queue[Path]) -> set[Path]:
        out: set[Path] = set()
        while not queue.empty():
            out.add(queue.get_nowait())
        return out

    async def test_on_deleted_enqueues_md(self):
        handler, queue = self._handler()
        handler.on_deleted(FileDeletedEvent("/mem/gone.md"))
        await asyncio.sleep(0)  # let call_soon_threadsafe run
        assert self._drain(queue) == {Path("/mem/gone.md")}

    async def test_on_deleted_filters_unsupported_suffix(self):
        handler, queue = self._handler()
        handler.on_deleted(FileDeletedEvent("/mem/gone.txt"))
        await asyncio.sleep(0)
        assert self._drain(queue) == set()

    async def test_on_deleted_ignores_directory(self):
        handler, queue = self._handler()
        handler.on_deleted(DirDeletedEvent("/mem/subdir"))
        await asyncio.sleep(0)
        assert self._drain(queue) == set()

    async def test_on_moved_enqueues_both_paths(self):
        handler, queue = self._handler()
        handler.on_moved(FileMovedEvent("/mem/old.md", "/mem/new.md"))
        await asyncio.sleep(0)
        assert self._drain(queue) == {Path("/mem/old.md"), Path("/mem/new.md")}

    async def test_on_moved_rename_away_from_md_keeps_old_path(self):
        # dest is a non-.md path (renamed away) — only the .md src survives the
        # suffix filter, so the old path's stale chunks still get cleaned.
        handler, queue = self._handler()
        handler.on_moved(FileMovedEvent("/mem/old.md", "/mem/old.txt"))
        await asyncio.sleep(0)
        assert self._drain(queue) == {Path("/mem/old.md")}

    async def test_on_moved_rename_into_md_keeps_new_path(self):
        handler, queue = self._handler()
        handler.on_moved(FileMovedEvent("/mem/old.txt", "/mem/new.md"))
        await asyncio.sleep(0)
        assert self._drain(queue) == {Path("/mem/new.md")}


# ===========================================================================
# Engine-level: missing file == delete-by-source
# ===========================================================================


class TestDeleteMissingSource:
    """``index_file`` purges chunks for a source file that is gone from disk."""

    async def test_deleted_file_removes_its_chunks(self, components, memory_dir):
        md_path = memory_dir / "delete_me.md"
        await _index_content(components, md_path)
        assert len(await components.storage.get_chunk_hashes(md_path)) > 0

        md_path.unlink()
        stats = await components.index_engine.index_file(md_path)

        assert stats.deleted_chunks > 0
        assert len(await components.storage.get_chunk_hashes(md_path)) == 0

    async def test_transient_oserror_does_not_delete(self, components, memory_dir):
        """A non-ENOENT OSError (EACCES/EIO) must never delete — only a genuine
        missing file does. Guards against a permission/mount blip mass-deleting."""
        md_path = memory_dir / "blip.md"
        await _index_content(components, md_path)
        assert len(await components.storage.get_chunk_hashes(md_path)) > 0

        original_stat = Path.stat

        def fake_stat(self, *args, **kwargs):
            if self.name == "blip.md":
                raise PermissionError("simulated transient error")
            return original_stat(self, *args, **kwargs)

        with mock.patch.object(Path, "stat", autospec=True, side_effect=fake_stat):
            stats = await components.index_engine.index_file(md_path)

        assert stats.deleted_chunks == 0
        assert len(await components.storage.get_chunk_hashes(md_path)) > 0

    async def test_unmounted_root_does_not_delete(self, components, memory_dir):
        """If the whole index root is gone (unmount), skip deletion — the
        two-pass orphan scan adjudicates that case instead."""
        md_path = memory_dir / "note.md"
        await _index_content(components, md_path)
        assert len(await components.storage.get_chunk_hashes(md_path)) > 0

        # Remove the entire memory_dir root out from under the engine.
        shutil.rmtree(memory_dir)
        stats = await components.index_engine.index_file(md_path)

        assert stats.deleted_chunks == 0
        assert len(await components.storage.get_chunk_hashes(md_path)) > 0

    async def test_subdir_deletion_removes_chunks_without_resurrecting_dir(
        self, components, memory_dir
    ):
        """Deleting a file whose subdirectory was removed must clean its chunks
        and must NOT recreate the deleted directory (the sidecar-lock mkdir
        hazard)."""
        subdir = memory_dir / "sub"
        subdir.mkdir()
        md_path = subdir / "nested.md"
        await _index_content(components, md_path)
        assert len(await components.storage.get_chunk_hashes(md_path)) > 0

        shutil.rmtree(subdir)
        stats = await components.index_engine.index_file(md_path)

        assert stats.deleted_chunks > 0
        assert len(await components.storage.get_chunk_hashes(md_path)) == 0
        assert not subdir.exists(), "deleted subdirectory must not be resurrected by the lock mkdir"

    async def test_rename_cleans_old_path_and_indexes_new(self, components, memory_dir):
        old_path = memory_dir / "before.md"
        new_path = memory_dir / "after.md"
        await _index_content(components, old_path)
        assert len(await components.storage.get_chunk_hashes(old_path)) > 0

        old_path.rename(new_path)

        del_stats = await components.index_engine.index_file(old_path)
        assert del_stats.deleted_chunks > 0
        assert len(await components.storage.get_chunk_hashes(old_path)) == 0

        add_stats = await components.index_engine.index_file(new_path)
        assert add_stats.indexed_chunks > 0
        assert len(await components.storage.get_chunk_hashes(new_path)) > 0

    async def test_deleted_excluded_path_is_still_purged(self, components, memory_dir):
        """Cleanup is not blocked by exclude: a file indexed before an exclude
        pattern was added, then deleted, must still have its stale chunks purged
        — matching the exclude-agnostic orphan sweep (otherwise deleted-and-
        excluded content would persist forever)."""
        md_path = memory_dir / "was_indexed.md"
        await _index_content(components, md_path)
        assert len(await components.storage.get_chunk_hashes(md_path)) > 0

        components.config.indexing.exclude_patterns = ["was_indexed.md"]
        md_path.unlink()
        stats = await components.index_engine.index_file(md_path)

        assert stats.deleted_chunks > 0
        assert len(await components.storage.get_chunk_hashes(md_path)) == 0

    async def test_present_excluded_path_is_not_indexed_or_purged(self, components, memory_dir):
        """A still-present excluded file is a no-op: its (pre-exclude) chunks are
        left intact and no new content is indexed — exclude blocks *indexing*,
        the delete path only fires for a missing file."""
        md_path = memory_dir / "still_here.md"
        await _index_content(components, md_path)
        before = len(await components.storage.get_chunk_hashes(md_path))
        assert before > 0

        components.config.indexing.exclude_patterns = ["still_here.md"]
        stats = await components.index_engine.index_file(md_path)

        assert stats.deleted_chunks == 0
        assert stats.indexed_chunks == 0
        assert len(await components.storage.get_chunk_hashes(md_path)) == before

    async def test_file_replaced_by_directory_purges_chunks(self, components, memory_dir):
        """A source file swapped for a directory of the same name (stat succeeds,
        read raises IsADirectoryError) is gone as a *file* — purge its chunks."""
        md_path = memory_dir / "swap.md"
        await _index_content(components, md_path)
        assert len(await components.storage.get_chunk_hashes(md_path)) > 0

        md_path.unlink()
        md_path.mkdir()  # same name, now a directory
        stats = await components.index_engine.index_file(md_path)

        assert stats.deleted_chunks > 0
        assert len(await components.storage.get_chunk_hashes(md_path)) == 0

    async def test_excluded_file_replaced_by_directory_still_purges(self, components, memory_dir):
        """Intersection of the two edges: a file that is both excluded AND
        replaced by a same-named directory. ``exists()`` is true for the dir, so
        keying cleanup off ``is_file`` (not ``exists``) and checking the stat
        mode before the exclude guard is what lets the stale chunks be purged."""
        md_path = memory_dir / "excluded_swap.md"
        await _index_content(components, md_path)
        assert len(await components.storage.get_chunk_hashes(md_path)) > 0

        components.config.indexing.exclude_patterns = ["excluded_swap.md"]
        md_path.unlink()
        md_path.mkdir()  # excluded name, now a directory
        stats = await components.index_engine.index_file(md_path)

        assert stats.deleted_chunks > 0
        assert len(await components.storage.get_chunk_hashes(md_path)) == 0

    async def test_nested_root_removed_engages_brake(self, components, tmp_path):
        """When nested roots are configured and the most-specific one is removed,
        the brake keys off *that* root (not a surviving parent) and skips the
        delete — leaving the bulk case to the two-pass mass-orphan scan."""
        outer = tmp_path / "outer"
        inner = outer / "inner"
        inner.mkdir(parents=True)
        components.config.indexing.memory_dirs = [outer, inner]

        nested = inner / "nested.md"
        await _index_content(components, nested)
        assert len(await components.storage.get_chunk_hashes(nested)) > 0

        shutil.rmtree(inner)  # inner root gone, outer survives
        stats = await components.index_engine.index_file(nested)

        assert stats.deleted_chunks == 0
        assert len(await components.storage.get_chunk_hashes(nested)) > 0


# ===========================================================================
# Mid-level: the consumer drives the delete end-to-end (no real Observer)
# ===========================================================================


async def test_process_events_deletes_via_queue(components, memory_dir):
    """Feed the deleted path + stop sentinel straight into the queue and run
    the consumer — deterministic, no filesystem-event timing."""
    md_path = memory_dir / "queued.md"
    await _index_content(components, md_path)
    assert len(await components.storage.get_chunk_hashes(md_path)) > 0

    watcher = FileWatcher(
        index_engine=components.index_engine,
        config=components.config.indexing,
        debounce_ms=100,
    )
    md_path.unlink()
    watcher._queue.put_nowait(md_path)
    watcher._queue.put_nowait(_STOP_SENTINEL)

    await watcher._process_events()

    assert len(await components.storage.get_chunk_hashes(md_path)) == 0


async def test_reindex_logs_removed_not_reindexed(components, memory_dir, caplog):
    """The delete pass logs 'Removed deleted file from index', not the
    misleading 'Auto-reindexed ... indexed=0 ... deleted=N'."""
    md_path = memory_dir / "logtest.md"
    await _index_content(components, md_path)

    watcher = FileWatcher(
        index_engine=components.index_engine,
        config=components.config.indexing,
        debounce_ms=100,
    )
    md_path.unlink()

    with caplog.at_level(logging.INFO, logger="memtomem.indexing.watcher"):
        await watcher._reindex(md_path)

    messages = [r.getMessage() for r in caplog.records]
    assert any("Removed deleted file from index" in m for m in messages)
    assert not any("Auto-reindexed" in m for m in messages)
