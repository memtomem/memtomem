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

Three variants, differing only in what they *raise* versus *hand back*:

===========================  ==================  ====================
variant                      worker failure      cancellation
===========================  ==================  ====================
``settle_shielded``          raises              raises
``settle_shielded_result``   logs, returns None  returns
``settle_shielded_value``    raises              returns (with value)
===========================  ==================  ====================

``settle_shielded_result`` is for callers whose work is advisory — a
failed instance registration degrades a warning, so swallowing it is
correct there. ``settle_shielded_value`` is for **fail-closed** callers
that must still order compensating work before propagating cancellation:
the lifecycle barrier (#1936) has to store a successfully acquired handle
before re-raising a caught cancellation, yet an acquisition *failure*
must reach the caller unchanged — swallowing it would let startup
continue past an active destructive operation.

Private module — not part of the public API surface.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


async def settle_shielded_result(
    future: asyncio.Future, *, what: str
) -> tuple[object | None, asyncio.CancelledError | None]:
    """Like :func:`settle_shielded`, but hand back the outcome instead of raising.

    Returns ``(result, first_cancellation)`` once *future* settles:
    ``result`` is the future's value (``None`` when it failed — the
    failure is logged, never re-raised) and ``first_cancellation`` is the
    first :class:`asyncio.CancelledError` caught while settling, or
    ``None``. Callers that must order compensating work *before*
    propagating cancellation (e.g. instance-registry settlement, which
    may only release its sentinel after storage close is confirmed —
    #1935) use this variant and re-raise the cancellation themselves
    after that work; :func:`settle_shielded` stays the right call when
    re-raise-immediately is the correct order.
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
            # The future settled by failing — captured below via result().
            break
    result: object | None = None
    try:
        result = future.result()
    except BaseException:
        logger.warning("%s failed while settling", what, exc_info=True)
    return result, cancelled


async def settle_shielded_value(
    future: asyncio.Future, *, what: str
) -> tuple[object, asyncio.CancelledError | None]:
    """Like :func:`settle_shielded_result`, but the failure is *raised*.

    Returns ``(result, first_cancellation)`` once *future* settles
    successfully. When the worker failed, the original exception
    propagates unchanged if no cancellation was caught — fail-closed
    callers need the actionable error (a ``PermissionError`` naming the
    unusable runtime dir, say), not ``None``. Once a cancellation has
    been caught, cancellation still wins over the failure, matching the
    other two variants.

    Callers that must act on a *successful* result before propagating
    cancellation (storing an acquired lock handle so it stays releasable
    — #1936) get it back alongside the cancellation and re-raise
    themselves.
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
            # The future settled by failing — re-read below so the
            # cancellation-wins precedence is decided in one place.
            break
    try:
        result = future.result()
    except BaseException:
        if cancelled is None:
            raise
        logger.warning(
            "%s failed while settling a cancellation — cancellation wins", what, exc_info=True
        )
        raise cancelled from None
    return result, cancelled


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
