"""Watching a call queue on the writer lock while another task owns it.

The writer-wait tests start a call, wait until it is queued on the store's
writer lock behind an owner, release the owner, and check the call's outcome.
:func:`until_queued` is that wait. It returns what it saw on its last look, so
a failure names which of these happened:

- the call finished before a writer wait was observed;
- the ceiling ran out with the call still running, which says nothing about
  the call: a slow runner looks the same (#3451).

The owner is any object with ``task`` (``None`` until it is started) and
``entered`` (an ``asyncio.Event`` set once it holds the writer).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

# How long a watched call may take to reach the writer, to be collected after
# the owner is released, or an owner to take the writer. This only stops a hung
# test: running out of it is reported as "could not tell", never as a refusal.
CEILING_S = 60.0

_POLL_S = 0.005


def _now() -> float:
    """The clock the ceiling is measured on. The harness's own test drives it."""
    return asyncio.get_running_loop().time()


def queued_on_writer(storage) -> bool:
    """Whether a task is queued on the writer lock (test-only peek at asyncio)."""
    return any(not w.cancelled() for w in (storage._writer_lock._waiters or ()))


@dataclass(frozen=True, repr=False)
class Queued:
    """One look at the overlap. True when the call was queued behind the owner.

    Its text is the diagnosis, so a bare ``assert queued`` reports it.
    """

    started: bool
    entered: bool
    waiting: bool
    done: bool
    waited_s: float
    ceiling_s: float

    def __bool__(self) -> bool:
        return self.entered and self.waiting and not self.done

    def __repr__(self) -> str:
        seen = (
            f"owner started={self.started}, entered={self.entered}, waiter visible={self.waiting}"
        )
        if self:
            return f"queued on the writer after {self.waited_s:.2f}s ({seen})"
        if self.done:
            return (
                f"the call finished before a writer wait was observed, "
                f"after {self.waited_s:.2f}s ({seen})"
            )
        return (
            f"could not tell: gave up after {self.waited_s:.2f}s (ceiling {self.ceiling_s:g}s) "
            f"with the call still running and no wait behind the owner observed ({seen})"
        )


async def until_queued(storage, owner, task, *, ceiling_s: float = CEILING_S) -> Queued:
    """Look until ``task`` is queued behind ``owner``, is done, or the ceiling runs out."""
    began = _now()
    while True:
        seen = Queued(
            started=owner.task is not None,
            entered=owner.entered.is_set(),
            waiting=queued_on_writer(storage),
            done=task.done(),
            waited_s=_now() - began,
            ceiling_s=ceiling_s,
        )
        if seen or seen.done or seen.waited_s > ceiling_s:
            return seen
        await asyncio.sleep(_POLL_S)
