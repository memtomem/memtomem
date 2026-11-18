"""A metadata-only change still reaches the indexer under watchdog 7 (#3082).

watchdog 7.0 reports some metadata changes as ``FileAttribEvent``, dispatched
to ``on_attrib``: on Linux, every ``IN_ATTRIB`` (permissions, ownership,
timestamps), which 6.0 reported as a modification; with polling, a change of
mode, ownership or file flags, which 6.0 did not report at all. The handler
must route it to the indexing queue, or a file made unreadable keeps serving
its chunks until its content changes.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest
import watchdog.events

from memtomem.indexing.watcher import _MarkdownEventHandler

if not hasattr(watchdog.events, "FileAttribEvent"):
    pytest.skip("watchdog < 7.0 has no attribute events", allow_module_level=True)

from watchdog.events import DirAttribEvent, FileAttribEvent  # noqa: E402
from watchdog.observers.polling import PollingObserver  # noqa: E402


def _handler() -> tuple[_MarkdownEventHandler, asyncio.Queue[Path]]:
    queue: asyncio.Queue[Path] = asyncio.Queue()
    handler = _MarkdownEventHandler(queue, asyncio.get_running_loop(), frozenset({".md"}))
    return handler, queue


def _drain(queue: asyncio.Queue[Path]) -> list[Path]:
    out: list[Path] = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


async def test_dispatched_file_attrib_event_enqueues_the_file() -> None:
    handler, queue = _handler()
    handler.dispatch(FileAttribEvent("/mem/locked.md"))
    await asyncio.sleep(0)  # run the call_soon_threadsafe put
    assert _drain(queue) == [Path("/mem/locked.md")]


async def test_dispatched_attrib_event_keeps_the_suffix_filter() -> None:
    handler, queue = _handler()
    handler.dispatch(FileAttribEvent("/mem/locked.txt"))
    await asyncio.sleep(0)
    assert _drain(queue) == []


async def test_dispatched_dir_attrib_event_is_ignored() -> None:
    handler, queue = _handler()
    handler.dispatch(DirAttribEvent("/mem/sub.md"))
    await asyncio.sleep(0)
    assert _drain(queue) == []


@pytest.mark.skipif(sys.platform == "win32", reason="chmod only toggles read-only on Windows")
async def test_polling_observer_delivers_a_chmod_to_the_queue(tmp_path: Path) -> None:
    """End to end through a real observer: 6.0's polling snapshot compared only
    mtime and size, so this chmod produced no event at all."""
    note = tmp_path / "note.md"
    note.write_text("# Note\n\nbody\n", encoding="utf-8")
    handler, queue = _handler()
    observer = PollingObserver(timeout=0.1)
    observer.schedule(handler, str(tmp_path), recursive=True)
    observer.start()
    try:
        await asyncio.sleep(0.3)  # let the first snapshot settle
        os.chmod(note, 0o000)
        got = await asyncio.wait_for(queue.get(), timeout=10)
    finally:
        os.chmod(note, 0o644)
        observer.stop()
        await asyncio.to_thread(observer.join, 5)
    assert got.name == "note.md"
