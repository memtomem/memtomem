"""Unit tests for the component-generation lease (#2180).

The integration behavior — a revert that defers its close to the last in-flight
search — lives in ``test_server_degraded_mode.py``. These pin the primitive:
who runs the close, how many times, and what a release racing a drain does.
"""

from __future__ import annotations

import asyncio

import pytest

from memtomem.config import Mem2MemConfig
from memtomem.generation import ComponentGeneration
from memtomem.runtime.components import Components, close_components

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _recorder(calls: list[str], name: str = "close"):
    async def _close() -> None:
        calls.append(name)

    return _close


async def test_retire_while_idle_returns_the_close_for_the_caller():
    """Nothing in flight: the caller gets the coroutine and awaits it inline,
    so a swap closes promptly instead of waiting on a release that will never
    come (acceptance criterion 3)."""
    gen = ComponentGeneration()
    calls: list[str] = []

    pending = gen.retire(_recorder(calls))

    assert pending is not None
    assert calls == [], "retire must not run the close itself"
    await pending
    assert calls == ["close"]


async def test_retire_while_leased_defers_to_the_last_release():
    gen = ComponentGeneration()
    calls: list[str] = []

    with gen.hold():
        with gen.hold():
            assert gen.retire(_recorder(calls)) is None
            assert calls == []
        assert calls == [], "close fired while an outer lease was still held"

    await asyncio.sleep(0)
    assert calls == ["close"]


async def test_release_via_exception_still_closes():
    """The release is in a ``finally``: a search that raises must not strand
    the retired generation open until process exit."""
    gen = ComponentGeneration()
    calls: list[str] = []

    with pytest.raises(RuntimeError):
        with gen.hold():
            gen.retire(_recorder(calls))
            raise RuntimeError("search blew up")

    await asyncio.sleep(0)
    assert calls == ["close"]


async def test_a_lease_taken_after_retirement_still_runs_the_close_once():
    """Leases are counted, not gated: a caller already inside ``hold`` when
    the retirement lands keeps the generation alive, and the last one out
    closes it exactly once."""
    gen = ComponentGeneration()
    calls: list[str] = []

    with gen.hold():
        gen.retire(_recorder(calls))
        with gen.hold():
            pass
        assert calls == [], "an inner release closed the generation early"

    await asyncio.sleep(0)
    assert calls == ["close"]


async def test_drain_forces_a_close_nobody_released():
    """Shutdown backstop for a hung or cancelled leaseholder."""
    gen = ComponentGeneration()
    calls: list[str] = []

    lease = gen.hold()
    lease.__enter__()
    gen.retire(_recorder(calls))

    assert await gen.drain() is None
    assert calls == ["close"]

    # The straggler release finds the callback already popped.
    lease.__exit__(None, None, None)
    await asyncio.sleep(0)
    assert calls == ["close"]


async def test_drain_awaits_an_already_scheduled_close():
    gen = ComponentGeneration()
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def _slow_close() -> None:
        started.set()
        await release.wait()
        calls.append("close")

    with gen.hold():
        gen.retire(_slow_close)
    await started.wait()

    drain = asyncio.create_task(gen.drain())
    await asyncio.sleep(0)
    assert calls == [], "drain returned before the deferred close finished"
    release.set()
    assert await drain is None
    assert calls == ["close"]


async def test_drain_reports_a_cancelled_forced_close_instead_of_raising():
    """``close_components`` folds this into its deferred-cancellation
    accumulation — a cancelled close must not abort the rest of shutdown."""
    gen = ComponentGeneration()

    async def _cancelled_close() -> None:
        raise asyncio.CancelledError()

    lease = gen.hold()
    lease.__enter__()
    gen.retire(_cancelled_close)

    cancelled = await gen.drain()
    assert isinstance(cancelled, asyncio.CancelledError)
    lease.__exit__(None, None, None)


async def test_drain_swallows_an_ordinary_close_failure():
    gen = ComponentGeneration()

    async def _failing_close() -> None:
        raise RuntimeError("close failure")

    lease = gen.hold()
    lease.__enter__()
    gen.retire(_failing_close)

    assert await gen.drain() is None
    lease.__exit__(None, None, None)


async def test_drain_on_a_generation_that_was_never_retired_is_a_noop():
    gen = ComponentGeneration()
    assert await gen.drain() is None


async def test_drain_returns_rather_than_propagating_its_own_cancellation():
    """``close_components`` still has the live embedder and the storage to
    close after this call: a cancellation delivered mid-drain has to come back
    as a value (accumulate-and-defer), not unwind the rest of shutdown."""
    gen = ComponentGeneration()
    started = asyncio.Event()

    async def _slow_close() -> None:
        started.set()
        await asyncio.Event().wait()  # never completes

    with gen.hold():
        gen.retire(_slow_close)
    await started.wait()

    drain = asyncio.create_task(gen.drain())
    await asyncio.sleep(0)
    drain.cancel()

    # The task must complete normally with the cancellation as its value.
    assert isinstance(await drain, asyncio.CancelledError)


async def test_component_teardown_observes_shielded_retired_close_after_cancellation():
    gen = ComponentGeneration()
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def _retired_close() -> None:
        started.set()
        await release.wait()
        calls.append("retired")

    class _Resource:
        def __init__(self, name: str, *, releases_retired: bool = False) -> None:
            self.name = name
            self.releases_retired = releases_retired

        async def close(self) -> None:
            calls.append(self.name)
            if self.releases_retired:
                release.set()

    lease = gen.hold()
    lease.__enter__()
    gen.retire(_retired_close)
    components = Components(
        config=Mem2MemConfig(),
        storage=_Resource("storage", releases_retired=True),  # type: ignore[arg-type]
        embedder=_Resource("embedder"),  # type: ignore[arg-type]
        index_engine=object(),  # type: ignore[arg-type]
        search_pipeline=_Resource("pipeline"),  # type: ignore[arg-type]
        retired_generations=[gen],
    )

    teardown = asyncio.create_task(close_components(components))
    await started.wait()
    teardown.cancel()
    result = await teardown

    assert isinstance(result.cancelled, asyncio.CancelledError)
    assert calls == ["pipeline", "embedder", "storage", "retired"]
    assert gen.settled is True
    assert gen._close_task is None
    assert components.retired_generations == []
    lease.__exit__(None, None, None)


async def test_drain_reports_a_cancelled_deferred_close():
    """A deferred close that ends cancelled is reported too, so shutdown can
    re-raise it once every other component is down."""
    gen = ComponentGeneration()

    async def _cancelled_close() -> None:
        raise asyncio.CancelledError()

    with gen.hold():
        gen.retire(_cancelled_close)

    assert isinstance(await gen.drain(), asyncio.CancelledError)


async def test_retire_refuses_a_second_call():
    """Each swap publishes a fresh handle. A second retirement here means a
    caller retired a handle it does not own, and silently overwriting the
    pending callback would leak the generation it belonged to."""
    gen = ComponentGeneration()
    calls: list[str] = []

    with gen.hold():
        gen.retire(_recorder(calls, "first"))
        with pytest.raises(RuntimeError, match="retire called twice"):
            gen.retire(_recorder(calls, "second"))

    await asyncio.sleep(0)
    assert calls == ["first"]


async def test_a_never_retired_generation_is_settled():
    """Nothing to close, so a shutdown drain has nothing to do — the same
    reason ``drain`` on such a handle is already a no-op."""
    assert ComponentGeneration().settled is True


async def test_an_idle_retirement_is_settled_immediately():
    """The idle path hands the close back to the caller and stores nothing,
    so the handle never needs the shutdown backstop (#2201)."""
    gen = ComponentGeneration()
    calls: list[str] = []

    pending = gen.retire(_recorder(calls))

    assert gen.settled is True
    assert pending is not None
    await pending
    assert gen.settled is True


async def test_a_pending_deferred_close_is_not_settled():
    """A leaseholder still holds the generation, so the callback is stored
    and only shutdown may run it — exactly the entry the list exists for."""
    gen = ComponentGeneration()
    calls: list[str] = []

    with gen.hold():
        assert gen.retire(_recorder(calls)) is None
        assert gen.settled is False


async def test_a_running_deferred_close_is_not_settled_until_it_finishes():
    gen = ComponentGeneration()
    release = asyncio.Event()

    async def _slow_close() -> None:
        await release.wait()

    with gen.hold():
        gen.retire(_slow_close)

    await asyncio.sleep(0)
    assert gen.settled is False, "the deferred close task is still running"

    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert gen.settled is True


async def test_a_cancelled_deferred_close_stays_unsettled():
    """Its cancellation is a value ``drain`` still owes ``close_components``
    (see ``test_drain_reports_a_cancelled_deferred_close``), so the handle
    must survive pruning until shutdown reads it."""
    gen = ComponentGeneration()

    async def _cancelled_close() -> None:
        raise asyncio.CancelledError()

    with gen.hold():
        gen.retire(_cancelled_close)

    await asyncio.sleep(0)
    assert gen.settled is False
    assert isinstance(await gen.drain(), asyncio.CancelledError)


async def test_an_ordinary_close_failure_settles():
    """``drain`` only logs and swallows those, so retaining the handle would
    buy nothing."""
    gen = ComponentGeneration()

    async def _failing_close() -> None:
        raise RuntimeError("boom")

    with gen.hold():
        gen.retire(_failing_close)

    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert gen.settled is True


async def test_teardown_gives_up_on_a_generation_that_never_settles():
    """The shutdown drain is bounded, not ``while True``.

    ``drain`` reports a cancellation *without* settling whenever the shielded
    close is still running, so a supervisor that re-delivers cancellation on
    every ``await`` — an expired ``asyncio.timeout`` around shutdown — makes
    every pass raise immediately. An unbounded retry would spin there without
    ever yielding. Cap the passes, keep the cancellation, and leave the close
    running for process exit.
    """
    from memtomem.runtime import components as comp_mod

    class _NeverSettles:
        def __init__(self) -> None:
            self.drains = 0
            self.settled = False

        async def drain(self) -> asyncio.CancelledError | None:
            self.drains += 1
            return asyncio.CancelledError()

    gen = _NeverSettles()
    components = Components(
        config=Mem2MemConfig(),
        storage=None,
        embedder=None,
        index_engine=object(),  # type: ignore[arg-type]
        search_pipeline=None,
        retired_generations=[gen],  # type: ignore[list-item]
    )

    result = await asyncio.wait_for(close_components(components), timeout=5)

    assert isinstance(result.cancelled, asyncio.CancelledError)
    # One pass in the ordering loop, then the capped observation loop.
    assert gen.drains == 1 + comp_mod._MAX_DRAIN_PASSES
    # Unsettled, so it is retained rather than pruned.
    assert components.retired_generations == [gen]
