"""Search service: the surface-independent half of a memory search.

Holds the retrieval logic every surface repeats — temporal-bound parsing,
namespace fallback, RRF weight assembly, the pipeline call, and the
trust-UX hints that belong to the *result*, not to the transport. The MCP
tool, the CLI, and the in-process adapters can all call this and then
render however they render.

Dependencies arrive explicitly (``pipeline``, ``current_namespace``,
``project_context_root``) rather than through a server ``app`` object, so
this module imports nothing from MCP/Web/CLI — see ``services/__init__``.

``origin`` has no default: it labels the call in the persisted query-run
observation, and a default here would silently mislabel whichever surface
forgot to pass one.

Errors are raised, not returned as strings. Each surface translates them
into its own idiom (the MCP tool returns ``"Error: ..."`` text, web routes
map to HTTP status codes).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from memtomem.chunking.markdown import _parse_validity_bound

if TYPE_CHECKING:
    from pathlib import Path

    from memtomem.models import SearchResult
    from memtomem.search.pipeline import RetrievalStats, SearchPipeline


class InvalidTemporalBoundError(ValueError):
    """``as_of`` was not a recognized temporal bound.

    A dedicated type so surfaces can translate *this* failure without
    also catching a ``ValueError`` raised from inside the pipeline.
    """


def parse_as_of_bound(as_of: str | None) -> int | None:
    """Parse an ``as_of`` temporal bound into a unix timestamp.

    Exposed separately so surfaces can reject a malformed bound before
    they do any setup work — ``mem_search`` validates its arguments
    before initializing the app, and that ordering is part of its
    contract.

    Raises:
        InvalidTemporalBoundError: ``as_of`` is not a recognized bound.
    """
    if as_of is None:
        return None
    as_of_unix = _parse_validity_bound(as_of, upper=False)
    if as_of_unix is None:
        raise InvalidTemporalBoundError(
            f"invalid as_of value '{as_of}'. "
            "Accepted formats: 'YYYY-MM-DD' (date) or 'YYYY-QN' (quarter, N in 1-4)."
        )
    return as_of_unix


async def run_search(
    pipeline: SearchPipeline,
    *,
    query: str,
    top_k: int = 10,
    source_filter: str | None = None,
    tag_filter: str | None = None,
    namespace: str | None = None,
    current_namespace: str | None = None,
    as_of: str | None = None,
    bm25_weight: float | None = None,
    dense_weight: float | None = None,
    context_window: int = 0,
    scope: str | None = None,
    rerank: bool | None = None,
    project_context_root: Path | None = None,
    origin: str,
) -> tuple[list[SearchResult], RetrievalStats, list[str]]:
    """Run a hybrid search and assemble the result-derived hints.

    Args:
        pipeline: Search pipeline to query.
        query: Search query, already validated by the caller.
        namespace: Explicit namespace, or ``None`` to fall back to
            ``current_namespace``.
        current_namespace: The surface's ambient namespace.
        as_of: ``YYYY-MM-DD`` / ``YYYY-QN`` temporal bound, or ``None``.
        project_context_root: ADR-0011 scope anchor; resolve it on the
            caller's side (``runtime.project_context``).
        origin: Call-origin label recorded with the query run.

    Returns:
        ``(results, stats, hints)``. ``hints`` are the notices derivable
        from the query and its stats, in emission order; surface-bound
        notices (e.g. the embedding-dimension announcement, which is
        per-process state) are the caller's to append.

    Raises:
        InvalidTemporalBoundError: ``as_of`` is not a recognized bound.
    """
    as_of_unix = parse_as_of_bound(as_of)

    effective_ns = namespace or current_namespace

    rrf_weights = None
    if bm25_weight is not None or dense_weight is not None:
        rrf_weights = [bm25_weight or 1.0, dense_weight or 1.0]

    results, stats = await pipeline.search(
        query=query,
        top_k=top_k,
        source_filter=source_filter,
        tag_filter=tag_filter,
        namespace=effective_ns,
        rrf_weights=rrf_weights,
        context_window=context_window if context_window > 0 else None,
        as_of_unix=as_of_unix,
        scope=scope,
        project_context_root=project_context_root,
        rerank=rerank,
        origin=origin,
    )

    # Trust-UX hints shared across formats. The archive notice is emitted
    # only for callers who did NOT pin a namespace — otherwise the archive
    # filter never engaged.
    hints: list[str] = []
    # stats.rerank_applied is the per-call effective decision, not the live
    # config — accurate even when a hot reload flips rerank.enabled while
    # this call is in flight.
    if rerank is True and not stats.rerank_applied:
        hints.append(
            "rerank=true requested but server reranking is disabled "
            "(rerank.enabled=false); results are un-reranked."
        )
    if effective_ns is None and stats.hidden_system_ns > 0:
        hints.append(
            f"{stats.hidden_system_ns} result(s) hidden in system namespaces "
            f'(pass namespace="archive:..." to include them).'
        )

    return results, stats, hints
