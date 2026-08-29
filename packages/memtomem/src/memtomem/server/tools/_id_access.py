"""ADR-0036: resolving a chunk id inside the caller's project boundary.

One helper, so that every tool taking a chunk id screens it the same way and
a new one cannot quietly skip the check by reaching for ``storage.get_chunk``.
The rule and its rationale live in
:func:`memtomem.search.visibility.resolve_visible_chunk`; this module only
binds it to an ``app``.
"""

from __future__ import annotations

from uuid import UUID

from pathlib import Path

from memtomem.models import Chunk
from memtomem.search.visibility import chunk_in_scope_boundary, resolve_visible_chunk


def not_found(chunk_id: str) -> str:
    """The one answer for "no such id" and "not yours" alike.

    Callers must not vary it. An out-of-boundary id that reported anything a
    nonexistent id would not — a different message, a different code, a
    different latency-visible branch — would confirm the row exists, which is
    the fact the boundary is there to withhold.
    """
    return f"Chunk {chunk_id} not found."


async def resolve_chunk(app, chunk_id: UUID) -> Chunk | None:
    """Fetch a chunk by id, or ``None`` if it is outside the caller's project.

    ``None`` covers both cases on purpose; render it with :func:`not_found`.
    """
    return await resolve_visible_chunk(
        app.storage,
        chunk_id,
        project_context_root=caller_boundary(app),
    )


def caller_boundary(app) -> Path | None:
    """The caller's project context root, for screening a batch of chunks.

    Use with :func:`in_boundary` where a surface has already fetched the
    chunks (a relation listing, a window) and re-fetching through
    :func:`resolve_chunk` would double the queries or erase the distinction
    between an absent row and a hidden one.

    Resolved through ``server.tools.search``'s re-export, imported late: that
    is the patch point the MCP tools' tests have always used
    (``runtime/project_context.py``), and reading the root anywhere else would
    give this module a second, unpatched way to answer the same question.
    The import is inside the call because ``search`` imports this module.
    """
    from memtomem.server.tools.search import _resolve_project_context_root

    return _resolve_project_context_root(app)


def in_boundary(chunk: Chunk, project_context_root: Path | None) -> bool:
    """Whether ``chunk`` is inside ``project_context_root``'s boundary."""
    return chunk_in_scope_boundary(chunk.metadata, project_context_root)
