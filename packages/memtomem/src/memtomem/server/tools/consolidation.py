"""Tools: mem_consolidate, mem_consolidate_apply."""

from __future__ import annotations

import logging

from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register

logger = logging.getLogger(__name__)


@mcp.tool()
@tool_handler
@register("maintenance")
async def mem_consolidate(
    namespace: str | None = None,
    source_filter: str | None = None,
    max_groups: int = 5,
    min_group_size: int = 3,
    ctx: CtxType = None,
) -> str:
    """Find groups of related chunks that could be consolidated into summaries.

    Analyzes chunks by source file and semantic similarity to identify
    groups that an agent can summarize. This is a dry-run — no mutations.

    Args:
        namespace: Scope to this namespace
        source_filter: Only analyze chunks from matching sources
        max_groups: Maximum number of groups to return
        min_group_size: Minimum chunks per group (default 3)
    """
    if not 1 <= max_groups <= 50:
        return "Error: max_groups must be between 1 and 50."
    if min_group_size < 2:
        return "Error: min_group_size must be at least 2."

    app = _get_app(ctx)
    effective_ns = namespace or app.current_namespace

    # Group chunks by source file
    sources = await app.storage.get_source_files_with_counts()
    if source_filter:
        from fnmatch import fnmatch

        has_glob = any(c in source_filter for c in ("*", "?", "["))
        sources = [
            s
            for s in sources
            if (fnmatch(str(s[0]), source_filter) if has_glob else source_filter in str(s[0]))
        ]

    # Filter by namespace if specified
    if effective_ns:
        sources = [s for s in sources if s[3] and effective_ns in s[3].split(",")]

    # Find groups with enough chunks
    groups = []
    group_id = 0
    for path, count, updated, ns, avg_tok, _, _ in sources:
        if count < min_group_size:
            continue
        chunks = await app.storage.list_chunks_by_source(path, limit=20)
        if len(chunks) < min_group_size:
            continue

        total_tokens = sum(len(c.content.split()) for c in chunks)
        previews = []
        chunk_ids = []
        for c in chunks[:5]:
            preview = c.content[:80].replace("\n", " ")
            previews.append(f"    - [{str(c.id)[:8]}] {preview}...")
            chunk_ids.append(str(c.id))

        groups.append(
            {
                "group_id": group_id,
                "source": str(path),
                "chunk_count": len(chunks),
                "total_tokens": total_tokens,
                "namespace": ns,
                "previews": previews,
                "chunk_ids": chunk_ids,
            }
        )
        group_id += 1
        if len(groups) >= max_groups:
            break

    if not groups:
        return (
            "No consolidation candidates found.\n"
            f"(Checked {len(sources)} source files, "
            f"min_group_size={min_group_size}, max_groups={max_groups})"
        )

    lines = [f"Consolidation candidates: {len(groups)} groups\n"]
    for g in groups:
        lines.append(f"### Group {g['group_id']}: {g['source'].split('/')[-1]}")
        lines.append(f"  Chunks: {g['chunk_count']}, ~{g['total_tokens']} tokens")
        if g["namespace"]:
            lines.append(f"  Namespace: {g['namespace']}")
        lines.extend(g["previews"])
        lines.append(f"  → Use mem_consolidate_apply(group_id={g['group_id']}, summary='...')")
        lines.append("")

    # Persist groups to scratch storage (survives restart, auto-expires in 1 hour)
    import json
    from datetime import datetime, timedelta, timezone

    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
    await app.storage.scratch_set(
        "consolidation_groups",
        json.dumps(groups, default=str),
        expires_at=expires,
    )

    return "\n".join(lines)


@mcp.tool()
@tool_handler
@register("maintenance")
async def mem_consolidate_apply(
    group_id: int,
    summary: str,
    keep_originals: bool = True,
    ctx: CtxType = None,
) -> str:
    """Apply a consolidation by creating a summary chunk for a group.

    The agent writes the summary; this tool persists it via the normal
    ``mem_add`` path — the summary becomes a real markdown entry in the
    user's first ``memory_dirs`` daily notes file, and each original chunk
    gets a ``consolidated_into`` relation pointing to the summary. This
    preserves the file-based mental model: consolidation events are visible
    in the filesystem and in git history, and the summary can be
    hand-edited like any other markdown entry.

    The policy-driven ``auto_consolidate`` flow deliberately takes a
    different path (virtual chunk in the ``archive:summary`` namespace with
    content-embedded source hash for idempotency). See
    ``project_ltm_manager_roadmap.md`` Phase A.5 for the rationale.

    Args:
        group_id: Group ID from mem_consolidate output.
        summary: The consolidated summary written by the agent.
        keep_originals: Keep original chunks (default True). If False,
            originals are soft-decayed (``importance_score *= 0.5``, floor
            0.3); never a hard delete.
    """
    import json
    from datetime import datetime, timezone

    from memtomem.tools.consolidation_engine import (
        DECAY_FLOOR,
        link_consolidation_relations,
    )

    app = _get_app(ctx)

    entry = await app.storage.scratch_get("consolidation_groups")
    if not entry:
        return "Error: run mem_consolidate first to identify groups."

    # Check expiration (scratch_get does not filter expired entries)
    if entry.get("expires_at"):
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if entry["expires_at"] < now:
            await app.storage.scratch_delete("consolidation_groups")
            return "Error: consolidation groups are stale (>1 hour). Run mem_consolidate again."

    groups = json.loads(entry["value"])

    group = next((g for g in groups if g["group_id"] == group_id), None)
    if group is None:
        return f"Error: group_id {group_id} not found. Run mem_consolidate again."

    # Agent path is file-first: append to a daily notes file + index. We use
    # ``_mem_add_core`` (not the MCP ``mem_add`` tool) so we can grab the
    # IndexingStats and recover the new chunk id without the old
    # ``recall_chunks(limit=1)`` trick, which raced with any concurrent
    # write between mem_add and the lookup — silent data corruption
    # territory.
    from memtomem.server.tools.memory_crud import _mem_add_core

    source_name = group["source"].split("/")[-1]
    add_result, stats = await _mem_add_core(
        content=summary,
        title=f"Consolidated: {source_name}",
        tags=["consolidated", "summary"],
        file=None,
        namespace=group.get("namespace"),
        template=None,
        ctx=ctx,
    )

    if stats is None or not stats.new_chunk_ids:
        logger.warning(
            "mem_consolidate_apply: mem_add produced no new chunk ids — "
            "cannot link originals for group %s",
            group_id,
        )
        await app.storage.scratch_delete("consolidation_groups")
        return (
            f"Consolidation applied for group {group_id} (unlinked).\n"
            f"{add_result}\n"
            f"- Original chunks: {group['chunk_count']}\n"
            f"- Originals kept: {keep_originals}\n"
            "- Warning: could not recover summary chunk id; relations not created."
        )

    if len(stats.new_chunk_ids) > 1:
        # Canary for chunker behavior drift. Today the Markdown chunker
        # keeps a single ``Consolidated: ...`` H1 section together, so we
        # expect exactly 1. If this warning ever fires, revisit the
        # summary → chunk matching strategy (see Phase A.5 docs-review
        # thread) — the current "take the first" rule is intentionally
        # simple so the contract failure is loud.
        logger.warning(
            "mem_consolidate_apply: mem_add produced %d chunks, using first as summary_id",
            len(stats.new_chunk_ids),
        )

    summary_id = stats.new_chunk_ids[0]

    # Link originals → summary via the shared helper (same edge type that
    # execute_auto_consolidate uses, so queries like mem_related / mem_expand
    # work uniformly across both flows).
    linked = await link_consolidation_relations(
        app.storage,
        group["chunk_ids"],
        summary_id,
    )

    if not keep_originals and group["chunk_ids"]:
        scores = await app.storage.get_importance_scores(group["chunk_ids"])
        if scores:
            floored = {cid: max(score * 0.5, DECAY_FLOOR) for cid, score in scores.items()}
            await app.storage.update_importance_scores(floored)

    app.search_pipeline.invalidate_cache()
    await app.storage.scratch_delete("consolidation_groups")

    return (
        f"Consolidation applied for group {group_id}.\n"
        f"{add_result}\n"
        f"- Summary chunk id: {summary_id}\n"
        f"- Originals linked: {linked}/{group['chunk_count']}\n"
        f"- Originals kept: {keep_originals}"
    )
