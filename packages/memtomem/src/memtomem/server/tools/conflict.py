"""Tool: mem_conflict_check."""

from __future__ import annotations

from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register


@mcp.tool()
@tool_handler
@register("advanced")
async def mem_conflict_check(
    content: str,
    threshold: float = 0.75,
    ctx: CtxType = None,
) -> str:
    """Check for potential contradictions with existing memories.

    Finds chunks with high semantic similarity but low text overlap,
    which may indicate conflicting information.

    Args:
        content: Content to check against existing memories.
        threshold: Minimum similarity score to consider (default 0.75).
    """
    app = _get_app(ctx)

    from memtomem.search.conflict import detect_conflicts

    conflicts = await detect_conflicts(
        content,
        app.storage,
        app.embedder,
        threshold=threshold,
    )

    if not conflicts:
        return "No conflicts detected."

    lines = [f"Potential conflicts ({len(conflicts)}):"]
    for c in conflicts:
        preview = c.existing_chunk.content[:100].replace("\n", " ")
        lines.append(
            f"  - similarity={c.similarity:.0%} overlap={c.text_overlap:.0%} "
            f"score={c.conflict_score:.2f}\n    {preview}..."
        )
    return "\n".join(lines)
