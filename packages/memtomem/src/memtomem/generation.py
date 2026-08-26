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
        task, self._close_task = self._close_task, None
        cancelled: asyncio.CancelledError | None = None
        if cb is not None:
            try:
                await cb()
            except asyncio.CancelledError as exc:
                cancelled = exc
            except Exception:
                logger.warning(
                    "Failed to close a retired component generation at shutdown; "
                    "its resources are leaked until the process exits",
                    exc_info=True,
                )
        if task is not None:
            try:
                results = await asyncio.gather(task, return_exceptions=True)
            except asyncio.CancelledError as exc:
                # Delivered to *this* coroutine mid-gather. The deferred close
                # keeps running as its own task; report the cancellation
                # instead of propagating it into the rest of the shutdown.
                return cancelled or exc
            if cancelled is None:
                cancelled = next(
                    (r for r in results if isinstance(r, asyncio.CancelledError)), None
                )
        return cancelled
