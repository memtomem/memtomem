"""File helpers a host process may use beside memtomem's own writers.

The implementations live in :mod:`memtomem.context._atomic`, which the rest of
the package imports directly. That module is private by name, so a package
built on the in-process layer (ADR-0042) has no supported way to take the same
sidecar lock or the same atomic replace that memtomem's writers take. This
module is that way: the same objects, re-exported, so a lock acquired here and
one acquired inside memtomem are the same lock.

``norm_path`` is how memtomem spells a source path when it stores one: resolved,
and folded to one Unicode form where the filesystem folds them (macOS). A caller
that compares its own paths with the paths the index returns normalises them the
same way, or an NFD spelling of the same file never matches.

``CRUD_SIDECAR_LOCK_BUDGET_S`` is the timeout memtomem's own memory-file CRUD
paths pass to ``async_file_lock``; a caller that wants to queue behind them
with the same patience uses it too.
"""

from __future__ import annotations

from memtomem.context._atomic import (
    _CRUD_SIDECAR_LOCK_BUDGET_S,
    async_file_lock,
    atomic_write_text,
    memory_lock_path,
)
from memtomem.storage.sqlite_helpers import norm_path

CRUD_SIDECAR_LOCK_BUDGET_S: float = _CRUD_SIDECAR_LOCK_BUDGET_S

__all__ = [
    "CRUD_SIDECAR_LOCK_BUDGET_S",
    "async_file_lock",
    "atomic_write_text",
    "memory_lock_path",
    "norm_path",
]
