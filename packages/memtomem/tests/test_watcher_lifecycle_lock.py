"""Restarting a watcher in one process: the lock, and the state a stop leaves.

``reconfigure`` used to hold a lock of its own, which was enough while the only
start and stop happened at lifespan entry and exit. A watcher that a degraded
start leaves stopped can now be started later by an embedding reset (#2188), so
a memory-directory route or the hot-reloader can be reconfiguring the very
instance that a stop — the cleanup after a failed start, or teardown — is
midway through dismantling.

``stop`` is the half that suspends: it waits on the processor task before
clearing the handles. A reconfigure arriving inside that window still sees a
live observer and schedules watches onto one that is already being torn down.
``start`` never awaits between assigning the handles and finishing, so it has
no such window today; the lock covers it because that is a property of the
current body rather than of the contract.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from memtomem.config import IndexingConfig
from memtomem.indexing.watcher import FileWatcher


@pytest.fixture
def watched(tmp_path, monkeypatch):
    """A started watcher over ``first``, with a mock observer and a live task."""
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    observer = MagicMock()
    observer.schedule = MagicMock(return_value=MagicMock())
    monkeypatch.setattr("memtomem.indexing.watcher._create_observer", lambda _cfg: observer)

    engine = SimpleNamespace(index_path=MagicMock(), index_file=MagicMock())
    watcher = FileWatcher(engine, IndexingConfig(memory_dirs=[str(first)]))

    async def _slow_process_events():
        # Long enough that ``stop``'s wait on this task is a real window for a
        # concurrent reconfigure, short enough not to hit its 5s timeout —
        # timing out would cancel the task and make the window fixed-cost.
        await asyncio.sleep(0.05)

    monkeypatch.setattr(watcher, "_process_events", _slow_process_events)
    return watcher, observer, first, second


@pytest.mark.asyncio
async def test_a_reconfigure_during_stop_does_not_schedule_on_a_dying_observer(watched):
    """The race the shared lock exists to close.

    ``stop`` waits on the processor task before clearing the observer, so a
    reconfigure that lands in that window finds a live observer and schedules
    the newly added root onto it. Those watches belong to a watcher that is
    about to be torn down: the observer is stopped and joined moments later,
    and ``_watches`` is cleared, so the reconfigure's work is silently lost
    while its caller — a memory-directory route — reports success.

    Holding one lock across both makes the reconfigure wait, find a stopped
    watcher, and record the config for the next start instead.
    """
    watcher, observer, first, second = watched
    await watcher.start()
    assert observer.schedule.call_count == 1  # the initial root

    stopping = asyncio.create_task(watcher.stop())
    # Let ``stop`` reach its wait on the processor task.
    await asyncio.sleep(0)
    await watcher.reconfigure(IndexingConfig(memory_dirs=[str(first), str(second)]))
    await stopping

    # Nothing was scheduled onto the observer being dismantled.
    assert observer.schedule.call_count == 1
    assert watcher._observer is None
    assert watcher._watches == {}


@pytest.mark.asyncio
async def test_the_config_a_blocked_reconfigure_carried_survives_for_the_next_start(watched):
    """Waiting must not mean losing the change.

    The reconfigure is serialized behind the stop, not dropped: it records the
    new roots on the stopped watcher, so the start an embedding reset performs
    later picks them up. Otherwise closing the race would trade a lost watch
    for a lost directory.
    """
    watcher, observer, first, second = watched
    await watcher.start()

    stopping = asyncio.create_task(watcher.stop())
    await asyncio.sleep(0)
    await watcher.reconfigure(IndexingConfig(memory_dirs=[str(first), str(second)]))
    await stopping

    await watcher.start()
    scheduled = {call.args[1] for call in observer.schedule.call_args_list[1:]}
    assert scheduled == {str(first.resolve()), str(second.resolve())}
    await watcher.stop()


@pytest.mark.asyncio
async def test_a_stop_after_a_failed_observer_start_does_not_raise(tmp_path, monkeypatch):
    """Cleanup must survive the failure it exists to clean up after.

    An observer whose ``start`` failed part-way has stopped emitters but no
    dispatcher thread, and watchdog refuses to join one that never started.
    That ``RuntimeError`` reaches the failed-start cleanup as a stop that
    failed, which bars any further recovery in the process — over a watcher
    that is holding nothing.
    """
    from memtomem.indexing.watcher import WatcherResumer

    root = tmp_path / "root"
    root.mkdir()

    class _NeverStarted:
        """Watchdog's shape for an observer whose start failed."""

        def __init__(self) -> None:
            self.stopped = False

        def schedule(self, *_a, **_kw):
            return MagicMock()

        def start(self):
            raise OSError("inotify instance limit reached")

        def stop(self):
            self.stopped = True

        def is_alive(self):
            return False

        def join(self):  # pragma: no cover - must never be reached
            raise RuntimeError("cannot join thread before it is started")

    observer = _NeverStarted()
    monkeypatch.setattr("memtomem.indexing.watcher._create_observer", lambda _cfg: observer)
    engine = SimpleNamespace(index_path=MagicMock(), index_file=MagicMock())
    watcher = FileWatcher(engine, IndexingConfig(memory_dirs=[str(root)]))

    resumer = WatcherResumer(watcher)
    assert await resumer.resume() is False
    assert observer.stopped is True
    # The instance is clean, so a later reset may try again.
    assert resumer.can_retry is True
    assert watcher._observer is None


@pytest.mark.asyncio
async def test_a_restart_does_not_inherit_the_previous_stop_order(watched):
    """A stale stop sentinel must not kill the next processor on arrival.

    ``stop`` signals the processor through the queue, and a processor that
    misses it — the wait times out and the task is cancelled — leaves the
    sentinel behind. Reusing that queue makes the next start look successful
    while its processor exits immediately, so an embedding reset would report
    file watching back on with nothing draining events. The seeded sentinel
    here is exactly the state a timed-out stop leaves.
    """
    from memtomem.indexing.watcher import _STOP_SENTINEL

    watcher, _observer, first, _second = watched
    await watcher.start()
    await watcher.stop()
    watcher._queue.put_nowait(_STOP_SENTINEL)

    # The real processor, not the fixture's stub: its reaction to the sentinel
    # is the whole subject.
    object.__setattr__(watcher, "_process_events", FileWatcher._process_events.__get__(watcher))
    await watcher.start()
    await asyncio.sleep(0)

    assert watcher._task is not None
    assert not watcher._task.done()
    await watcher.stop()
