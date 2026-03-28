"""Tool: mem_importance_scan."""

from __future__ import annotations

from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register


@mcp.tool()
@tool_handler
@register("advanced")
async def mem_importance_scan(
    namespace: str | None = None,
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Compute and update importance scores for all chunks.

    Importance = weighted combination of access_count, tag_count,
    relation_count, and recency.

    Args:
        namespace: Optional namespace filter.
    """
    from datetime import datetime, timezone

    from memtomem.search.importance import compute_importance

    app = _get_app(ctx)
    rows = await app.storage.get_chunk_factors(namespace=namespace)
    now = datetime.now(timezone.utc)

    weights = (
        tuple(app.config.importance.weights)
        if app.config.importance.weights
        else (0.3, 0.2, 0.3, 0.2)
    )

    scores: dict[str, float] = {}
    for row in rows:
        try:
            updated_at = datetime.fromisoformat(row["updated_at"])
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            age_days = (now - updated_at).total_seconds() / 86400
        except (ValueError, TypeError):
            age_days = 0.0

        score = compute_importance(
            row["access_count"], row["tag_count"], row["relation_count"], age_days, weights
        )
        scores[row["id"]] = score

    updated = await app.storage.update_importance_scores(scores)
    return f"Updated importance scores for {updated} chunks."
