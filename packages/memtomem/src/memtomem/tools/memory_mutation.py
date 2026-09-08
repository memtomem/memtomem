"""Surface-neutral helpers for the cross-process memory-CRUD write span (#1587).

The MCP tools (``server/tools/memory_crud``) hold the per-file in-process lock
(L1, ``AppContext.get_memory_file_lock``) plus the cross-process sidecar (L2).
The web routes and CLI reach the same markdown files but have no ``AppContext``,
so they take only L2 — and that is sufficient: L2's in-process guard serializes
same-process web handlers (so they queue in memory instead of spending the
flock's timeout budget on each other) while the flock serializes across
processes. That guard is also what remains when the span runs on a source whose
directory has been removed and the sidecar has to be skipped (#2346); see the
lock-ordering invariant in :mod:`memtomem.context._atomic`.

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
from memtomem.context._atomic import async_memory_file_lock
from memtomem.search.visibility import chunk_in_scope_boundary
from memtomem.tools.memory_writer import (
    RestoreOutcome,
    SourceChangedError,
    read_pre_image,
    restore_pre_image_quietly,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path
    from uuid import UUID

    from memtomem.models import Chunk, IndexingStats
    from memtomem.tools.memory_writer import PreImage

logger = logging.getLogger(__name__)


@asynccontextmanager
async def locked_source_chunk(
    storage,
    chunk_id: UUID,
    *,
    project_context_root: Path | None,
    budget: float | None = None,
) -> AsyncIterator[tuple[Chunk | None, str | None, bool]]:
    """Yield ``(chunk, None, cross_process_held)`` with the chunk's source-file
    sidecar (L2) held and the chunk re-fetched fresh under it, or
    ``(None, reason, cross_process_held)`` where ``reason`` is one of
    ``"not_found"`` / ``"moved"`` / ``"locked"``.

    Two-step acquire (issue #1587): fetch once *unlocked* to learn the
    ``source_file`` (the lock key), acquire that file's sidecar, then re-fetch
    *under the lock* so ``start_line`` / ``end_line`` and ``scope`` reflect any
    write committed while we waited. Unlike the MCP ``_locked_chunk`` (which
    re-keys onto a moved file and holds the L1 lock too), a file moved by
    ``memory-migrate`` between the two fetches is reported as ``"moved"`` for the
    caller to retry — web/CLI callers re-issue the request rather than looping.
    ``"locked"`` means the sidecar acquire timed out. Exactly one ``yield`` runs
    on every path.

    The span is ``async_memory_file_lock``, not a bare sidecar acquire: this
    helper is reached for a *delete* as well as an edit, and a chunk can
    outlive the directory its source lived in. Taking the sidecar there would
    ``mkdir`` the removed directory back just to lock a delete (#2346), so on
    a vanished parent the span degrades to L2's in-process layer alone. What a
    caller holds on a successful yield is therefore "the full sidecar, or the
    same-loop guard when the source's directory is gone", and the third element
    says which.

    **A caller that gets ``cross_process_held=False`` must not write bytes to
    the source file.** The degraded span cannot exclude another process, and a
    directory recreated after the degrade can already hold a writer — so an
    edit would splice a file this span never locked, on line numbers taken
    from before it existed. Checking whether the file came back is not a
    substitute: that answer is stale the moment it is read. The bit is not,
    which is why the rule is stated on the bit. Row-level work stays allowed;
    the worst case there is a re-index re-adding what was removed.

    ``project_context_root`` is the caller's ADR-0011 boundary and is required
    — there is no value that safely means "skip the check", so a caller with no
    project context passes ``None`` to say exactly that. The boundary is
    applied at both fetches and the **second** one decides (ADR-0036):
    ``memory-migrate`` can re-scope a chunk while we wait for the sidecar, and
    a check on the discarded probe would be a check on a value the write never
    uses. An out-of-boundary chunk reports ``"not_found"``, the same reason a
    missing id gets, so callers cannot tell them apart.
    """
    if budget is None:
        # Resolve at call time so tests can monkeypatch the module constant.
        budget = _atomic._CRUD_SIDECAR_LOCK_BUDGET_S
    chunk = await storage.get_chunk(chunk_id)
    if chunk is None or not chunk_in_scope_boundary(chunk.metadata, project_context_root):
        yield None, "not_found", False
        return
    resolved = chunk.metadata.source_file.expanduser().resolve()
    # ``acquired`` distinguishes a timeout from the sidecar *acquire* (→ report
    # "locked") from a ``TimeoutError`` raised by the caller's own body after we
    # yielded — that must propagate, not be masked as a lock timeout or trigger
    # a second yield (which would break the @asynccontextmanager protocol).
    acquired = False
    try:
        async with async_memory_file_lock(resolved, timeout=budget) as cross_process_held:
            acquired = True
            fresh = await storage.get_chunk(chunk_id)
            if fresh is None or not chunk_in_scope_boundary(fresh.metadata, project_context_root):
                yield None, "not_found", cross_process_held
                return
            if fresh.metadata.source_file.expanduser().resolve() != resolved:
                yield None, "moved", cross_process_held
                return
            yield fresh, None, cross_process_held
    except TimeoutError:
        if acquired:
            raise
        # Nothing was acquired, so nothing is held — report it as such rather
        # than letting a caller read the default as "you have the flock".
        yield None, "locked", False


async def mutate_source_and_reindex(
    index_engine,
    source_file: Path,
    mutate: Callable[[PreImage], None],
) -> IndexingStats:
    """Backup-read → ``mutate`` (in a worker thread) → incremental re-index with
    ``lock_held=True``, restoring the pre-image and re-raising on failure.

    The caller MUST already hold ``source_file``'s **full** L2 sidecar via
    :func:`locked_source_chunk` — that is, it must have yielded
    ``cross_process_held=True``. This function writes bytes, and #2346's rule
    is that a degraded span does not; the callers refuse before reaching here.
    ``lock_held=True`` skips the nested engine acquire that would otherwise
    self-deadlock. Mirrors the MCP
    ``_mutate_file_and_reindex`` rollback contract, giving the web edit path the
    rollback it previously lacked.

    The pre-image goes back only when the source is still the file it was read
    from: that lock binds cooperating writers, never an external ``rm`` / ``mv``
    / save-via-rename, so an unconditional write would recreate a file somebody
    deleted while this ran (#2347). The restore reports instead of raising, and
    this function always re-raises the *body's* exception — the route
    (``web/routes/chunks.py``) classifies on its type, and a restore's own
    ``ENOENT`` arriving in its place would answer 500 to a transient failure the
    caller should have been told to retry. What the restore did is said in the
    log, where the route's handler already points.

    ``mutate`` receives the pre-image so the write it performs can refuse on the
    same identity the rollback would check (#2367); a mutation that refuses says
    so with a ``SourceChangedError`` and is not rolled back, since it wrote
    nothing to roll back.
    """
    pre_image = await asyncio.to_thread(read_pre_image, source_file)
    mutation_completed = False
    try:
        await asyncio.to_thread(mutate, pre_image)
        mutation_completed = True
        return await index_engine.index_file(source_file, already_scanned=True, lock_held=True)
    except Exception as exc:
        if isinstance(exc, SourceChangedError) and not mutation_completed:
            # The write refused before putting a byte on disk (#2367), so there
            # is no mutation of ours to undo — and restoring anyway would be the
            # very resurrection this refusal exists to prevent: on a filesystem
            # that cannot answer identity the restore proceeds on existence
            # alone, so a file that reappeared in the meantime would be
            # overwritten with the pre-image of a write that never happened.
            # Both halves of the condition are load-bearing. The type says
            # a write refused; the completion flag says it was *this* span's
            # write. This handler also covers the re-index (and, on the MCP
            # twin, the cache and provenance work after it), so a
            # ``SourceChangedError`` surfacing from a later stage names a
            # mutation that already landed and must still be rolled back.
            outcome = exc.outcome
        else:
            outcome = await asyncio.to_thread(restore_pre_image_quietly, source_file, pre_image)
        try:
            await index_engine.index_file(source_file, already_scanned=True, lock_held=True)
        except Exception:
            logger.warning("Rollback re-index also failed", exc_info=True)
        if outcome is RestoreOutcome.failed:
            logger.error(
                "%s: the rollback failed after %s; the file may still hold the partial edit",
                source_file,
                exc,
            )
        elif outcome is not RestoreOutcome.restored:
            logger.warning(
                "%s: not rolled back (%s) — the source was changed by another process while "
                "the edit ran, so nothing was written back over it",
                source_file,
                outcome,
            )
        raise
