"""Tools: mem_reflect, mem_reflect_save."""

from __future__ import annotations

import logging

from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app_initialized
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register

logger = logging.getLogger(__name__)


@mcp.tool()
@tool_handler
@register("analytics")
async def mem_reflect(
    namespace: str | None = None,
    since: str | None = None,
    limit: int = 20,
    ctx: CtxType = None,
) -> str:
    """Analyze recent memory activity and surface patterns for reflection.

    Returns a statistical report of memory usage patterns that an agent
    can analyze to derive higher-level insights. Based on the Stanford
    Generative Agents reflection pattern.

    Args:
        namespace: Scope analysis to this namespace
        since: Only analyze activity after this date (YYYY-MM-DD)
        limit: Maximum items per category
    """
    if not 1 <= limit <= 200:
        return f"Error: limit must be between 1 and 200, got {limit}."

    app = await _get_app_initialized(ctx)
    storage = app.storage

    lines = ["## Memory Reflection Report\n"]

    # 1. Most frequently accessed topics
    top_topics = await storage.get_frequently_accessed(namespace=namespace, limit=limit)
    if top_topics:
        lines.append("### Frequently Accessed Topics")
        for row in top_topics:
            hierarchy = row["hierarchy"]
            topic = " > ".join(hierarchy) if hierarchy else row["source_file"].split("/")[-1]
            lines.append(f"  {row['total_access']}x — {topic}")
        lines.append("")

    # 2. Recent session activity patterns
    agent_sessions = await storage.get_agent_sessions(since=since, limit=limit)
    if agent_sessions:
        lines.append("### Agent Activity")
        for row in agent_sessions:
            lines.append(
                f"  {row['agent_id']}: {row['session_count']} sessions (last: {row['last_session']})"
            )
        lines.append("")

    # 3. Tag frequency (what topics keep coming up)
    tag_counts = await storage.get_tag_counts()
    if tag_counts:
        lines.append("### Recurring Themes (by tag)")
        for tag, count in tag_counts[:limit]:
            lines.append(f"  {tag}: {count} chunks")
        lines.append("")

    # 4. Knowledge gaps (queries with no results)
    gaps = await storage.get_knowledge_gaps(limit=min(limit, 10))
    if gaps:
        lines.append("### Knowledge Gaps (frequent queries with no results)")
        for row in gaps:
            lines.append(f'  {row["count"]}x — "{row["query"][:60]}"')
        lines.append("")

    # 5. Cross-reference clusters
    connected = await storage.get_most_connected(limit=min(limit, 5))
    if connected:
        lines.append("### Most Connected Memories")
        for row in connected:
            chunk = None
            try:
                from uuid import UUID

                UUID(row["chunk_id"])
                chunk = await storage.get_chunk(row["chunk_id"])
            except (ValueError, TypeError):
                pass
            preview = chunk.content[:50].replace("\n", " ") if chunk else row["chunk_id"][:8]
            lines.append(f"  {row['link_count']} links — {preview}...")
        lines.append("")

    # If no data was found at all, give helpful guidance
    if len(lines) == 1:  # Only the header
        return (
            "No memory activity to reflect on yet.\n\n"
            "Add memories with `mem_add` and search with `mem_search` to build activity, "
            "then run `mem_reflect` again."
        )

    lines.append("---")
    lines.append("Use `mem_reflect_save` to record insights derived from this report.")

    return "\n".join(lines)


@mcp.tool()
@tool_handler
@register("analytics")
async def mem_reflect_save(
    insight: str,
    related_chunks: list[str] | None = None,
    tags: list[str] | None = None,
    ctx: CtxType = None,
) -> str:
    """Save a reflection insight derived from memory analysis.

    After reviewing a mem_reflect report, use this to save higher-level
    observations and patterns as new memories.

    Args:
        insight: The insight or observation to save
        related_chunks: Optional list of chunk UUIDs that informed this insight
        tags: Additional tags (reflection and insight tags added automatically)
    """
    from memtomem.server.tools.memory_crud import mem_add

    all_tags = list(tags or [])
    for t in ("reflection", "insight"):
        if t not in all_tags:
            all_tags.append(t)

    result = await mem_add(
        content=insight,
        title="Reflection",
        tags=all_tags,
        file="reflections.md",
        ctx=ctx,
    )

    # Link related chunks to the new insight
    if related_chunks:
        app = await _get_app_initialized(ctx)
        from uuid import UUID

        recent = await app.storage.recall_chunks(limit=1)
        if recent:
            insight_id = recent[0].id
            for cid in related_chunks:
                try:
                    await app.storage.add_relation(
                        UUID(cid),
                        insight_id,
                        "informs_reflection",
                    )
                except (ValueError, TypeError):
                    logger.debug("Skipping invalid UUID in related_chunks: %s", cid)
                except Exception:
                    logger.warning("Failed to link chunk %s to reflection", cid, exc_info=True)

    return f"Insight saved.\n{result}"
