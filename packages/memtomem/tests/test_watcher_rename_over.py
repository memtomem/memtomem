"""A save that renames a file over its target keeps the target searchable (#3031).

watchdog reports such a save as a delete of a path that still exists: the
FSEvents observer ends the burst with one, and the polling observer reports the
inode change as a delete and a create. The watcher journals a delete as a
pending source check, which hides the source from search until the debounced
reindex. It now probes the path first and journals only when the path is not
its regular file any more, within a bounded wait on the watchdog thread.
"""

from __future__ import annotations

import asyncio
import errno
import os
import shutil
import sys
import threading
import time
from pathlib import Path
from unittest import mock

import pytest
from watchdog.events import FileDeletedEvent, FileMovedEvent
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

from memtomem.indexing import watcher as watcher_module
from memtomem.indexing.watcher import (
    _REMOVED_PROBE_TIMEOUT_S,
    FileWatcher,
    _MarkdownEventHandler,
)


def _mock_embedder(components) -> None:
    embedder = mock.AsyncMock()
    embedder.embed_texts = mock.AsyncMock(
        side_effect=lambda texts, **_: [[0.1] * 1024 for _ in texts]
    )
    embedder.dimension = 1024
    components.index_engine._embedder = embedder


async def _index_note(components, path: Path, marker: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# Note\n\n{marker} text.\n", encoding="utf-8")
    _mock_embedder(components)
    await components.index_engine.index_file(path)
    assert await components.storage.bm25_search(marker), "the note must be indexed"


def _rename_over(path: Path, body: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, path)


def _symlink_or_skip(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:  # Windows without the privilege
        pytest.skip(f"symlinks unavailable: {exc}")


def _handler(components) -> tuple[FileWatcher, _MarkdownEventHandler]:
    watcher = FileWatcher(components.index_engine, components.config.indexing)
    return watcher, watcher._make_handler(asyncio.get_running_loop())


async def _queued(watcher: FileWatcher) -> set[Path]:
    await asyncio.sleep(0)  # ``call_soon_threadsafe`` schedules the put.
    out: set[Path] = set()
    while not watcher._queue.empty():
        out.add(watcher._queue.get_nowait())
    return out


async def _assert_hidden_as_stored(components, note: Path, marker: str) -> None:
    # ``is_source_pending`` resolves its argument, which now names another file.
    assert note in await components.storage.get_pending_source_checks()
    assert await components.storage.bm25_search(marker) == []


# ===========================================================================
# What the probe decides
# ===========================================================================


async def test_a_path_that_is_still_its_file_is_not_hidden(components, memory_dir):
    note = memory_dir / "note.md"
    await _index_note(components, note, "zebrakeep")
    _rename_over(note, "# Note\n\nzebrakeep and more.\n")
    watcher, handler = _handler(components)

    handler.on_deleted(FileDeletedEvent(str(note)))

    assert not await components.storage.is_source_pending(note)
    assert await components.storage.bm25_search("zebrakeep")
    assert await _queued(watcher) == {note}, "the event must still reach the reindex"


async def test_a_deleted_path_is_hidden(components, memory_dir):
    note = memory_dir / "gone.md"
    await _index_note(components, note, "zebragone")
    note.unlink()
    _watcher, handler = _handler(components)

    handler.on_deleted(FileDeletedEvent(str(note)))

    assert await components.storage.is_source_pending(note)
    assert await components.storage.bm25_search("zebragone") == []


@pytest.mark.requires_symlinks
async def test_a_path_that_became_a_link_is_hidden(components, memory_dir, tmp_path):
    note = memory_dir / "linked.md"
    await _index_note(components, note, "zebralink")
    other = tmp_path / "elsewhere.md"
    other.write_text("# Other\n", encoding="utf-8")
    note.unlink()
    _symlink_or_skip(note, other)
    _watcher, handler = _handler(components)

    handler.on_deleted(FileDeletedEvent(str(note)))

    await _assert_hidden_as_stored(components, note, "zebralink")


@pytest.mark.requires_symlinks
async def test_a_parent_that_became_a_link_is_hidden(components, memory_dir, tmp_path):
    """The file at the path is another one: without the journal, ``_reindex``
    drops the now-outside path and the stored rows stay searchable for good."""
    note = memory_dir / "sub" / "a.md"
    await _index_note(components, note, "zebraparent")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.md").write_text("# Outside\n", encoding="utf-8")
    shutil.rmtree(memory_dir / "sub")
    _symlink_or_skip(memory_dir / "sub", outside, directory=True)
    assert note.is_file(), "the path must present the outside file, or this proves nothing"
    _watcher, handler = _handler(components)

    handler.on_deleted(FileDeletedEvent(str(note)))

    await _assert_hidden_as_stored(components, note, "zebraparent")


async def test_a_component_that_cannot_be_resolved_is_hidden(components, memory_dir, monkeypatch):
    note = memory_dir / "eio.md"
    await _index_note(components, note, "zebraeio")
    _watcher, handler = _handler(components)
    real_realpath = os.path.realpath

    def failing_realpath(path, *, strict=False):
        if os.fspath(path) != str(note):
            return real_realpath(path, strict=strict)
        # What ``realpath`` does when a component's lookup fails: a strict
        # call raises, any other returns the spelling it was given.
        if strict:
            raise OSError(errno.EIO, "I/O error")
        return os.fspath(path)

    with monkeypatch.context() as m:
        m.setattr(os.path, "realpath", failing_realpath)
        handler.on_deleted(FileDeletedEvent(str(note)))

    assert await components.storage.is_source_pending(note)


async def test_a_probe_thread_that_cannot_start_is_hidden(components, memory_dir, monkeypatch):
    note = memory_dir / "nothread.md"
    await _index_note(components, note, "zebranothread")
    _watcher, handler = _handler(components)
    real_start = threading.Thread.start

    def refusing_start(self):
        if self.name == "memtomem-removed-probe":
            raise RuntimeError("can't start new thread")
        real_start(self)

    with monkeypatch.context() as m:
        m.setattr(threading.Thread, "start", refusing_start)
        handler.on_deleted(FileDeletedEvent(str(note)))

    assert await components.storage.is_source_pending(note)


async def test_a_moved_source_that_is_back_in_place_is_not_hidden(components, memory_dir):
    """An editor that renames the original away and writes it again."""
    note = memory_dir / "swap.md"
    await _index_note(components, note, "zebraswap")
    backup = memory_dir / "swap.md~"
    note.rename(backup)
    note.write_text("# Note\n\nzebraswap again.\n", encoding="utf-8")
    _watcher, handler = _handler(components)

    handler.on_moved(FileMovedEvent(str(note), str(backup)))

    assert not await components.storage.is_source_pending(note)


# ===========================================================================
# What the probe costs the watchdog thread
# ===========================================================================


async def _present_notes(components, memory_dir, count: int, stem: str) -> list[Path]:
    notes = []
    for i in range(count):
        note = memory_dir / f"{stem}{i}.md"
        await _index_note(components, note, f"zebra{stem}{i}")
        notes.append(note)
    return notes


async def test_a_stalled_probe_journals_and_starts_no_other(components, memory_dir, monkeypatch):
    notes = await _present_notes(components, memory_dir, 3, "stall")
    release = threading.Event()
    calls: list[Path] = []

    def blocking_probe(path: Path) -> bool:
        calls.append(path)
        if len(calls) == 1:
            release.wait(30)
        return True

    monkeypatch.setattr(watcher_module, "_path_still_presents_its_file", blocking_probe)
    watcher, handler = _handler(components)
    try:
        began = time.monotonic()
        handler.on_deleted(FileDeletedEvent(str(notes[0])))
        waited = time.monotonic() - began
        assert waited < _REMOVED_PROBE_TIMEOUT_S + 0.5
        assert await components.storage.is_source_pending(notes[0])

        handler.on_deleted(FileDeletedEvent(str(notes[1])))
        assert await components.storage.is_source_pending(notes[1])
        assert calls == [notes[0]], "no probe may start while one is stalled"
    finally:
        release.set()

    for thread in list(watcher._stalled_removed_probes):
        thread.join(5)
    handler.on_deleted(FileDeletedEvent(str(notes[2])))
    assert not await components.storage.is_source_pending(notes[2]), (
        "once the stalled probe ends, probing resumes"
    )


async def test_many_saves_never_bring_the_hide_back(components, memory_dir, monkeypatch):
    """Nothing is used up by healthy probes: each one that answers in time
    keeps its file visible, however many come in a row."""
    notes = await _present_notes(components, memory_dir, 4, "many")
    calls = 0

    def slow_probe(path: Path) -> bool:
        nonlocal calls
        calls += 1
        time.sleep(0.01)
        return True

    monkeypatch.setattr(watcher_module, "_path_still_presents_its_file", slow_probe)
    _watcher, handler = _handler(components)
    for i in range(100):
        handler.on_deleted(FileDeletedEvent(str(notes[i % len(notes)])))

    assert calls == 100
    assert not [n for n in notes if await components.storage.is_source_pending(n)]


async def test_concurrent_probes_do_not_turn_each_other_into_a_hide(
    components, memory_dir, monkeypatch
):
    """``reconfigure`` briefly runs two observers on one handler: two callbacks
    for one rename-over can probe at once."""
    note = memory_dir / "twice.md"
    await _index_note(components, note, "zebratwice")
    # A generous wait: what is under test is that a running probe does not make
    # the second callback journal, not how fast a runner schedules it.
    monkeypatch.setattr(watcher_module, "_REMOVED_PROBE_TIMEOUT_S", 10.0)
    barrier = threading.Barrier(2, timeout=8.0)
    first_probing = threading.Event()

    def meeting_probe(path: Path) -> bool:
        first_probing.set()
        barrier.wait()  # Raises unless both probes run at the same time.
        return True

    monkeypatch.setattr(watcher_module, "_path_still_presents_its_file", meeting_probe)
    _watcher, handler = _handler(components)
    callbacks = [
        threading.Thread(target=handler.on_deleted, args=(FileDeletedEvent(str(note)),))
        for _ in range(2)
    ]
    callbacks[0].start()
    # The second callback arrives while the first one's probe is running.
    assert first_probing.wait(5)
    callbacks[1].start()
    for thread in callbacks:
        thread.join(10)

    assert not barrier.broken
    assert not await components.storage.is_source_pending(note)


# ===========================================================================
# A real observer
# ===========================================================================


@pytest.mark.parametrize(
    "backend",
    [
        "polling",
        pytest.param(
            "native",
            marks=pytest.mark.skipif(sys.platform != "darwin", reason="FSEvents is macOS-only"),
        ),
    ],
)
async def test_a_rename_over_save_never_hides_the_file(
    components, memory_dir, monkeypatch, backend
):
    note = memory_dir / "saved.md"
    await _index_note(components, note, "zebrashared")
    deleted: list[Path] = []
    seen: list[Path] = []
    journaled: list[Path] = []
    storage = components.storage
    real_journal = storage.queue_source_check_sync
    real_enqueue = _MarkdownEventHandler._enqueue

    def spy_journal(path: Path) -> bool:
        journaled.append(Path(path))
        return real_journal(path)

    def spy_enqueue(self, path: str, *, removed: bool = False) -> None:
        seen.append(Path(path))
        if removed:
            deleted.append(Path(path))
        real_enqueue(self, path, removed=removed)

    monkeypatch.setattr(storage, "queue_source_check_sync", spy_journal)
    monkeypatch.setattr(_MarkdownEventHandler, "_enqueue", spy_enqueue)
    monkeypatch.setattr(
        watcher_module,
        "_create_observer",
        lambda _c: PollingObserver(timeout=0.05) if backend == "polling" else Observer(),
    )
    watcher = FileWatcher(components.index_engine, components.config.indexing, debounce_ms=1500)
    await watcher.start()
    if backend == "native" and watcher.observer_error is not None:
        await watcher.stop()
        pytest.skip(f"the native observer could not start here: {watcher.observer_error}")
    assert watcher.observer_error is None
    try:
        ready = memory_dir / "ready.md"
        deadline = time.monotonic() + 10
        while ready not in seen:
            assert time.monotonic() < deadline, "the observer never delivered an event"
            ready.write_text(f"# Ready {time.monotonic()}\n", encoding="utf-8")
            await asyncio.sleep(0.2)

        _rename_over(note, "# Note\n\nzebrashared zebrafresh text.\n")
        deadline = time.monotonic() + 15
        while not await storage.bm25_search("zebrafresh"):
            assert time.monotonic() < deadline, "the new content was never indexed"
            assert await storage.bm25_search("zebrashared"), "the saved file was hidden"
            await asyncio.sleep(0.05)
    finally:
        await watcher.stop()

    assert note in deleted, f"the {backend} observer reported no delete, so this proves nothing"
    assert note not in journaled, "the rename-over was journaled as a delete"
