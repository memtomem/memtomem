"""Routes that wait for another task's write transaction, and say so when it runs out (#3182).

While another task in the process owns ``storage.transaction()`` (a policy
job, an import, a consolidation), a writer access refuses at once with
``TransactionOwnedError``. The routes built on these helpers wait for the
writer instead, up to ``WRITER_WAIT_S``. When the wait runs out, they answer
503 with a detail that states the route's post-condition, not the generic
500.

This is per call site, not an app-wide handler (see ``web/app.py``): "nothing
was changed" is true only where the wait comes before every write, and only
the call site knows that.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager

from fastapi import HTTPException

from memtomem.errors import TransactionOwnedError
from memtomem.storage.base import WRITER_WAIT_S

#: The wait ran out before the route wrote anything, or inside a transaction
#: that rolled back.
WRITER_BUSY_NOTHING_CHANGED_DETAIL = (
    "Another write is still in progress, so this request timed out waiting for it; "
    "nothing was changed. Retry."
)
#: Refused at once, without waiting, before the route wrote anything.
WRITER_BUSY_REFUSED_NOTHING_CHANGED_DETAIL = (
    "Another write is in progress; nothing was changed. Retry."
)
#: The wait ran out on a route that only reads.
WRITER_BUSY_READ_DETAIL = (
    "Another write is still in progress, so this request timed out waiting for it. Retry."
)


@contextmanager
def writer_busy_as_503(detail: str) -> Iterator[None]:
    """Answer 503 with ``detail`` when the block's writer wait runs out.

    For blocks whose storage calls wait inside themselves. The caller picks
    ``detail``: :data:`WRITER_BUSY_NOTHING_CHANGED_DETAIL` only where nothing
    in the block has written when the refusal can arrive.
    """
    try:
        yield
    except TransactionOwnedError as exc:
        raise HTTPException(status_code=503, detail=detail) from exc


@asynccontextmanager
async def writer_transaction(storage) -> AsyncIterator[None]:
    """``storage.transaction(wait=WRITER_WAIT_S)`` that answers 503 when the wait runs out.

    The body runs atomically: an exception rolls it back. So a refusal from
    the wait, or from inside the body, leaves nothing changed.
    """
    with writer_busy_as_503(WRITER_BUSY_NOTHING_CHANGED_DETAIL):
        async with storage.transaction(wait=WRITER_WAIT_S):
            yield


async def wait_for_writer(storage, *, detail: str) -> None:
    """Wait until no other task owns the writer, or answer 503 with ``detail``.

    For a synchronous writer access that cannot wait itself, such as the
    ``stored_embedding_info`` property. The access must follow this call with
    no ``await`` in between, or another task may take the writer first.
    A backend without the wait (a test double) is left as it is.
    """
    waiter = getattr(type(storage), "_wait_for_writer_idle", None)
    if waiter is None:
        return
    with writer_busy_as_503(detail):
        await storage._wait_for_writer_idle(wait=WRITER_WAIT_S)
