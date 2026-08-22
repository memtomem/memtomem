"""Cross-surface coordinator for namespace rename and assignment (#2016).

Namespace SQL changes chunk rows without rewriting their source files, but it
still shares those rows with source-backed CRUD and indexing. Freeze the exact
candidate rows under every relevant source-file L2 sidecar before entering the
raw storage transaction. The storage layer then verifies the snapshot again as
one compare-and-swap operation.
"""

from __future__ import annotations

import stat
import time
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, TypeVar

from memtomem.context._atomic import async_file_lock, memory_lock_path
from memtomem.errors import NamespaceMutationBusyError

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


async def _coordinated_mutation(
    *,
    load_candidates: Callable[[], Awaitable[list[NamespaceChunkCandidate]]],
    apply: Callable[[Sequence[NamespaceChunkCandidate]], Awaitable[_ResultT]],
) -> _ResultT:
    deadline = time.monotonic() + _NAMESPACE_MUTATION_BUDGET_S
    candidates = await load_candidates()

    for _attempt in range(_MAX_RETARGET_ATTEMPTS):
        if deadline - time.monotonic() <= 0:
            break
        planned_lock_paths = {memory_lock_path(src) for src in _lockable_sources(candidates)}
        retarget = False

        try:
            async with AsyncExitStack() as stack:
                for lock_path in sorted(planned_lock_paths, key=str):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    await stack.enter_async_context(async_file_lock(lock_path, timeout=remaining))

                # This is the authoritative snapshot. Candidate changes on
                # already-held sources are safe; a newly seen source requires
                # releasing and acquiring the expanded sorted lock set.
                current = await load_candidates()
                candidates = current
                current_lock_paths = {memory_lock_path(src) for src in _lockable_sources(current)}
                if not current_lock_paths.issubset(planned_lock_paths):
                    retarget = True
                else:
                    try:
                        return await apply(current)
                    except NamespaceMutationBusyError:
                        # An uncoordinated/raw SQL writer may have changed the
                        # rows between the service snapshot and storage CAS.
                        # The raw transaction rolled back, so re-snapshot.
                        retarget = True
        except (TimeoutError, OSError) as exc:
            raise _busy_error() from exc

        if retarget:
            continue

    raise _busy_error()


async def rename_namespace(
    storage: StorageBackend,
    old: str,
    new: str,
    *,
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
    )


async def assign_namespace(
    storage: StorageBackend,
    namespace: str,
    *,
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
    )
