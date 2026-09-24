"""Watcher queue overflow: a dropped event must still reach the index (#2530).

The watchdog thread hands each event to the loop with
``call_soon_threadsafe(_put_or_warn, path)``, and the consumer in
``_process_events`` drains the bounded queue whenever it gets a turn. A drop
therefore needs more than ``maxsize`` puts to run between two consumer turns,
which happens two ways, and each has a test here:

* the loop thread is held off while the watchdog thread delivers a burst;
* the consumer is inside ``_flush_batch``: it does not read the queue until the
  batch finishes, so a second burst during a flush overflows on a free loop.

A drop cannot say which dropped paths had changed, so the watcher rescans the
dropped path's root once the burst settles. The queue size is shrunk for speed;
the mechanism does not depend on it.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from unittest import mock

import pytest
from watchdog.events import FileDeletedEvent, FileModifiedEvent, FileMovedEvent

from memtomem.indexing import watcher as watcher_module
from memtomem.indexing.watcher import _STOP_SENTINEL, FileWatcher

_MAXSIZE = 8


def _mock_embedder(components) -> None:
    embedder = mock.AsyncMock()
    embedder.embed_texts = mock.AsyncMock(
        side_effect=lambda texts, **_: [[0.1] * 1024 for _ in texts]
    )
    embedder.dimension = 1024
    components.index_engine._embedder = embedder


async def _seed(components, memory_dir: Path) -> tuple[list[Path], Path]:
    """Index ``_MAXSIZE`` fillers plus ``changed.md``, then change it on disk."""
    _mock_embedder(components)
    fillers = [memory_dir / f"filler-{i}.md" for i in range(_MAXSIZE)]
    for i, path in enumerate(fillers):
        path.write_text(f"# Filler {i}\n\nUnchanged filler body {i}.\n", encoding="utf-8")
        await components.index_engine.index_file(path)
    changed = memory_dir / "changed.md"
    changed.write_text("# Note\n\nold-marker body\n", encoding="utf-8")
    await components.index_engine.index_file(changed)
    changed.write_text("# Note\n\nnew-marker body\n", encoding="utf-8")
    return fillers, changed


async def _wait_for_marker(components, path: Path, marker: str, timeout: float = 5.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        chunks = await components.storage.list_chunks_by_source(path)
        if any(marker in c.content for c in chunks):
            return True
        await asyncio.sleep(0.05)
    return False


def _fire(handler, paths: list[Path]) -> None:
    for path in paths:
        handler.on_modified(FileModifiedEvent(str(path)))


async def _stop(watcher: FileWatcher, task: asyncio.Task[None]) -> None:
    await watcher._queue.put(_STOP_SENTINEL)
    await asyncio.wait_for(task, timeout=10)


@pytest.fixture
def small_queue(monkeypatch):
    monkeypatch.setattr(watcher_module, "_WATCHER_QUEUE_MAXSIZE", _MAXSIZE)


async def test_change_dropped_while_the_loop_is_held_off_is_indexed(
    small_queue, components, memory_dir, caplog
):
    fillers, changed = await _seed(components, memory_dir)
    watcher = FileWatcher(components.index_engine, components.config.indexing, debounce_ms=100)
    handler = watcher._make_handler(asyncio.get_running_loop())
    task = asyncio.create_task(watcher._process_events())
    await asyncio.sleep(0)  # park the consumer on ``queue.get()``

    producer = threading.Thread(target=_fire, args=(handler, [*fillers, changed]))
    with caplog.at_level(logging.WARNING, logger="memtomem.indexing.watcher"):
        producer.start()
        # Joining on the loop thread holds the loop off for the whole burst,
        # so every put is queued before the consumer gets a turn.
        producer.join()
        indexed = await _wait_for_marker(components, changed, "new-marker")
    await _stop(watcher, task)

    drops = [r.getMessage() for r in caplog.records if "queue full" in r.getMessage()]
    assert drops, "the burst must overflow, or this test proves nothing"
    assert indexed, "the dropped change never reached the index"
    assert str(changed) in drops[0], "the warning must name the full path"
    assert str(memory_dir) in drops[0] and "rescan" in drops[0]


async def test_change_dropped_during_a_flush_is_indexed(small_queue, components, memory_dir):
    fillers, changed = await _seed(components, memory_dir)
    watcher = FileWatcher(components.index_engine, components.config.indexing, debounce_ms=100)
    handler = watcher._make_handler(asyncio.get_running_loop())

    gate = asyncio.Event()
    flushing = asyncio.Event()
    real_index_file = components.index_engine.index_file

    async def held_index_file(path, *args, **kwargs):
        flushing.set()
        await gate.wait()
        return await real_index_file(path, *args, **kwargs)

    with mock.patch.object(components.index_engine, "index_file", held_index_file):
        task = asyncio.create_task(watcher._process_events())
        _fire(handler, [fillers[0]])
        await asyncio.wait_for(flushing.wait(), timeout=5)
        # The loop is free for the whole burst; only the consumer is busy.
        await asyncio.to_thread(_fire, handler, [*fillers, changed])
        assert watcher._queue.full(), "the burst must overflow, or this test proves nothing"
        gate.set()
        indexed = await _wait_for_marker(components, changed, "new-marker")
        await _stop(watcher, task)

    assert indexed, "the dropped change never reached the index"


class _BurstObserver:
    """Stands in for watchdog's observer. ``start`` delivers a burst from
    another thread and waits for it, the way a live observer can fire, and
    overflow, before the processor task takes its first turn."""

    def __init__(self, paths: list[Path]) -> None:
        self._paths = paths
        self._handler = None

    def schedule(self, handler, path, recursive=False):
        self._handler = handler
        return object()

    def start(self) -> None:
        producer = threading.Thread(target=_fire, args=(self._handler, self._paths))
        producer.start()
        producer.join()

    def stop(self) -> None:
        pass

    def is_alive(self) -> bool:
        return False

    def join(self) -> None:
        pass


async def test_overflow_before_the_processor_first_turn_is_rescanned(
    small_queue, components, memory_dir, monkeypatch
):
    """``start`` must not lose a drop recorded before ``_process_events`` runs."""
    fillers, changed = await _seed(components, memory_dir)
    observer = _BurstObserver([*fillers, changed])
    monkeypatch.setattr(watcher_module, "_create_observer", lambda _config: observer)
    watcher = FileWatcher(components.index_engine, components.config.indexing, debounce_ms=100)

    await watcher.start()
    try:
        indexed = await _wait_for_marker(components, changed, "new-marker")
    finally:
        await watcher.stop()

    assert indexed, "a drop before the processor's first turn was forgotten"


async def test_rescan_is_not_held_behind_pending_backoff(
    small_queue, components, memory_dir, monkeypatch
):
    """A due rescan runs at the debounce deadline even when every pending path
    is inside its retry backoff (here 20 s)."""
    fillers, changed = await _seed(components, memory_dir)
    monkeypatch.setattr(watcher_module.random, "uniform", lambda _a, _b: 10.0)
    watcher = FileWatcher(components.index_engine, components.config.indexing, debounce_ms=100)
    handler = watcher._make_handler(asyncio.get_running_loop())
    real_index_path = components.index_engine.index_path
    walks = 0

    async def locked_index_file(path, *args, **kwargs):
        raise TimeoutError  # every filler stays locked, so it goes into backoff

    async def flaky_index_path(path, *args, **kwargs):
        nonlocal walks
        walks += 1
        if walks == 1:
            raise watcher_module.RetryableError("store busy")
        return await real_index_path(path, *args, **kwargs)

    with (
        mock.patch.object(components.index_engine, "index_file", locked_index_file),
        mock.patch.object(components.index_engine, "index_path", flaky_index_path),
    ):
        task = asyncio.create_task(watcher._process_events())
        await asyncio.sleep(0)
        producer = threading.Thread(target=_fire, args=(handler, [*fillers, changed]))
        producer.start()
        producer.join()
        indexed = await _wait_for_marker(components, changed, "new-marker")
        await _stop(watcher, task)

    assert walks == 2
    assert indexed, "the retried rescan waited for the pending paths' backoff"


async def test_retryable_rescan_failure_is_bounded(small_queue, components, memory_dir, caplog):
    watcher = FileWatcher(components.index_engine, components.config.indexing, debounce_ms=50)
    calls = 0

    async def always_retryable(path, *args, **kwargs):
        nonlocal calls
        calls += 1
        raise watcher_module.RetryableError("store down")

    with mock.patch.object(components.index_engine, "index_path", always_retryable):
        task = asyncio.create_task(watcher._process_events())
        with caplog.at_level(logging.WARNING, logger="memtomem.indexing.watcher"):
            watcher._note_overflow(memory_dir / "dropped.md")
            deadline = asyncio.get_running_loop().time() + 10
            while watcher._rescan_due and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.2)  # room for a wrong fourth walk to show up
        await _stop(watcher, task)

    assert calls == watcher_module._BACKFILL_MAX_ATTEMPTS
    assert not watcher._rescan_due
    final = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(final) == 1 and f"mm index {memory_dir}" in final[0].getMessage()


async def test_rescan_skips_a_root_removed_after_the_drop(components, memory_dir, tmp_path):
    watcher = FileWatcher(components.index_engine, components.config.indexing, debounce_ms=50)
    watcher._note_overflow(memory_dir / "dropped.md")
    assert set(watcher._rescan_due) == {memory_dir}
    other = tmp_path / "other"
    other.mkdir()
    await watcher.reconfigure(
        components.config.indexing.model_copy(update={"memory_dirs": [other]})
    )

    with mock.patch.object(components.index_engine, "index_path") as index_path:
        await watcher._rescan_overflowed_roots(set())

    index_path.assert_not_called()
    assert not watcher._rescan_due
    assert not watcher._dropped_paths


async def test_stop_names_a_rescan_it_cancelled(
    small_queue, components, memory_dir, monkeypatch, caplog
):
    """A walk cancelled by ``stop``'s timeout is no longer in ``_rescan_due``;
    the warning must still name its root."""
    fillers, changed = await _seed(components, memory_dir)
    monkeypatch.setattr(
        watcher_module, "_create_observer", lambda _config: _BurstObserver([*fillers, changed])
    )
    monkeypatch.setattr(watcher_module, "_STOP_FLUSH_TIMEOUT_S", 0.2)
    watcher = FileWatcher(components.index_engine, components.config.indexing, debounce_ms=50)
    walking = asyncio.Event()

    async def hung_index_path(path, *args, **kwargs):
        walking.set()
        await asyncio.Event().wait()

    with mock.patch.object(components.index_engine, "index_path", hung_index_path):
        await watcher.start()
        await asyncio.wait_for(walking.wait(), timeout=5)
        with caplog.at_level(logging.WARNING, logger="memtomem.indexing.watcher"):
            await watcher.stop()

    stopped = [
        r.getMessage() for r in caplog.records if "stopped before rescanning" in r.getMessage()
    ]
    assert len(stopped) == 1 and str(memory_dir) in stopped[0]


async def test_drop_during_a_walk_marks_the_root_again(components, memory_dir):
    """The walk may already have passed a file dropped while it runs."""
    watcher = FileWatcher(components.index_engine, components.config.indexing, debounce_ms=50)
    real_index_path = components.index_engine.index_path
    walks = 0

    async def walk(path, *args, **kwargs):
        nonlocal walks
        walks += 1
        if walks == 1:
            watcher._note_overflow(memory_dir / "late.md")
        return await real_index_path(path, *args, **kwargs)

    watcher._note_overflow(memory_dir / "early.md")
    with mock.patch.object(components.index_engine, "index_path", walk):
        await watcher._rescan_overflowed_roots(set())
        assert set(watcher._rescan_due) == {memory_dir}
        await watcher._rescan_overflowed_roots(set())

    assert walks == 2
    assert not watcher._rescan_due


async def test_drop_maps_to_the_most_specific_root(components, memory_dir):
    nested = memory_dir / "nested"
    nested.mkdir()
    config = components.config.indexing.model_copy(
        update={"memory_dirs": [memory_dir], "project_memory_dirs": [nested]}
    )
    watcher = FileWatcher(components.index_engine, config, debounce_ms=50)

    watcher._note_overflow(nested / "a.md")
    watcher._note_overflow(memory_dir / "b.md")

    assert watcher._rescan_due == {nested: 1, memory_dir: 1}


async def test_only_the_first_drop_per_root_warns(components, memory_dir, caplog):
    watcher = FileWatcher(components.index_engine, components.config.indexing, debounce_ms=50)
    with caplog.at_level(logging.DEBUG, logger="memtomem.indexing.watcher"):
        for i in range(3):
            watcher._note_overflow(memory_dir / f"d{i}.md")

    drops = [r for r in caplog.records if "queue full" in r.getMessage()]
    assert [r.levelno for r in drops] == [logging.WARNING, logging.DEBUG, logging.DEBUG]
    assert all(str(memory_dir / f"d{i}.md") in r.getMessage() for i, r in enumerate(drops))
    assert watcher._rescan_due == {memory_dir: 3}


async def test_root_removed_during_an_earlier_walk_is_skipped(components, memory_dir, tmp_path):
    second = tmp_path / "second"
    second.mkdir()
    config = components.config.indexing.model_copy(update={"memory_dirs": [memory_dir, second]})
    watcher = FileWatcher(components.index_engine, config, debounce_ms=50)
    watcher._note_overflow(memory_dir / "a.md")
    watcher._note_overflow(second / "b.md")
    walked: list[Path] = []

    async def walk(path, *args, **kwargs):
        walked.append(path)
        # The first walk is awaited while a config change drops the other root.
        await watcher.reconfigure(config.model_copy(update={"memory_dirs": [path]}))
        return await real_index_path(path, *args, **kwargs)

    real_index_path = components.index_engine.index_path
    with mock.patch.object(components.index_engine, "index_path", walk):
        await watcher._rescan_overflowed_roots(set())

    assert len(walked) == 1


async def test_a_rescan_that_changes_nothing_still_logs_completion(components, memory_dir, caplog):
    _mock_embedder(components)
    note = memory_dir / "same.md"
    note.write_text("# Same\n\nUnchanged.\n", encoding="utf-8")
    await components.index_engine.index_file(note)
    watcher = FileWatcher(components.index_engine, components.config.indexing, debounce_ms=50)
    watcher._note_overflow(note)

    with caplog.at_level(logging.INFO, logger="memtomem.indexing.watcher"):
        await watcher._rescan_overflowed_roots(set())

    done = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Watcher rescan")]
    assert done == [f"Watcher rescan {memory_dir}: indexed=0 skipped=1 deleted=0"]


# -- Dropped paths are replayed as events (#2532) ------------------------------
#
# A walk only visits files that exist, so the rescan alone cannot purge a file
# whose delete event was dropped. The dropped path itself is handed back to the
# ordinary per-event pipeline.


async def _index_note(components, path: Path, marker: str) -> None:
    path.write_text(f"# Note\n\n{marker} body\n", encoding="utf-8")
    await components.index_engine.index_file(path)
    assert await components.storage.list_chunks_by_source(path)


async def _wait_until_gone(components, path: Path, timeout: float = 5.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if not await components.storage.list_chunks_by_source(path):
            return True
        await asyncio.sleep(0.05)
    return False


def _spy_replayed(watcher: FileWatcher) -> list[set[Path]]:
    """Record the dropped paths each rescan starts with. The drop callbacks run
    on the loop after the burst, so the dropped set is read inside the rescan."""
    replayed: list[set[Path]] = []
    real_rescan = watcher._rescan_overflowed_roots

    async def spy_rescan(pending):
        replayed.append({p for paths in watcher._dropped_paths.values() for p in paths})
        await real_rescan(pending)

    watcher._rescan_overflowed_roots = spy_rescan
    return replayed


def _fire_deleted(handler, path: Path) -> None:
    handler.on_deleted(FileDeletedEvent(str(path)))


async def test_delete_dropped_while_the_loop_is_held_off_is_purged(
    small_queue, components, memory_dir
):
    fillers, _changed = await _seed(components, memory_dir)
    gone = memory_dir / "gone.md"
    await _index_note(components, gone, "gone-marker")
    gone.unlink()
    watcher = FileWatcher(components.index_engine, components.config.indexing, debounce_ms=100)
    handler = watcher._make_handler(asyncio.get_running_loop())
    replayed = _spy_replayed(watcher)
    task = asyncio.create_task(watcher._process_events())
    await asyncio.sleep(0)

    def burst() -> None:
        _fire(handler, fillers)
        _fire_deleted(handler, gone)

    producer = threading.Thread(target=burst)
    producer.start()
    producer.join()
    purged = await _wait_until_gone(components, gone)
    await _stop(watcher, task)

    assert replayed and gone in replayed[0], "the delete must be dropped, or this proves nothing"
    assert purged, "a dropped delete left the file's chunks searchable"


async def test_move_dropped_during_a_burst_purges_its_source(small_queue, components, memory_dir):
    fillers, _changed = await _seed(components, memory_dir)
    src = memory_dir / "before.md"
    dest = memory_dir / "after.md"
    await _index_note(components, src, "moved-marker")
    src.rename(dest)
    watcher = FileWatcher(components.index_engine, components.config.indexing, debounce_ms=100)
    handler = watcher._make_handler(asyncio.get_running_loop())
    replayed = _spy_replayed(watcher)
    task = asyncio.create_task(watcher._process_events())
    await asyncio.sleep(0)

    def burst() -> None:
        _fire(handler, fillers)
        handler.on_moved(FileMovedEvent(str(src), str(dest)))

    producer = threading.Thread(target=burst)
    producer.start()
    producer.join()
    purged = await _wait_until_gone(components, src)
    indexed = await _wait_for_marker(components, dest, "moved-marker")
    await _stop(watcher, task)

    assert replayed and src in replayed[0], "the move must be dropped, or this proves nothing"
    assert purged, "a dropped move left its source's chunks searchable"
    assert indexed


async def test_rescan_replays_only_the_dropped_paths(components, memory_dir):
    """A file deleted without a dropped event keeps its rows: the rescan does
    not reconcile the whole root, so an emptied mount is not purged by it."""
    _mock_embedder(components)
    quiet = memory_dir / "quiet.md"
    await _index_note(components, quiet, "quiet-marker")
    quiet.unlink()
    present = memory_dir / "present.md"
    await _index_note(components, present, "present-marker")
    watcher = FileWatcher(components.index_engine, components.config.indexing, debounce_ms=50)
    watcher._note_overflow(present)

    pending: set[Path] = set()
    await watcher._rescan_overflowed_roots(pending)
    await watcher._flush_batch(pending)

    assert pending == {present}
    assert await components.storage.list_chunks_by_source(quiet)


async def test_drop_during_a_walk_is_replayed_by_the_next_rescan(components, memory_dir):
    watcher = FileWatcher(components.index_engine, components.config.indexing, debounce_ms=50)
    early = memory_dir / "early.md"
    late = memory_dir / "late.md"
    real_index_path = components.index_engine.index_path
    walks = 0

    async def walk(path, *args, **kwargs):
        nonlocal walks
        walks += 1
        if walks == 1:
            watcher._note_overflow(late)
        return await real_index_path(path, *args, **kwargs)

    watcher._note_overflow(early)
    pending: set[Path] = set()
    with mock.patch.object(components.index_engine, "index_path", walk):
        await watcher._rescan_overflowed_roots(pending)
        assert pending == {early}
        assert watcher._dropped_paths == {memory_dir: {late}}
        await watcher._rescan_overflowed_roots(pending)

    assert pending == {early, late}
    assert not watcher._dropped_paths


async def test_replayed_path_does_not_inherit_backoff(components, memory_dir):
    watcher = FileWatcher(components.index_engine, components.config.indexing, debounce_ms=50)
    path = memory_dir / "busy.md"
    watcher._retry_after[path] = 1e12
    watcher._retry_attempts[path] = 4
    watcher._note_overflow(path)

    await watcher._rescan_overflowed_roots(set())

    assert path not in watcher._retry_after
    assert path not in watcher._retry_attempts


async def test_stop_replays_a_dropped_delete_before_exiting(components, memory_dir):
    _mock_embedder(components)
    gone = memory_dir / "gone.md"
    await _index_note(components, gone, "gone-marker")
    gone.unlink()
    watcher = FileWatcher(components.index_engine, components.config.indexing, debounce_ms=60_000)
    watcher._note_overflow(gone)

    task = asyncio.create_task(watcher._process_events())
    await _stop(watcher, task)

    assert not await components.storage.list_chunks_by_source(gone)
