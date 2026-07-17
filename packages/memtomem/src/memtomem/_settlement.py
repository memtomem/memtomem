"""Shared shielded settlement for executor-backed awaits (#1803, #1806).

A worker thread can't be interrupted — cancelling only the awaiting task
would either drop sync work that is still queued in the executor or leave
it running while shutdown tears components down under it. The two
executor-backed settle paths (``server/warmup.py`` model loads and
``OnnxEmbedder.close`` teardown) await through :func:`settle_shielded`
so they share one contract instead of drifting apart:

* Every settle-await is shielded — repeated cancellation of the awaiting
  task can never cancel executor work, queued or running.
* After the work settles, the *first* caught cancellation is re-raised as
  the same exception instance, so a ``task.cancel(msg)`` message survives
  settlement and later cancellations don't overwrite it.
* Executor-failure precedence is explicit: with no cancellation pending,
  the failure propagates to the caller unchanged. Once a cancellation has
  been caught, cancellation wins — the failure is logged (never silently
  dropped) and the cancellation is re-raised, because shutdown paths rely
  on observing ``CancelledError`` (e.g. ``AppContext.close`` awaits the
  cancelled warmup task) and a surprise teardown exception there would
  masquerade as a crash.

Private module — not part of the public API surface.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


async def settle_shielded(future: asyncio.Future, *, what: str) -> None:
    """Await *future* until it settles, surviving repeated cancellation.

    Returns normally when the future succeeds with no cancellation caught.
    Re-raises the first caught :class:`asyncio.CancelledError` (message
    preserved) once the future is done, or the future's own failure when
    no cancellation was caught. *what* names the work in the log line
    emitted when cancellation suppresses a failure.
    """
    cancelled: asyncio.CancelledError | None = None
    while True:
        try:
            await asyncio.shield(future)
            break
        except asyncio.CancelledError as exc:
            if cancelled is None:
                cancelled = exc
            if future.done():
                break
        except BaseException:
            if cancelled is not None:
                break
            raise
    if cancelled is not None:
        try:
            future.result()
        except BaseException:
            logger.warning(
                "%s failed while settling a cancellation — cancellation wins", what, exc_info=True
            )
        raise cancelled
