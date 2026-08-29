"""Tools: mem_search, mem_expand."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any
from uuid import UUID

from memtomem.constants import INVALID_OUTPUT_FORMAT_PREFIX
from memtomem.models import (
    Chunk,
    InvalidFilterSyntaxError,
    NamespaceFilter,
    ScopeFilter,
)
from memtomem.search.visibility import neighbor_visible

# Shared by the MCP tools, the CLI, the web routes, and the in-process
# LangGraph adapter; re-exported from this module so the long-standing
# import path — and the test patches that target it — keep working.
from memtomem.runtime.project_context import (
    _resolve_project_context_from_dirs as _resolve_project_context_from_dirs,
    _resolve_project_context_root as _resolve_project_context_root,
)
from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app_initialized
from memtomem.server.error_handler import tool_handler
from memtomem.server.formatters import (
    OutputFormat,
    _VALID_OUTPUT_FORMATS,
    _display_path,
    _format_results,
    _format_structured_results,
)
from memtomem.server.helpers import _announce_dim_mismatch_once
from memtomem.server.tool_registry import register
from memtomem.server.tools._id_access import not_found, resolve_chunk
from memtomem.services.search_service import (
    InvalidRrfWeightError,
    InvalidTemporalBoundError,
    parse_as_of_bound,
    rrf_weights_from,
    run_search,
)
from memtomem.config import MAX_CONTEXT_WINDOW_CHUNKS
from memtomem.server.validation import MAX_QUERY_LENGTH

logger = logging.getLogger(__name__)


@mcp.tool()
@tool_handler
async def mem_search(
    query: str,
    top_k: int = 10,
    source_filter: str | None = None,
    tag_filter: str | None = None,
    namespace: str | None = None,
    as_of: str | None = None,
    bm25_weight: float | None = None,
    dense_weight: float | None = None,
    context_window: int = 0,
    verbose: bool = False,
    output_format: OutputFormat = "compact",
    scope: str | None = None,
    rerank: bool | None = None,
    record: bool = True,
    ctx: CtxType = None,
) -> str:
    """Search across indexed memory files using hybrid BM25 + semantic search.

    Args:
        query: Natural language search query
        top_k: Number of results to return (default 10)
        source_filter: Source path filter — substring, or glob with *, ?, []
        tag_filter: Comma-separated tags; matches chunks carrying ANY of them
        namespace: Namespace scope — value, comma list (``work,personal``) or
            glob (``proj:*``), not both. Omitted, system namespaces are hidden;
            naming one includes it.
        as_of: Temporal bound for retroactive search — ``YYYY-MM-DD`` or
            ``YYYY-QN``, default now. Chunks whose ``valid_from`` /
            ``valid_to`` frontmatter excludes that point drop out; chunks
            without those keys are always valid. Time decay anchors here,
            not to the wall clock.
        bm25_weight: RRF weight for keyword matches (default 1.0; raise to favor)
        dense_weight: RRF weight for meaning matches (default 1.0). Both must be
            finite and >= 0, not both zero; 0 disables that leg
        context_window: Expand each result with ±N adjacent chunks (0 = off).
            Neighbours follow the namespace/scope/validity visibility rules,
            not the tag/type/date filters
        verbose: Deprecated — use output_format="verbose"
        output_format: "compact" (default), "verbose" (adds UUID / pipeline stats),
            or "structured" (JSON). A non-default value overrides ``verbose``.
        scope: ADR-0011 tier filter — value, comma list (``user,project_local``)
            or glob (``project_*``), not both. Omitted, the default merge
            applies: inside a project ``user`` + that project's tiers, outside
            one ``user`` only. Pass ``project_shared`` from outside a project to
            search across projects.
        rerank: ``false`` skips the cross-encoder stage and collapses the pool
            to ``top_k`` — the fast path for latency-bounded callers, so it
            narrows recall as well as changing the score scale. Omitted/``true``
            follows server config; ``true`` cannot enable reranking a server has
            disabled.
        record: ``false`` = background read, for fan-out callers: no
            access-count increments, no query history, caches neither read
            nor written, dense retrieval exhaustive — so results can differ.

    A count below ``top_k`` can mean filters excluded candidates or the index
    holds no more. Raising ``top_k`` widens the request; it does not promise
    more results.

    ``output_format="structured"`` adds a top-level ``score_scale`` naming the
    scale ``score`` is on: ``rerank`` (model-dependent range; ``reranker`` names
    the model), ``rrf``, ``bm25``/``dense``, or ``none`` (filter-only — no
    relevance scale). Compare scores only within one scale, and only across
    servers with the same optional modifier stages enabled.
    """
    if not query.strip():
        return "Error: query cannot be empty."
    if len(query) > MAX_QUERY_LENGTH:
        return f"Error: query too long (max 10,000 characters, got {len(query)})."
    if not 1 <= top_k <= 100:
        return f"Error: top_k must be between 1 and 100, got {top_k}."

    # Resolve effective format: output_format takes precedence over verbose
    effective_format = output_format
    if effective_format == "compact" and verbose:
        effective_format = "verbose"
    if effective_format not in _VALID_OUTPUT_FORMATS:
        return f"Error: {INVALID_OUTPUT_FORMAT_PREFIX} '{output_format}'."

    # Reject malformed arguments before touching the app: validation here runs
    # without an initialized server. The core re-derives both from the raw
    # values, which keeps one definition of what is accepted.
    try:
        parse_as_of_bound(as_of)
        rrf_weights_from(bm25_weight, dense_weight)
        NamespaceFilter.parse(namespace)
        ScopeFilter.parse(scope)
    except (
        InvalidTemporalBoundError,
        InvalidRrfWeightError,
        InvalidFilterSyntaxError,
    ) as e:
        return f"Error: {e}"

    app = await _get_app_initialized(ctx)

    project_context_root = _resolve_project_context_root(app)
    results, stats, hints = await run_search(
        app.search_pipeline,
        query=query,
        top_k=top_k,
        source_filter=source_filter,
        tag_filter=tag_filter,
        namespace=namespace,
        current_namespace=app.current_namespace,
        as_of=as_of,
        bm25_weight=bm25_weight,
        dense_weight=dense_weight,
        context_window=context_window,
        scope=scope,
        rerank=rerank,
        record=record,
        project_context_root=project_context_root,
        origin="mcp",
    )

    # The dimension-mismatch notice is per-process announcement state, not a
    # property of this query, so it is appended here rather than in the core.
    # Skipped when this query already carries the per-search degradation hint
    # (#2063): that hint names the same dimensions and the same fix, so
    # emitting both duplicates one notice. The announce flag is deliberately
    # left unconsumed in that branch, so mem_add / mem_recall still get their
    # one-shot on the write side.
    if not stats.dense_suppressed_mismatch:
        dim_notice = await _announce_dim_mismatch_once(app)
        if dim_notice:
            hints.append(dim_notice)

    if not results:
        # Collect the filter/error context that compact/verbose embed in the
        # empty-result text. For structured mode these are surfaced through
        # the JSON ``hints`` array so machine consumers get the same notice.
        empty_hints: list[str] = []
        # ``tag_filter`` is not named here: it is enforced at the retrievers
        # now (#2191), so anything counted in ``fused_total`` already carries
        # the tag and telling the caller to drop it would be false advice.
        # A tag that matches nothing yields ``fused_total == 0`` and no hint.
        if source_filter and stats.fused_total > 0:
            empty_hints.append(
                f"No results match your filters "
                f"({stats.fused_total} results found before filtering). "
                f"Try a broader filter or remove source_filter."
            )
        if stats.bm25_error and stats.dense_error:
            empty_hints.append(
                "Search unavailable: both keyword and semantic search failed. "
                f"BM25: {stats.bm25_error}; Dense: {stats.dense_error}"
            )
        elif stats.bm25_error:
            empty_hints.append(f"keyword search unavailable: {stats.bm25_error}")
        elif stats.dense_error:
            empty_hints.append(f"semantic search unavailable: {stats.dense_error}")

        if effective_format == "structured":
            all_hints = hints + empty_hints
            return _format_structured_results(
                [], hints=all_hints or None, query_run_id=stats.query_run_id
            )

        # Pick the message, then append hints once. The hint tail has to be
        # the last thing every branch does: ``hints`` can carry the one-shot
        # dimension notice, which this call already consumed, so a branch
        # that returns without it doesn't just skip a line — it destroys the
        # only announcement the process was ever going to make.
        if source_filter and stats.fused_total > 0:
            message = (
                f"No results match your filters "
                f"({stats.fused_total} results found before filtering). "
                f"Try a broader filter or remove source_filter."
            )
        elif stats.bm25_error and stats.dense_error:
            message = (
                "Search unavailable: both keyword and semantic search failed.\n"
                f"- BM25: {stats.bm25_error}\n"
                f"- Dense: {stats.dense_error}"
            )
        elif stats.bm25_error:
            message = f"No results found. (Note: keyword search unavailable: {stats.bm25_error})"
        elif stats.dense_error:
            message = f"No results found. (Note: semantic search unavailable: {stats.dense_error})"
        else:
            message = "No results found."

        # Even when the result set is empty, surface hints — the caller may
        # have archived results they're unaware of.
        tail = "\n\n" + "\n".join(f"({h})" for h in hints) if hints else ""
        return message + tail

    if effective_format == "structured":
        output = _format_structured_results(
            results,
            hints=hints or None,
            score_scale=stats.score_scale,
            reranker=stats.reranker_model,
            query_run_id=stats.query_run_id,
        )
    else:
        is_verbose = effective_format == "verbose"
        output = _format_results(results, verbose=is_verbose)

        if stats.bm25_error and not is_verbose:
            output += "\n\n(Note: keyword index unavailable — results from semantic search only)"

        if is_verbose:
            pipeline_info = []
            if stats.bm25_candidates:
                pipeline_info.append(f"BM25:{stats.bm25_candidates}")
            if stats.dense_candidates:
                pipeline_info.append(f"Dense:{stats.dense_candidates}")
            if stats.fused_total:
                pipeline_info.append(f"RRF:{stats.fused_total}")
            pipeline_info.append(f"Final:{stats.final_total}")
            if stats.bm25_error:
                pipeline_info.append(f"BM25-err:{stats.bm25_error}")
            if stats.dense_error:
                pipeline_info.append(f"Dense-err:{stats.dense_error}")
            output += f"\n\n---\npipeline: {' → '.join(pipeline_info)}"

        for hint in hints:
            output += f"\n\n({hint})"

    # Fire webhook (see ``WebhookManager.fire`` — it spawns its own tracked task).
    if app.webhook_manager:
        await app.webhook_manager.fire("search", {"query": query, "result_count": len(results)})

    return output


def _expand_visibility_predicate(app: Any) -> Callable[[Chunk], bool]:
    """Build the neighbour-visibility test ``mem_expand`` screens with.

    The rule the search pipeline applies to ``context_window`` neighbours
    (:func:`memtomem.search.visibility.neighbor_visible`), at its default
    setting: system namespaces hidden, the ADR-0011 boundary pinned to the
    caller's project context, expired chunks dropped.

    Deliberately *not* keyed to the anchor chunk. Reading the id as an
    opt-in — "you named this chunk, so its namespace and project are yours
    to read around" — assumes possession of an id implies prior authorised
    access, and that does not hold: several surfaces hand out full ids for
    chunks the default rules hide, among them ``mem_dedup_scan``,
    ``mem_export`` and the web ``/chunks`` route (#2236). Widening on that
    basis would be inferring authorisation from something that carries
    none. Callers who want that context ask for it through ``mem_search``,
    whose ``namespace=`` and ``scope=`` are actual requests.
    """
    config = app.config.search
    system_prefixes = tuple(config.system_namespace_prefixes or ())
    ns_filter = NamespaceFilter.parse(None, system_prefixes=system_prefixes)
    project_context_root = _resolve_project_context_root(app)
    as_of_unix = int(time.time())

    def visible(candidate: Chunk) -> bool:
        return neighbor_visible(
            candidate,
            ns_filter=ns_filter,
            system_prefixes=system_prefixes,
            scope_filter=None,
            project_context_root=project_context_root,
            as_of_unix=as_of_unix,
        )

    return visible


@mcp.tool()
@tool_handler
@register("search")
async def mem_expand(
    chunk_id: str,
    window: int = 2,
    ctx: CtxType = None,
) -> str:
    """Expand a chunk with adjacent context from the same source file.

    Use this after mem_search when you need more surrounding context for
    a specific result. Returns ±N adjacent chunks ordered by line number.

    Neighbours inherit the same visibility rules as mem_search's
    context_window: system namespaces, the project-scope boundary, and
    temporal validity are enforced, so a hidden neighbour shrinks the window
    rather than surfacing. The chunk id does not widen that — expanding a
    chunk in a hidden namespace returns the chunk with little or no context.
    Use mem_search(namespace=..., context_window=N) to read around one.

    The addressed chunk must itself be inside the caller's project boundary
    (ADR-0011); an id from another project reads as not-found. A hidden or
    expired anchor still expands — those axes are search-relevance defaults,
    not boundaries (ADR-0036).

    Args:
        chunk_id: The UUID of the chunk to expand (from mem_search results)
        window: Number of adjacent chunks before and after (default 2, max 10)
    """
    window = max(0, min(window, MAX_CONTEXT_WINDOW_CHUNKS))
    app = await _get_app_initialized(ctx)

    try:
        uid = UUID(chunk_id)
    except (ValueError, TypeError):
        return f"Error: invalid chunk ID format: {chunk_id}"

    chunk = await resolve_chunk(app, uid)
    if chunk is None:
        return not_found(chunk_id)

    source_file = chunk.metadata.source_file
    all_chunks = await app.storage.list_chunks_by_source(source_file, limit=10000)

    # Find position of this chunk
    idx_map = {str(c.id): i for i, c in enumerate(all_chunks)}
    pos = idx_map.get(chunk_id)
    if pos is None:
        return f"Chunk {chunk_id} not found in source file listing."

    visible = _expand_visibility_predicate(app)
    before = [c for c in all_chunks[max(0, pos - window) : pos] if visible(c)]
    after = [c for c in all_chunks[pos + 1 : pos + 1 + window] if visible(c)]
    # The addressed chunk is always part of the accounting set: it is being
    # returned as the match, so counting around it as if it were hidden would
    # report a position that does not describe what the caller is reading.
    # (An expired anchor still surfaces here — the id was named explicitly —
    # while expired *neighbours* stay filtered out.)
    visible_ids = [str(c.id) for c in all_chunks if str(c.id) == chunk_id or visible(c)]
    anchor_rank = visible_ids.index(chunk_id) + 1

    parts = [
        f"## Expand: chunk {anchor_rank}/{len(visible_ids)} in {_display_path(source_file)}",
        f"Window: ±{window} chunks\n",
    ]

    if before:
        parts.append("### Before")
        for c in before:
            hierarchy = (
                " > ".join(c.metadata.heading_hierarchy) if c.metadata.heading_hierarchy else ""
            )
            header = f"**[{_display_path(c.metadata.source_file)} L{c.metadata.start_line}-{c.metadata.end_line}]**"
            if hierarchy:
                header += f" {hierarchy}"
            parts.append(f"{header}\n```\n{c.content}\n```")

    parts.append("### Matched")
    parts.append(f"```\n{chunk.content}\n```")

    if after:
        parts.append("### After")
        for c in after:
            hierarchy = (
                " > ".join(c.metadata.heading_hierarchy) if c.metadata.heading_hierarchy else ""
            )
            header = f"**[{_display_path(c.metadata.source_file)} L{c.metadata.start_line}-{c.metadata.end_line}]**"
            if hierarchy:
                header += f" {hierarchy}"
            parts.append(f"{header}\n```\n{c.content}\n```")

    return "\n\n".join(parts)


@tool_handler
@register("search")
async def mem_increment_access(
    chunk_ids: list[str],
    ctx: CtxType = None,
) -> str:
    """Increment access_count for the given chunks.

    Drives the access-frequency boost in search ranking.

    Used by external surfacing systems (e.g. memtomem-stm) to record positive
    feedback as a future search-ranking boost. Each call increments the count
    by 1 per chunk; the search pipeline applies a logarithmic transform with
    ``max_boost`` capping (default 1.5×) so this never produces runaway scores.

    Idempotency / per-event capping is the caller's responsibility — this
    action just forwards the IDs to storage.

    Args:
        chunk_ids: List of chunk UUIDs (strings) to boost
    """
    app = await _get_app_initialized(ctx)

    if not chunk_ids:
        return "No chunk_ids provided."

    valid: list[UUID] = []
    invalid: list[str] = []
    for cid in chunk_ids:
        try:
            valid.append(UUID(cid))
        except (ValueError, TypeError):
            invalid.append(str(cid))

    if not valid:
        return f"Error: no valid UUIDs in chunk_ids (rejected: {len(invalid)})."

    await app.storage.increment_access(valid)

    msg = f"Incremented access_count for {len(valid)} chunk(s)."
    if invalid:
        msg += f" Skipped {len(invalid)} invalid id(s)."
    return msg
