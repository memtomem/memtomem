"""Surface-neutral helpers for the cross-process memory-CRUD write span (#1587).

The MCP tools (``server/tools/memory_crud``) hold the per-file in-process lock
(L1, ``AppContext.get_memory_file_lock``) plus the cross-process sidecar (L2).
The web routes and CLI reach the same markdown files but have no ``AppContext``,
so they take only L2 — and that is sufficient: ``async_file_lock``'s in-process
guard serializes same-process web handlers (Windows ``LockFileEx`` alone would
not) while the flock serializes across processes. See the lock-ordering
invariant in :mod:`memtomem.context._atomic`.

These helpers give the web/CLI edit paths the same fresh-re-fetch-under-lock and
rollback contract the MCP tools already have, without threading an ``AppContext``
through the web layer.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from memtomem.context import _atomic
from memtomem.context._atomic import _lock_path_for, async_file_lock

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path
    from uuid import UUID

    from memtomem.models import Chunk, IndexingStats

logger = logging.getLogger(__name__)


@asynccontextmanager
async def locked_source_chunk(
    storage,
    chunk_id: UUID,
    *,
    budget: float | None = None,
) -> AsyncIterator[tuple[Chunk | None, str | None]]:
    """Yield ``(chunk, None)`` with the chunk's source-file sidecar (L2) held and
    the chunk re-fetched fresh under it, or ``(None, reason)`` where ``reason`` is
    one of ``"not_found"`` / ``"moved"`` / ``"locked"``.

    Two-step acquire (issue #1587): fetch once *unlocked* to learn the
    ``source_file`` (the lock key), acquire that file's sidecar, then re-fetch
    *under the lock* so ``start_line`` / ``end_line`` and ``scope`` reflect any
    write committed while we waited. Unlike the MCP ``_locked_chunk`` (which
    re-keys onto a moved file and holds the L1 lock too), a file moved by
    ``memory-migrate`` between the two fetches is reported as ``"moved"`` for the
    caller to retry — web/CLI callers re-issue the request rather than looping.
    ``"locked"`` means the sidecar acquire timed out. Exactly one ``yield`` runs
    on every path.
    """
    if budget is None:
        # Resolve at call time so tests can monkeypatch the module constant.
        budget = _atomic._CRUD_SIDECAR_LOCK_BUDGET_S
    chunk = await storage.get_chunk(chunk_id)
    if chunk is None:
        yield None, "not_found"
        return
    resolved = chunk.metadata.source_file.expanduser().resolve()
    # ``acquired`` distinguishes a timeout from the sidecar *acquire* (→ report
    # "locked") from a ``TimeoutError`` raised by the caller's own body after we
    # yielded — that must propagate, not be masked as a lock timeout or trigger
    # a second yield (which would break the @asynccontextmanager protocol).
    acquired = False
    try:
        async with async_file_lock(_lock_path_for(resolved), timeout=budget):
            acquired = True
            fresh = await storage.get_chunk(chunk_id)
            if fresh is None:
                yield None, "not_found"
                return
            if fresh.metadata.source_file.expanduser().resolve() != resolved:
                yield None, "moved"
                return
            yield fresh, None
    except TimeoutError:
        if acquired:
            raise
        yield None, "locked"


async def mutate_source_and_reindex(
    index_engine,
    source_file: Path,
    mutate: Callable[[], None],
) -> IndexingStats:
    """Backup-read → ``mutate`` (in a worker thread) → force re-index with
    ``lock_held=True``, restoring the pre-image and re-raising on failure.

    The caller MUST already hold ``source_file``'s sidecar (via
    :func:`locked_source_chunk`); ``lock_held=True`` skips the nested engine
    acquire that would otherwise self-deadlock. Mirrors the MCP
    ``_mutate_file_and_reindex`` rollback contract, giving the web edit path the
    rollback it previously lacked.
    """
    original = await asyncio.to_thread(source_file.read_text, encoding="utf-8")
    try:
        await asyncio.to_thread(mutate)
        return await index_engine.index_file(
            source_file, force=True, already_scanned=True, lock_held=True
        )
    except Exception:
        await asyncio.to_thread(source_file.write_text, original, encoding="utf-8")
        try:
            await index_engine.index_file(
                source_file, force=True, already_scanned=True, lock_held=True
            )
        except Exception:
            logger.warning("Rollback re-index also failed", exc_info=True)
        raise
