"""Cross-surface coordinator for namespace rename and assignment (#2016).

Namespace SQL changes chunk rows without rewriting their source files, but it
still shares those rows with source-backed CRUD and indexing. Freeze the exact
candidate rows under every relevant source-file L2 sidecar before entering the
raw storage transaction. The storage layer then verifies the snapshot again as
one compare-and-swap operation.

The rows can name any file ``index_file`` was ever given, so a source may lie
outside every directory memtomem keeps memory files in. Such a source gets no
sidecar created beside it: the caller's ``is_managed_source`` says which ones,
and the lock-order block in ``context/_atomic.py`` says what is taken instead.
"""

from __future__ import annotations

import stat
import time
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, TypeVar

from memtomem.context._atomic import (
    async_detached_source_lock,
    async_existing_memory_file_lock,
    async_file_lock,
    detached_source_lock_key,
    memory_lock_path,
)
from memtomem.errors import NamespaceMutationBusyError, TransactionOwnedError

if TYPE_CHECKING:
    from pathlib import Path

    from memtomem.storage.base import (
        NamespaceAssignResult,
        NamespaceChunkCandidate,
        NamespaceRenameResult,
        StorageBackend,
    )

_NAMESPACE_MUTATION_BUDGET_S = 30.0
_MAX_RETARGET_ATTEMPTS = 3

_ResultT = TypeVar("_ResultT")


def _busy_error() -> NamespaceMutationBusyError:
    return NamespaceMutationBusyError(
        "Namespace mutation could not freeze a stable source snapshot within "
        f"{_NAMESPACE_MUTATION_BUDGET_S:g}s and {_MAX_RETARGET_ATTEMPTS} attempt(s); "
        "nothing was changed in this operation. Retry."
    )


def _lockable_sources(candidates: Sequence[NamespaceChunkCandidate]) -> set[Path]:
    """Resolved source paths that may be locked, skipping deleted parent trees.

    Returns the FILES, not their sidecars: the keys are built by
    :func:`_coordinated_mutation` with ``memory_lock_path`` so the builder and
    the acquire live in one function. ``test_context_c0_prelude_guard`` derives
    lock paths by intra-function taint, and a key crossing this helper boundary
    made the acquire below invisible to it (#2130).

    The paths are resolved, so two of them never share a sidecar.
    """
    lockable: set[Path] = set()
    for candidate in candidates:
        source = candidate.source_file.expanduser().resolve(strict=False)
        try:
            parent_stat = source.parent.stat()
        except (FileNotFoundError, NotADirectoryError):
            # Issue #1566: creating a sidecar must never recreate a directory
            # tree that another process deliberately removed.
            continue
        except OSError as exc:
            raise _busy_error() from exc
        if not stat.S_ISDIR(parent_stat.st_mode):
            continue
        lockable.add(source)
    return lockable


def _sidecar_order(source: Path) -> str:
    """Sort key for the adjacent pass: the sidecar's path, as every multi-acquirer sorts.

    A function of its own because the loop that uses it hands ``source`` to a
    data-path acquire, and ``test_context_c0_prelude_guard`` reads a name
    derived from ``memory_lock_path`` inside the acquiring function as a lock
    path.
    """
    return str(memory_lock_path(source))


async def _coordinated_mutation(
    *,
    load_candidates: Callable[[], Awaitable[list[NamespaceChunkCandidate]]],
    apply: Callable[[Sequence[NamespaceChunkCandidate]], Awaitable[_ResultT]],
    is_managed_source: Callable[[Path], bool],
) -> _ResultT:
    """Load, lock and apply one namespace mutation; busy if it cannot settle.

    The locks are taken in two passes under the one deadline. First the
    detached lock of every source whose directory ``is_managed_source`` answers
    no for, each key once (two sources can share a key, and the lock is not
    reentrant).
    Then the adjacent sidecars of all sources in the order every multi-acquirer
    of them uses, created for a managed source and taken only if already there
    for the others.

    Both candidate loads wait for another task's write transaction inside
    storage (#3182). The reload under the file locks is followed by ``apply``
    with no ``await`` before its write, so that wait covers the write too. A
    wait that runs out is reported as ``NamespaceMutationBusyError``, like a
    lock that stays busy: it comes before anything is written.
    """
    deadline = time.monotonic() + _NAMESPACE_MUTATION_BUDGET_S
    try:
        candidates = await load_candidates()
    except TransactionOwnedError as exc:
        raise _busy_error() from exc

    for _attempt in range(_MAX_RETARGET_ATTEMPTS):
        if deadline - time.monotonic() <= 0:
            break
        planned_sources = _lockable_sources(candidates)
        # Asked per directory: the sources are resolved, a file is under a root
        # exactly when its directory is the root or under it, and the predicate
        # resolves every configured root on each call.
        managed_dirs = {
            parent: is_managed_source(parent) for parent in {s.parent for s in planned_sources}
        }
        unmanaged = {src for src in planned_sources if not managed_dirs[src.parent]}
        detached = {detached_source_lock_key(src): src for src in sorted(unmanaged, key=str)}
        retarget = False

        try:
            async with AsyncExitStack() as stack:
                for key in sorted(detached):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    await stack.enter_async_context(
                        async_detached_source_lock(detached[key], timeout=remaining)
                    )
                for src in sorted(planned_sources, key=_sidecar_order):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    if src in unmanaged:
                        await stack.enter_async_context(
                            async_existing_memory_file_lock(src, timeout=remaining)
                        )
                    else:
                        await stack.enter_async_context(
                            async_file_lock(memory_lock_path(src), timeout=remaining)
                        )

                # This is the authoritative snapshot. Candidate changes on
                # already-held sources are safe; a newly seen source requires
                # releasing and acquiring the expanded sorted lock set.
                current = await load_candidates()
                candidates = current
                if not _lockable_sources(current).issubset(planned_sources):
                    retarget = True
                else:
                    try:
                        return await apply(current)
                    except NamespaceMutationBusyError:
                        # An uncoordinated/raw SQL writer may have changed the
                        # rows between the service snapshot and storage CAS.
                        # The raw transaction rolled back, so re-snapshot.
                        retarget = True
        except (TimeoutError, OSError, TransactionOwnedError) as exc:
            raise _busy_error() from exc

        if retarget:
            continue

    raise _busy_error()


async def rename_namespace(
    storage: StorageBackend,
    old: str,
    new: str,
    *,
    is_managed_source: Callable[[Path], bool],
    merge: bool = False,
) -> NamespaceRenameResult:
    async def load() -> list[NamespaceChunkCandidate]:
        return await storage.list_namespace_chunk_candidates(namespace=old)

    async def apply(
        candidates: Sequence[NamespaceChunkCandidate],
    ) -> NamespaceRenameResult:
        return await storage.rename_namespace(
            old,
            new,
            merge=merge,
            candidates=candidates,
        )

    return await _coordinated_mutation(
        load_candidates=load,
        apply=apply,
        is_managed_source=is_managed_source,
    )


async def assign_namespace(
    storage: StorageBackend,
    namespace: str,
    *,
    is_managed_source: Callable[[Path], bool],
    source_filter: str | None = None,
    old_namespace: str | None = None,
    merge: bool = False,
) -> NamespaceAssignResult:
    async def load() -> list[NamespaceChunkCandidate]:
        return await storage.list_namespace_chunk_candidates(
            source_filter=source_filter,
            namespace=old_namespace,
            exclude_namespace=namespace,
        )

    async def apply(
        candidates: Sequence[NamespaceChunkCandidate],
    ) -> NamespaceAssignResult:
        return await storage.assign_namespace(
            namespace,
            source_filter=source_filter,
            old_namespace=old_namespace,
            merge=merge,
            candidates=candidates,
        )

    return await _coordinated_mutation(
        load_candidates=load,
        apply=apply,
        is_managed_source=is_managed_source,
    )
