"""Which chunks a caller may see, applied outside the retrieval SQL (#2192).

The retrievers enforce namespace visibility, the ADR-0011 project boundary,
and temporal validity in SQL, so every *hit* already respects them. Chunks
reach a caller two other ways, and this module is the one place the rules are
re-stated for both, so the surfaces cannot drift apart:

- **By adjacency** — the context-window neighbours of a hit, and
  ``mem_expand``'s window. Read in bulk by source file, so no filtered query
  ever touches them. :func:`neighbor_visible` (#2192, #2233).
- **By id** — ``mem_read`` and every other surface that takes a chunk id.
  :func:`resolve_visible_chunk` (ADR-0036, #2238).

The two get different rules, and the difference is the point. Adjacency
inherits all three axes: a neighbour arrived because something near it
matched, so the defaults that shaped the search still apply. An id carries no
query to inherit from, and the caller named the chunk, so only the ADR-0011
boundary survives there — the other two axes are relevance defaults an
explicit ``namespace=`` already lifts, and a rule a caller can switch off is
not one to enforce against them.

Within adjacency the split is between *visibility* and *selection*. Visibility
filters say what the caller may see and apply to neighbours; selection filters
(chunk types, tags, created-date bounds) say what the caller searched for, and
a neighbour is deliberately exempt from them — "show me what surrounds this
match" is not a claim that the surroundings also match.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol
from uuid import UUID

from memtomem.models import (
    Chunk,
    ChunkMetadata,
    NamespaceFilter,
    ScopeFilter,
    has_namespace_prefix,
)


class _ChunkSource(Protocol):
    """The one storage method :func:`resolve_visible_chunk` needs."""

    async def get_chunk(self, chunk_id: UUID) -> Chunk | None: ...


def chunk_valid_at(metadata: ChunkMetadata, as_of_unix: int) -> bool:
    """Whether a chunk's temporal-validity window covers ``as_of_unix``.

    Inclusive on both ends (``valid_from <= as_of <= valid_to``); ``None`` on
    a bound means unbounded on that side, and both ``None`` means always-valid
    (RFC §Comparison semantics — the opt-in default for chunks without a
    window).
    """
    vfrom = metadata.valid_from_unix
    vto = metadata.valid_to_unix
    if vfrom is None and vto is None:
        return True
    lower = vfrom if vfrom is not None else float("-inf")
    upper = vto if vto is not None else float("inf")
    return lower <= as_of_unix <= upper


def neighbor_visible(
    chunk: Chunk,
    *,
    ns_filter: NamespaceFilter | None,
    system_prefixes: tuple[str, ...],
    scope_filter: ScopeFilter | None,
    project_context_root: Path | None,
    as_of_unix: int | None,
) -> bool:
    """Whether an adjacency-reached chunk may be shown to this caller.

    An explicit ``namespace=`` / ``scope=`` *widens* neighbour visibility and
    never narrows it: asking for ``archive:x`` reveals ``archive:x``
    neighbours, it does not hide the ordinary chunks surrounding the match.
    That asymmetry is what makes this a visibility rule rather than a second
    copy of the selection query.
    """
    meta = chunk.metadata
    is_system = has_namespace_prefix(meta.namespace, system_prefixes)
    if is_system and not (ns_filter is not None and ns_filter.matches(meta.namespace)):
        return False

    if not _scope_visible(meta, scope_filter, project_context_root):
        return False

    return as_of_unix is None or chunk_valid_at(meta, as_of_unix)


def chunk_in_scope_boundary(
    meta: ChunkMetadata,
    project_context_root: Path | None,
) -> bool:
    """Whether a chunk is inside the caller's ADR-0011 project boundary.

    The Python twin of ``scope_context_sql``'s no-filter fragment: inside
    project ``<X>``, ``scope = 'user' OR project_root = <X>``; outside one,
    ``scope = 'user'``. No explicit filter widens it — this is the boundary
    itself, not the widened form :func:`_scope_visible` builds on top.

    ADR-0036 makes this the rule for chunks reached by **id**, where there is
    no filter to widen with: an id outside the boundary resolves exactly as a
    nonexistent one. Note what it deliberately does not test — system
    namespaces and temporal validity. Those are retrieval-relevance defaults
    an explicit ``namespace=`` already lifts and one config line switches off,
    so id-addressed access reads an ``archive:*`` or expired chunk on purpose.
    """
    return meta.scope == "user" or (
        project_context_root is not None
        and meta.project_root is not None
        and str(meta.project_root) == str(project_context_root)
    )


async def resolve_visible_chunk(
    storage: _ChunkSource,
    chunk_id: UUID,
    *,
    project_context_root: Path | None,
) -> Chunk | None:
    """Fetch a chunk by id, or ``None`` if it is outside the caller's boundary.

    The single resolver for ADR-0036's rule on the id-addressed surface. The
    two answers are deliberately indistinguishable: a caller cannot tell an id
    that does not exist from one belonging to another project, so callers must
    render both with the same message. Ids address rows; they do not certify
    that the holder may read one.

    ``storage`` is duck-typed to whatever provides ``get_chunk`` so this module
    stays free of a storage import, and so the mutation paths can screen the
    chunk they re-fetch **under the lock** rather than only the probe before
    it — a scope change that lands while the lock is held has to be judged on
    the value that will actually be written.
    """
    chunk = await storage.get_chunk(chunk_id)
    if chunk is None:
        return None
    if not chunk_in_scope_boundary(chunk.metadata, project_context_root):
        return None
    return chunk


def _scope_visible(
    meta: ChunkMetadata,
    scope_filter: ScopeFilter | None,
    project_context_root: Path | None,
) -> bool:
    """Scope half of :func:`neighbor_visible`, mirroring ``scope_context_sql``.

    The always-on context boundary applies to every neighbour; an explicit
    filter is an additional way in, not a narrowing. Out-of-project
    ``scope=project_shared`` is the deliberate cross-project opt-in
    :func:`memtomem.storage.sqlite_scope.scope_context_sql` documents, so it
    reveals project-tier neighbours from any project while the boundary keeps
    user-tier ones visible too.
    """
    if chunk_in_scope_boundary(meta, project_context_root):
        return True
    # An empty filter carries no intent, and ``scope_context_sql`` falls back
    # to the no-filter rule for it. Reading its ``matches()`` — which admits
    # everything — as an opt-in would hand out other projects' rows on a
    # ``scope=[]`` that the retrievers answer with ``scope = 'user'``.
    if scope_filter is None or not (scope_filter.scopes or scope_filter.pattern):
        return False
    if not scope_filter.matches(meta.scope):
        return False
    # Explicit filter matched. In-project, project-tier rows stay pinned to
    # the current root (already ruled out above); out-of-project, the filter
    # is the cross-project opt-in.
    return project_context_root is None
