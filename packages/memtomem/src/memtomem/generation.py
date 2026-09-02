"""In-flight lease accounting for one component generation (#2180).

A "generation" is the ``(embedder, search_pipeline, index_engine)`` triple that
``Components`` publishes together. ``mem_embedding_reset(mode="revert_to_stored")``
replaces all three at once and then has to close the retired embedder — but a
search or index run that entered before the swap is still using it, and the ONNX
embedder's close is destructive to in-flight work (it latches ``_closing`` and
calls ``shutdown(cancel_futures=True)`` on its inference executor).

:class:`ComponentGeneration` is the same lease contract
:meth:`memtomem.search.pipeline.SearchPipeline.swap_reranker` uses one level down
(#1777), lifted to the whole triple: users of a generation hold it for the span
of their operation, a swap retires it, and the retired generation is closed on
the last release rather than immediately. With nothing in flight the close still
runs inline at swap time — retirement never waits on a timeout.

This module is deliberately a stdlib-only leaf: ``search.pipeline`` and
``indexing.engine`` import it, and both are imported by
``memtomem.runtime.components``, so anything it pulled in from the runtime layer
would be a cycle.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Coroutine, Iterator
from typing import Any

logger = logging.getLogger(__name__)

CloseCallback = Callable[[], Coroutine[Any, Any, None]]


def _deferred_close_error_cb(task: asyncio.Task) -> None:
    if task.cancelled():
        logger.warning("Deferred close of a retired component generation was cancelled")
        return
    exc = task.exception()
    if exc:
        logger.warning("Deferred close of a retired component generation failed: %s", exc)


@contextlib.contextmanager
def hold_components_generation(components: object) -> Iterator[None]:
    """Pin the generation a ``Components`` publishes, for one span.

    Reads the handle defensively rather than requiring the field: callers hand
    in their own ``Components`` (``from_components``, the CLI, focused tests),
    and a stand-in without one should leave the span unleased instead of
    failing. The ``isinstance`` check is what keeps that tolerance honest — a
    stand-in whose attributes are auto-created cannot pass a stub off as a real
    lease and have the hold silently do nothing.
    """
    generation = getattr(components, "generation", None)
    if not isinstance(generation, ComponentGeneration):
        yield
        return
    with generation.hold():
        yield


@contextlib.contextmanager
def hold_app_generation(app: object) -> Iterator[None]:
    """Pin the generation an ``AppContext`` has published, for one span.

    Tool call sites reach the embedder through ``app.embedder`` and hand it to
    helpers that take an embedder, not a generation — so the lease belongs
    around the *call*, not threaded through the helper (#2199).

    Reads the handle off the app rather than requiring a method on it, so the
    ad-hoc stand-ins tool tests build keep working; see
    :func:`hold_components_generation` for the tolerance rule.
    """
    with hold_components_generation(getattr(app, "_components", None)):
        yield


class ComponentGeneration:
    """Lease counter for one published embedder/pipeline/engine generation.

    Acquire and release are synchronous ops between awaits on one event loop,
    so a plain counter suffices — no lock (the #1777 reranker contract).

    A handle is retired at most once, by the swap that replaces it. The stored
    close callback doubles as the retired flag: popping it before scheduling is
    the exactly-once latch, so a release racing :meth:`drain` cannot close the
    generation twice.
    """

    __slots__ = ("leases", "_retired", "_close_cb", "_close_task")

    def __init__(self) -> None:
        self.leases = 0
        self._retired = False
        self._close_cb: CloseCallback | None = None
        self._close_task: asyncio.Task | None = None

    @property
    def retired(self) -> bool:
        """True once a swap has retired this generation.

        A lease taken after this point is still counted and still honored —
        a caller that read the components before the swap has nothing better
        to run on, and refusing it would turn a narrow embedder race into a
        guaranteed failure. It carries the pre-#2180 exposure: if the close
        already ran, that caller sees a closed embedder.
        """
        return self._retired

    @property
    def settled(self) -> bool:
        """True when :meth:`drain` has nothing left to do for this handle.

        Settled means no close callback is still pending *and* no deferred
        close is still running — so a shutdown drain would be a no-op and the
        handle can be dropped from ``Components.retired_generations`` (#2201).
        Leases are deliberately not part of it: a leaseholder that never
        releases leaves the callback pending, which is exactly the case the
        shutdown backstop exists for.

        A finished-but-*cancelled* deferred close is **not** settled. Its
        cancellation is a value :meth:`drain` still has to report, so that
        ``close_components`` can re-raise it after every other component is
        down (the accumulate-and-defer contract). Dropping the handle would
        silently swallow it.
        """
        if self._close_cb is not None:
            return False
        task = self._close_task
        return task is None or (task.done() and not task.cancelled())

    @contextlib.contextmanager
    def hold(self) -> Iterator[None]:
        """Pin this generation for the duration of one operation.

        The last release of a retired generation runs its close as a background
        task, so the unlucky last caller doesn't pay the close latency — the
        same reason ``_lease_reranker`` defers. ``drain`` awaits that task.
        """
        self.leases += 1
        try:
            yield
        finally:
            self.leases -= 1
            if self.leases == 0 and self._close_cb is not None:
                cb, self._close_cb = self._close_cb, None
                self._close_task = asyncio.create_task(cb())
                self._close_task.add_done_callback(_deferred_close_error_cb)

    def retire(self, close_cb: CloseCallback) -> Coroutine[Any, Any, None] | None:
        """Retire this generation, returning the close for the caller to await.

        Idle: returns the *coroutine* ``close_cb()`` produced right here, so the
        swap closes promptly and inline — a failure (including cancellation)
        surfaces to the caller exactly as a direct ``await close()`` would.

        Leased: stores the callback and returns ``None``; the last
        :meth:`hold` release runs it. The caller awaits nothing, so a swap
        never blocks on in-flight work.

        Single-shot per handle, enforced rather than assumed: every swap
        publishes a fresh generation, so a second ``retire`` here means a
        caller retired a handle it does not own — and silently overwriting a
        pending callback would leak the generation it belonged to.
        """
        if self._retired:
            raise RuntimeError(
                "ComponentGeneration.retire called twice — each swap must publish "
                "a fresh generation (the swap_reranker contract, #1777)"
            )
        self._retired = True
        if self.leases == 0:
            return close_cb()
        self._close_cb = close_cb
        return None

    async def drain(self) -> asyncio.CancelledError | None:
        """Shutdown backstop: close a retired generation nobody released.

        Pops the pending callback first, so a release landing after this point
        finds nothing to schedule, then awaits an already-scheduled deferred
        close. This is the one path that closes a generation while leases are
        still outstanding — at shutdown a hung or cancelled leaseholder is
        never coming back, and holding the ONNX session for it would outlive
        the process's usefulness.

        Never raises: every outcome — an ordinary failure, a cancellation
        raised by the close, a cancellation delivered to this coroutine — is
        turned into a return value, because ``close_components`` still has the
        live embedder and the storage to close after this. A caught
        :class:`asyncio.CancelledError` comes back for the caller to defer and
        re-raise (the accumulate-and-defer teardown contract, #1935/#2176);
        ordinary failures are logged and swallowed.
        """
        cb, self._close_cb = self._close_cb, None
        task = self._close_task
        if cb is not None:
            # Publish the task before awaiting it.  If shutdown itself is
            # cancelled, ``shield`` leaves the close running and a later
            # drain can observe the same task instead of starting the close
            # twice.  Unlike executor settlement, this coroutine must return
            # the cancellation promptly: the rest of component teardown is
            # still waiting behind it and may itself release what the close
            # needs in order to finish.
            task = asyncio.create_task(cb())
            self._close_task = task
        if task is None:
            return None

        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            if not task.done():
                return exc
            # A close which cancelled itself has settled; consume that
            # outcome and report it to the aggregate shutdown caller.
            if self._close_task is task:
                self._close_task = None
            return exc
        except BaseException:
            if self._close_task is task:
                self._close_task = None
            logger.warning("Retired component generation close failed", exc_info=True)
            return None

        if self._close_task is task:
            self._close_task = None
        return None
