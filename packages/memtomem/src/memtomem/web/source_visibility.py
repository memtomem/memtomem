"""What ``GET /api/sources`` lists, as one rule for every Sources surface.

Two rules hide a source from the Sources tree: it is held or pending
(:func:`~memtomem.storage.sqlite_visibility.hidden_source_paths`, #2498), or
its canonical-residency tier is filtered out (ADR-0015 §4a: ``project_local``
is hidden unless the caller asks for it by ``target_scope``). The list route,
the content-match route and the per-root counts in
``/api/memory-dirs/status`` all ask :meth:`SourceVisibility.check`, so their
visibility rules cannot drift apart again (#2561, #2567). This does not cover
paging: the tree fetches at most 10,000 rows, and the counts do not.

The two rules overlap: a held ``project_local`` draft is hidden for both
reasons. ``check`` therefore returns the *set* of reasons rather than one
reason picked by precedence, so a count per reason stays the whole of that
reason and the union is still counted once.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from memtomem.config import classify_scope
from memtomem.storage.base import StorageBackend
from memtomem.storage.sqlite_visibility import hidden_source_paths

HiddenReason = Literal["held", "scope"]

HELD: HiddenReason = "held"
SCOPE: HiddenReason = "scope"


@dataclass(frozen=True)
class SourceVisibility:
    """Per-request visibility rule. Build it with :func:`load_source_visibility`."""

    held: frozenset[str]
    project_memory_dirs: tuple[Path | str, ...]

    def check(
        self, path: Path | str, target_scope: str | None = None
    ) -> tuple[frozenset[HiddenReason], str]:
        """Return ``(reasons, scope)`` for one source path.

        ``reasons`` is empty when the source is listed. ``"held"``: the source
        is held or pending, or summarizes such a source. ``"scope"``: its tier
        is filtered out — by default ``project_local``; with an explicit
        ``target_scope``, every tier but that one. ``scope`` is the path's
        tier from :func:`~memtomem.config.classify_scope`, which the list
        route reports as ``SourceOut.target_scope``.
        """
        source_scope, _project_root = classify_scope(path, self.project_memory_dirs)
        reasons: set[HiddenReason] = set()
        if str(path) in self.held:
            reasons.add(HELD)
        if target_scope is None:
            if source_scope == "project_local":
                reasons.add(SCOPE)
        elif source_scope != target_scope:
            reasons.add(SCOPE)
        return frozenset(reasons), source_scope


async def load_source_visibility(storage: StorageBackend, config) -> SourceVisibility:
    """Read the held and pending set once and pin this request's config."""
    held = frozenset(str(path) for path in await hidden_source_paths(storage))
    return SourceVisibility(
        held=held,
        project_memory_dirs=tuple(config.indexing.project_memory_dirs),
    )
