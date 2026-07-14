"""Tools: mem_search, mem_expand."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from uuid import UUID

from memtomem.chunking.markdown import _parse_validity_bound
from memtomem.constants import INVALID_OUTPUT_FORMAT_PREFIX
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
from memtomem.config import MAX_CONTEXT_WINDOW_CHUNKS
from memtomem.server.validation import MAX_QUERY_LENGTH
from memtomem.server.webhooks import webhook_error_cb

logger = logging.getLogger(__name__)


def _resolve_project_context_from_dirs(project_memory_dirs) -> Path | None:
    """Same as :func:`_resolve_project_context_root` but takes the dirs
    list directly (no ``app`` / ``comp`` wrapper).

    Used by web routes that have ``Mem2MemConfig`` directly via
    ``get_config`` and by any caller that already extracted the
    registered project tier list. The wrapper :func:`_resolve_project_context_root`
    delegates here so MCP tool callers (``app``) and CLI callers
    (``comp``) keep their existing one-arg signature.
    """
    project_dirs = list(project_memory_dirs)
    if not project_dirs:
        return None
    try:
        cwd = Path(os.getcwd()).resolve()
    except OSError:
        return None
    best_root: Path | None = None
    best_depth = -1
    for d in project_dirs:
        try:
            resolved = Path(d).expanduser().resolve()
        except OSError:
            continue
        # ``resolved`` is expected to be ``<root>/.memtomem/memories``
        # or ``<root>/.memtomem/memories.local``. Project root is
        # grandparent.
        if resolved.parent.name != ".memtomem":
            continue
        project_root = resolved.parent.parent
        try:
            cwd.relative_to(project_root)
        except ValueError:
            continue
        depth = len(project_root.parts)
        if depth > best_depth:
            best_depth = depth
            best_root = project_root
    return best_root


def _resolve_project_context_root(app) -> Path | None:
    """Find the registered project root that contains the current cwd.

    Returns the project root for the current process, or ``None`` if no
    registered project tier covers the current cwd. Used by MCP read
    tools as the always-on context-boundary anchor (ADR-0011 §6) so a
    memtomem server started from inside a project naturally pins memory
    queries to that project's project_shared / project_local rows.

    Resolution: for each ``project_memory_dir`` registered in the user
    config, derive its project root (the grandparent of the
    ``.memtomem/memories[.local]`` entry); if the current cwd lives
    under that root, return it. Multiple matching roots → return the
    deepest match (most specific project context wins for nested
    project layouts).

    Empty ``project_memory_dirs`` → ``None``. Permission errors during
    resolve → ``None``.

    Accepts either ``app`` (MCP) or ``comp`` (CLI) — both expose
    ``.config.indexing.project_memory_dirs`` so the duck-typed access
    is symmetric.
    """
    return _resolve_project_context_from_dirs(app.config.indexing.project_memory_dirs)


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
    ctx: CtxType = None,
) -> str:
    """Search across indexed memory files using hybrid BM25 + semantic search.

    Args:
        query: Natural language search query
        top_k: Number of results to return (default 10)
        source_filter: Filter by source file path (substring match, or glob pattern with *, ?, [])
        tag_filter: Comma-separated tags — matches chunks containing ANY of the listed tags (OR logic)
        namespace: Namespace scope (single value)
        as_of: Temporal bound for retroactive search — date-only ``YYYY-MM-DD`` or
            quarter ``YYYY-QN`` (N in 1-4). Default ``None`` = current time. Chunks
            whose ``valid_from`` / ``valid_to`` frontmatter excludes this point in
            time are filtered out (chunks without those keys are always-valid).
        bm25_weight: Override BM25 weight in RRF fusion (default 1.0). Set higher to favor keyword matches.
        dense_weight: Override dense/semantic weight in RRF fusion (default 1.0). Set higher to favor meaning.
        context_window: Expand each result with ±N adjacent chunks (0=disabled). Use for more context.
        verbose: (Deprecated — use output_format="verbose" instead.) Show full details.
        output_format: Output format — "compact" (default, human-readable), "verbose" (full
            details with UUID/pipeline stats), or "structured" (JSON for machine parsing).
            When set to non-default, overrides the verbose flag.
        scope: ADR-0011 scope-axis filter — single value, comma list (``user,project_local``)
            or glob (``project_*``). When omitted, the default merge applies: in-project
            searches return ``user`` + the current project's project tiers; out-of-project
            searches return ``user`` only. Pass ``project_shared`` from outside any
            project context for a cross-project search.

    Result count may fall below ``top_k`` when filters exclude candidates.
    Increase ``top_k`` for a wider per-call request. When reranking is enabled,
    the candidate pool is automatically derived from ``rerank.oversample``,
    ``rerank.min_pool``, and ``rerank.max_pool``; there is no per-call
    ``rerank_pool`` argument.
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

    as_of_unix: int | None = None
    if as_of is not None:
        as_of_unix = _parse_validity_bound(as_of, upper=False)
        if as_of_unix is None:
            return (
                f"Error: invalid as_of value '{as_of}'. "
                "Accepted formats: 'YYYY-MM-DD' (date) or 'YYYY-QN' (quarter, N in 1-4)."
            )

    app = await _get_app_initialized(ctx)
    effective_ns = namespace or app.current_namespace

    rrf_weights = None
    if bm25_weight is not None or dense_weight is not None:
        rrf_weights = [bm25_weight or 1.0, dense_weight or 1.0]

    project_context_root = _resolve_project_context_root(app)
    results, stats = await app.search_pipeline.search(
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
    )

    # Build trust-UX hints shared across formats: archive filter count and a
    # one-shot embedding mismatch notice. Emitted for users who did NOT pin a
    # namespace (otherwise the archive filter never engaged).
    hints: list[str] = []
    if effective_ns is None and stats.hidden_system_ns > 0:
        hints.append(
            f"{stats.hidden_system_ns} result(s) hidden in system namespaces "
            f'(pass namespace="archive:..." to include them).'
        )
    dim_notice = await _announce_dim_mismatch_once(app)
    if dim_notice:
        hints.append(dim_notice)

    if not results:
        # Collect the filter/error context that compact/verbose embed in the
        # empty-result text. For structured mode these are surfaced through
        # the JSON ``hints`` array so machine consumers get the same notice.
        empty_hints: list[str] = []
        if (source_filter or tag_filter) and stats.fused_total > 0:
            empty_hints.append(
                f"No results match your filters "
                f"({stats.fused_total} results found before filtering). "
                f"Try broader filters or remove source_filter/tag_filter."
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
            return _format_structured_results([], hints=all_hints or None)

        if (source_filter or tag_filter) and stats.fused_total > 0:
            return (
                f"No results match your filters "
                f"({stats.fused_total} results found before filtering). "
                f"Try broader filters or remove source_filter/tag_filter."
            )
        if stats.bm25_error and stats.dense_error:
            return (
                "Search unavailable: both keyword and semantic search failed.\n"
                f"- BM25: {stats.bm25_error}\n"
                f"- Dense: {stats.dense_error}"
            )
        if stats.bm25_error:
            return f"No results found. (Note: keyword search unavailable: {stats.bm25_error})"
        if stats.dense_error:
            return f"No results found. (Note: semantic search unavailable: {stats.dense_error})"
        # Even when the result set is empty, surface hints — the caller may
        # have archived results they're unaware of.
        tail = "\n\n" + "\n".join(f"({h})" for h in hints) if hints else ""
        return "No results found." + tail

    if effective_format == "structured":
        output = _format_structured_results(results, hints=hints or None)
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

    # Fire webhook
    if app.webhook_manager:
        task = asyncio.create_task(
            app.webhook_manager.fire("search", {"query": query, "result_count": len(results)})
        )
        task.add_done_callback(webhook_error_cb)

    return output


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

    chunk = await app.storage.get_chunk(uid)
    if chunk is None:
        return f"Chunk {chunk_id} not found."

    source_file = chunk.metadata.source_file
    all_chunks = await app.storage.list_chunks_by_source(source_file, limit=10000)

    # Find position of this chunk
    idx_map = {str(c.id): i for i, c in enumerate(all_chunks)}
    pos = idx_map.get(chunk_id)
    if pos is None:
        return f"Chunk {chunk_id} not found in source file listing."

    before = all_chunks[max(0, pos - window) : pos]
    after = all_chunks[pos + 1 : pos + 1 + window]

    parts = [
        f"## Expand: chunk {pos + 1}/{len(all_chunks)} in {_display_path(source_file)}",
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
    """Increment access_count for the given chunks (drives access-frequency boost in search ranking).

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
