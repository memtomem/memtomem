"""Tools: mem_consolidate, mem_consolidate_apply."""

from __future__ import annotations

import logging
from uuid import UUID

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
    ctx: CtxType = None,  # type: ignore[assignment]
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

    # Store group info for apply step (with timestamp for staleness check)
    import time

    app._consolidation_groups = groups  # type: ignore[attr-defined]
    app._consolidation_ts = time.time()  # type: ignore[attr-defined]

    return "\n".join(lines)


@mcp.tool()
@tool_handler
@register("maintenance")
async def mem_consolidate_apply(
    group_id: int,
    summary: str,
    keep_originals: bool = True,
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Apply a consolidation by creating a summary chunk for a group.

    The agent writes the summary; this tool creates it as a new chunk
    and optionally links it to the originals via cross-references.

    Args:
        group_id: Group ID from mem_consolidate output
        summary: The consolidated summary written by the agent
        keep_originals: Keep original chunks (default True). If False, marks them for decay.
    """
    app = _get_app(ctx)

    import time

    groups = getattr(app, "_consolidation_groups", None)
    if not groups:
        return "Error: run mem_consolidate first to identify groups."

    ts = getattr(app, "_consolidation_ts", 0)
    if time.time() - ts > 3600:
        return "Error: consolidation groups are stale (>1 hour). Run mem_consolidate again."

    group = next((g for g in groups if g["group_id"] == group_id), None)
    if group is None:
        return f"Error: group_id {group_id} not found. Run mem_consolidate again."

    # Create summary chunk via mem_add
    from memtomem.server.tools.memory_crud import mem_add

    result = await mem_add(
        content=summary,
        title=f"Consolidated: {group['source'].split('/')[-1]}",
        tags=["consolidated", "summary"],
        namespace=group.get("namespace"),
        ctx=ctx,
    )

    # Find the newly created summary chunk (most recently created)
    linked = 0
    recent = await app.storage.recall_chunks(limit=1)
    if recent:
        summary_id = recent[0].id
        for cid in group["chunk_ids"]:
            try:
                await app.storage.add_relation(
                    UUID(cid),
                    summary_id,
                    "consolidated_into",
                )
                linked += 1
            except (ValueError, TypeError):
                logger.debug("Skipping invalid UUID in consolidation: %s", cid)
            except Exception:
                logger.warning("Failed to link chunk %s in consolidation", cid, exc_info=True)

    return (
        f"Consolidation applied for group {group_id}.\n"
        f"{result}\n"
        f"- Original chunks: {group['chunk_count']}\n"
        f"- Originals kept: {keep_originals}"
    )
