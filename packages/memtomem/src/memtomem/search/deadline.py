"""The ``deadline_ms`` processing budget of one search.

A budget on server processing, not an end-to-end response deadline: it bounds
waits (a read connection, an embedding slice still queued) and is checked at
stage boundaries. Work already running — a SQLite read on its worker thread, an
inference slice that started — completes first. Absent a budget, nothing here
runs: callers hold ``None`` and every helper is skipped.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import time
from collections.abc import Iterator
from dataclasses import dataclass

from memtomem.errors import SearchDeadlineExceeded

# ``(task, deadline)`` of the budgeted search running in that task. ``_lease_read_db`` applies it
# only when the lease is opened by that same task: a background task started from the search
# (an access-count increment, a persisted observation) copies this context but is another task,
# so its reads are never cut short by a budget that is not theirs.
_SEARCH_READ_DEADLINE: contextvars.ContextVar[tuple[object, float] | None] = contextvars.ContextVar(
    "memtomem_search_read_deadline", default=None
)


def ambient_read_deadline() -> float | None:
    """The budget of the search running in the current task, if it has one."""
    bound = _SEARCH_READ_DEADLINE.get()
    if bound is None:
        return None
    try:
        task = asyncio.current_task()
    except RuntimeError:
        return None
    return bound[1] if task is not None and task is bound[0] else None


@contextlib.contextmanager
def bound_to_current_task(deadline: SearchDeadline | None) -> Iterator[None]:
    """Make ``deadline`` the ambient read budget of the current task for the block."""
    if deadline is None:
        yield
        return
    token = _SEARCH_READ_DEADLINE.set((asyncio.current_task(), deadline.at))
    try:
        yield
    finally:
        _SEARCH_READ_DEADLINE.reset(token)


@dataclass(frozen=True, slots=True)
class SearchDeadline:
    """``at`` is a ``time.monotonic()`` instant; ``budget_ms`` is what was asked."""

    at: float
    budget_ms: int

    @classmethod
    def from_ms(cls, budget_ms: int | None) -> SearchDeadline | None:
        if budget_ms is None:
            return None
        return cls(time.monotonic() + budget_ms / 1000, budget_ms)

    def remaining(self) -> float:
        return self.at - time.monotonic()

    def exceeded(self, stage: str) -> SearchDeadlineExceeded:
        return SearchDeadlineExceeded(
            f"deadline of {self.budget_ms} ms exceeded at {stage}; no results were returned"
        )

    def check(self, stage: str) -> None:
        if self.remaining() <= 0:
            raise self.exceeded(stage)


def check(deadline: SearchDeadline | None, stage: str) -> None:
    """``deadline.check(stage)``, or nothing without a budget."""
    if deadline is not None:
        deadline.check(stage)
