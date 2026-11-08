"""The ``deadline_ms`` processing budget of one search.

A budget on server processing, not an end-to-end response deadline: it bounds
waits (a read connection, an embedding slice still queued) and is checked at
stage boundaries. Work already running — a SQLite read on its worker thread, an
inference slice that started — completes first. Absent a budget, nothing here
runs: callers hold ``None`` and every helper is skipped.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from memtomem.errors import SearchDeadlineExceeded


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
