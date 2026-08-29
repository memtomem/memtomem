"""Cooperative abort for context-engine work handed to a worker thread.

``asyncio.to_thread`` cannot be cancelled. A web route whose
``asyncio.timeout`` fires returns 503 and the worker it was awaiting keeps
running — reading, merging, and writing the user's files behind a response
that already said the operation failed. The engine-internal sidecar-lock
budgets do not close that window: they bound how long a worker *waits* for a
lock, not what it does once it holds one.

The flag here is how a dispatcher tells its worker to stop. The dispatcher
enters :func:`abandon_sync_on_exit` immediately before the hand-off; the
context manager's ``finally`` sets the event on every exit, which is a no-op
after a normal return (the worker is already done) and the signal that matters
on every abnormal one. ``asyncio.to_thread`` copies the caller's context, and
copying a context copies the *binding* rather than the ``Event``, so the worker
polls the same object the dispatcher sets. Engines call
:func:`sync_is_abandoned` at points they choose.

The abort is **cooperative, not transactional**. Each engine decides where its
checks go, and that choice is a claim about its own transaction boundaries: a
check may sit before a transaction begins, never between the legs of one. An
engine that writes a destination and then cleans a source (``settings_copy``,
``settings_migrate``) would leave an entry in both tiers or neither if it
stopped in between — strictly worse than finishing. Multi-item engines stop
between items; an all-or-nothing batch stops before its promote phase, never
inside it.

Extracted from ``context/settings.py`` (#2218) when the sibling engines adopted
it (#2247); ``settings`` re-exports both names, so existing imports and the
``test_settings_dispatch_pin_guard`` rule are unchanged.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

#: Kept under its original name so a context captured by one release and read
#: by another still matches (the variable is looked up by object identity, but
#: the name is what shows up in debugger dumps and in #2218's prose).
_sync_abandoned: ContextVar[threading.Event | None] = ContextVar(
    "memtomem_settings_sync_abandoned", default=None
)


@contextmanager
def abandon_sync_on_exit() -> Iterator[threading.Event]:
    """Signal a worker thread that the caller which dispatched it is gone (#2218).

    The twin of ``settings.pinned_host_homes``, and entered at the same place —
    immediately before an ``asyncio.to_thread`` hand-off. The pin decides
    *where* a late write lands; this decides whether it happens at all. A
    timed-out request returned 503 and then mutated the user's settings
    seconds later with nothing in the response saying so.

    Setting the event in ``finally`` needs no ``except CancelledError``: on a
    normal exit the worker has already returned, so the set is a no-op, and
    every abnormal exit — the route's ``asyncio.timeout``, an MCP caller's
    cancellation — is exactly the case where the worker is still running and
    must stop. ``asyncio.to_thread`` copies the caller's context, and copying
    a context copies the *binding*, not the ``Event``, so the worker polls the
    same object the caller sets.

    Cooperative by nature: a worker stops at the checkpoints its engine chose,
    so a cancellation landing past the last one still completes that unit of
    work (for settings targets the pin keeps it pointed at the caller's own
    home).
    """
    event = threading.Event()
    token = _sync_abandoned.set(event)
    try:
        yield event
    finally:
        event.set()
        _sync_abandoned.reset(token)


def sync_is_abandoned() -> bool:
    """Whether the caller that dispatched this work has already given up.

    ``False`` when nothing entered :func:`abandon_sync_on_exit`, which keeps
    synchronous callers (CLI, detectors) running exactly as before — only the
    threaded dispatch paths can be abandoned.
    """
    event = _sync_abandoned.get()
    return event is not None and event.is_set()


__all__ = ["abandon_sync_on_exit", "sync_is_abandoned"]
