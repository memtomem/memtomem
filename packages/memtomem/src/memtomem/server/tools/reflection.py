"""Tools: mem_reflect, mem_reflect_save."""

from __future__ import annotations

import logging

from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app_initialized
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register
from memtomem.server.tools._id_access import caller_boundary, in_boundary, resolve_chunk

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
    boundary = caller_boundary(app)

    # 1. Most frequently accessed topics
    top_topics = await storage.get_frequently_accessed(
        namespace=namespace, limit=limit, project_context_root=boundary
    )
    if top_topics:
        lines.append("### Frequently Accessed Topics")
        for row in top_topics:
            hierarchy = row["hierarchy"]
            topic = " > ".join(hierarchy) if hierarchy else row["source_file"].split("/")[-1]
            lines.append(f"  {row['total_access']}x — {topic}")
        lines.append("")

    # Session rows do not carry a project identity. Whole-store session
    # analytics are intentionally reserved for local maintenance surfaces.

    # 3. Tag frequency (what topics keep coming up)
    tag_counts = await storage.get_tag_counts(project_context_root=boundary)
    if tag_counts:
        lines.append("### Recurring Themes (by tag)")
        for tag, count in tag_counts[:limit]:
            lines.append(f"  {tag}: {count} chunks")
        lines.append("")

    # 4. Knowledge gaps (queries with no results)
    # Zero-result searches are exactly what this section counts, and their
    # history rows are written in the background (#2183) — settle them so a
    # reflection run right after a failed search sees it.
    await app.search_pipeline.flush_observation()
    gaps = await storage.get_knowledge_gaps(limit=min(limit, 10), project_context_root=boundary)
    if gaps:
        lines.append("### Knowledge Gaps (frequent queries with no results)")
        for row in gaps:
            lines.append(f'  {row["count"]}x — "{row["query"][:60]}"')
        lines.append("")

    # 5. Cross-reference clusters
    #
    # ``get_most_connected`` ranks by whole-store degree, so its order is not
    # the caller's order: a hub with ten edges of which one is visible would
    # outrank a hub with nine visible ones. Taking the first survivors of that
    # ranking would both hide the genuinely most-connected visible hub and let
    # the hidden edges decide what gets listed — the ranking itself becomes a
    # channel. So every over-fetched candidate is scored on its *visible*
    # degree and the page is cut after that re-ranking. Candidates beyond the
    # over-fetch window are still lost, which needs a boundary-aware aggregate
    # in SQL (#2244).
    want = min(limit, 5)
    connected = await storage.get_most_connected(
        limit=max(want * 4, want),
        namespace=namespace,
        project_context_root=boundary,
    )
    if connected:
        scored: list[tuple[int, str]] = []
        for row in connected:
            chunk = None
            try:
                from uuid import UUID

                UUID(row["chunk_id"])
                chunk = await storage.get_chunk(row["chunk_id"])
            except (ValueError, TypeError):
                pass
            # An unresolved hub is dropped, never degraded to its id prefix.
            # That fallback would print the uuid of a row the caller may not
            # be allowed to know about, next to a whole-store degree — the
            # two things ADR-0036 says a listing must not carry.
            if chunk is None or not in_boundary(chunk, boundary):
                continue
            related = await storage.get_related(chunk.id)
            neighbours = await storage.get_chunks_batch([rid for rid, _ in related])
            visible_links = sum(
                1
                for related_id, _rel in related
                if (n := neighbours.get(related_id)) is None or in_boundary(n, boundary)
            )
            if not visible_links:
                continue
            preview = chunk.content[:50].replace("\n", " ")
            scored.append((visible_links, f"  {visible_links} links — {preview}..."))

        # Stable sort, so hubs tied on visible degree keep the store's own
        # ordering rather than an arbitrary one.
        scored.sort(key=lambda pair: pair[0], reverse=True)
        rendered = [line for _count, line in scored[:want]]
        if rendered:
            lines.append("### Most Connected Memories")
            lines.extend(rendered)
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

        # ADR-0011 PR-D round 9: thread project context onto the
        # always-on scope filter. Insights default to user-tier today,
        # but ``mem_reflect`` may land a project-tier reflection in
        # the future; threading the context now means the
        # ``recent[0]`` lookup returns the just-written insight chunk
        # under whichever tier it lives in.
        from memtomem.server.tools.search import _resolve_project_context_root

        project_context_root = _resolve_project_context_root(app)
        recent = await app.storage.recall_chunks(limit=1, project_context_root=project_context_root)
        if recent:
            insight_id = recent[0].id
            for cid in related_chunks:
                try:
                    # Linking is a write, and the same boundary applies:
                    # a caller who may not read a chunk may not attach a
                    # reflection to it (ADR-0036). Skipped silently, like the
                    # invalid-UUID case below — a reflection is a summary, not
                    # a query, so one unusable id should not fail the save.
                    if await resolve_chunk(app, UUID(cid)) is None:
                        logger.debug("Skipping out-of-boundary chunk in related_chunks: %s", cid)
                        continue
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
