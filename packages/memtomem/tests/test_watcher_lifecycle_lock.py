"""One lock over every change to the watcher's observer/handler/task triple.

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
