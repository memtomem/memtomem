"""Helpers for background tasks that must not die quietly.

Two failure modes this module exists to prevent:

* a long-lived supervisory loop (health watchdog, schedulers) raising out of its
  loop body, which kills monitoring with no log and no restart — the only trace
  is ``get_status()["running"]`` flipping to ``False``;
* a fire-and-forget task with no strong reference anywhere, which the event loop
  is free to garbage-collect mid-flight.

``track_task`` covers both: it logs the death and keeps the handle alive until
the task finishes. ``tests/test_create_task_hygiene_guard.py`` enforces that
every ``create_task`` under ``server/`` and ``web/`` uses one of these shapes.
"""

from __future__ import annotations

import asyncio
import logging

from memtomem._settlement import settle_shielded_result

logger = logging.getLogger(__name__)


def loop_task_error_cb(task: asyncio.Task) -> None:
    """Log a supervisory loop that exited on an exception.

    Logged at ``error``: unlike a one-shot background task, a dead loop means
    the whole periodic service is gone for the life of the process.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Background loop %s died: %s", task.get_name(), exc, exc_info=exc)


def bg_task_error_cb(task: asyncio.Task) -> None:
    """Log a one-shot fire-and-forget task that failed."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("Background task %s failed: %s", task.get_name(), exc)


async def stop_loop_task(task: asyncio.Task) -> None:
    """Cancel and settle a supervisory loop, tolerating one that already died.

    Two cancellations can arrive here and they mean opposite things:

    * the *child's* — the one this helper just requested. Expected, and
      swallowed: the loop stopping is the whole point of the call.
    * the *caller's* — shutdown itself being cancelled mid-teardown. This one
      must propagate: ``AppContext.close`` re-raises ``CancelledError`` through
      ``_stop_quietly`` on purpose, and swallowing it lets a teardown that was
      asked to stop keep working through the rest of its ordering.

    The exception instance cannot tell them apart, and neither can
    ``current_task().cancelling()``: a cancellation *requested before* this
    helper was entered is already counted at entry, so the delivered
    ``CancelledError`` would read as the child's and be swallowed. So the
    child's outcome is turned into a plain value instead — ``gather(...,
    return_exceptions=True)`` hands its ``CancelledError`` back as a result —
    and any ``CancelledError`` still raised out of the await is, unambiguously,
    the caller's.

    That normalization makes :func:`memtomem._settlement.settle_shielded_result`
    directly reusable, so this shares the settlement contract rather than
    redoing it: the await is shielded, so the loop's own cleanup always runs to
    completion before anything propagates, and the *first* caller cancellation
    is re-raised as the same instance, so a ``cancel(msg)`` message survives.

    A loop that raised is already done, so its exception comes back as the
    gathered result. ``loop_task_error_cb`` has logged it at error level
    already; shutdown has nothing left to do with it.
    """
    task.cancel()
    settled = asyncio.gather(task, return_exceptions=True)
    results, cancelled = await settle_shielded_result(settled, what="background loop stop")
    for outcome in results if isinstance(results, list) else []:
        if isinstance(outcome, BaseException) and not isinstance(outcome, asyncio.CancelledError):
            logger.debug("Background loop %s had already died: %s", task.get_name(), outcome)
    if cancelled is not None:
        raise cancelled


def track_task(task: asyncio.Task, tasks: set[asyncio.Task]) -> asyncio.Task:
    """Hold a strong reference to ``task`` until it finishes, logging failures.

    The ``add``/``discard`` pair is the standard cure for the asyncio
    garbage-collection hazard: ``create_task`` only keeps a weak reference, so
    a task nobody holds can vanish mid-await.
    """
    tasks.add(task)
    task.add_done_callback(bg_task_error_cb)
    task.add_done_callback(tasks.discard)
    return task
