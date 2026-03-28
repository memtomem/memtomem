"""Tool: mem_eval — memory system health report."""

from __future__ import annotations

from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register


@mcp.tool()
@tool_handler
@register("analytics")
async def mem_eval(
    since: str | None = None,
    namespace: str | None = None,
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Evaluate memory system health and effectiveness.

    Analyzes search patterns, access frequency, tag coverage, and
    indexing activity to produce a health report.

    Args:
        since: Only analyze activity after this date (YYYY-MM-DD or ISO)
        namespace: Scope analysis to this namespace
    """
    app = _get_app(ctx)
    db = app.storage._get_db()

    lines = ["## Memory Health Report\n"]

    # 1. Overall stats
    stats = await app.storage.get_stats()
    lines.append("### Index Stats")
    lines.append(f"- Total chunks: {stats.get('total_chunks', 0)}")
    lines.append(f"- Total sources: {stats.get('total_sources', 0)}")

    # 2. Access patterns
    top_accessed = db.execute(
        "SELECT id, access_count, substr(content, 1, 60) FROM chunks "
        "WHERE access_count > 0 ORDER BY access_count DESC LIMIT 10"
    ).fetchall()
    if top_accessed:
        lines.append(f"\n### Most Accessed (Top {len(top_accessed)})")
        for row in top_accessed:
            preview = row[2].replace("\n", " ")[:50]
            lines.append(f"  {row[1]}x — {row[0][:8]}... {preview}")

    # 3. Dead memories (never accessed)
    total = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    never_accessed = db.execute("SELECT COUNT(*) FROM chunks WHERE access_count = 0").fetchone()[0]
    if total > 0:
        dead_pct = never_accessed / total * 100
        lines.append("\n### Access Coverage")
        lines.append(f"- Never accessed: {never_accessed}/{total} ({dead_pct:.0f}%)")
        lines.append(
            f"- Accessed at least once: {total - never_accessed}/{total} ({100 - dead_pct:.0f}%)"
        )

    # 4. Tag coverage
    tagged = db.execute("SELECT COUNT(*) FROM chunks WHERE tags != '[]' AND tags != ''").fetchone()[
        0
    ]
    if total > 0:
        tag_pct = tagged / total * 100
        lines.append("\n### Tag Coverage")
        lines.append(f"- Tagged: {tagged}/{total} ({tag_pct:.0f}%)")
        lines.append(f"- Untagged: {total - tagged}/{total} ({100 - tag_pct:.0f}%)")

    # 5. Namespace distribution
    ns_counts = await app.storage.list_namespaces()
    if ns_counts:
        lines.append("\n### Namespace Distribution")
        for ns, count in ns_counts:
            lines.append(f"  {ns}: {count} chunks")

    # 6. Session activity
    session_count = db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    if session_count > 0:
        lines.append("\n### Session Activity")
        lines.append(f"- Total sessions: {session_count}")
        recent = db.execute(
            "SELECT COUNT(*) FROM sessions WHERE started_at >= date('now', '-7 days')"
        ).fetchone()[0]
        lines.append(f"- Last 7 days: {recent}")

    # 7. Working memory
    wm_count = db.execute("SELECT COUNT(*) FROM working_memory").fetchone()[0]
    if wm_count > 0:
        promoted = db.execute("SELECT COUNT(*) FROM working_memory WHERE promoted = 1").fetchone()[
            0
        ]
        lines.append("\n### Working Memory")
        lines.append(f"- Active entries: {wm_count}")
        lines.append(f"- Promoted to long-term: {promoted}")

    # 8. Cross-references
    rel_count = db.execute("SELECT COUNT(*) FROM chunk_relations").fetchone()[0]
    if rel_count > 0:
        lines.append("\n### Cross-References")
        lines.append(f"- Total links: {rel_count}")

    return "\n".join(lines)
