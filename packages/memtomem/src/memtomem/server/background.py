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
    """Cancel and await a supervisory loop, tolerating one that already died.

    A loop that raised is already done, so ``await`` re-raises its exception
    into the caller's shutdown path. ``loop_task_error_cb`` has logged it
    already; shutdown has nothing left to do with it.
    """
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        pass  # already surfaced by ``loop_task_error_cb``


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
