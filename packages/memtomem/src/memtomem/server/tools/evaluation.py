"""Tool: mem_eval — memory system health report."""

from __future__ import annotations

from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app_initialized
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register


@mcp.tool()
@tool_handler
@register("analytics")
async def mem_eval(
    since: str | None = None,
    namespace: str | None = None,
    ctx: CtxType = None,
) -> str:
    """Evaluate memory system health and effectiveness.

    Analyzes search patterns, access frequency, tag coverage, and
    indexing activity to produce a health report.

    Args:
        since: Only analyze activity after this date (YYYY-MM-DD or ISO)
        namespace: Scope analysis to this namespace
    """
    app = await _get_app_initialized(ctx)
    report = await app.storage.get_health_report(namespace=namespace)

    lines = ["## Memory Health Report\n"]

    # 1. Index stats
    lines.append("### Index Stats")
    lines.append(f"- Total chunks: {report['total_chunks']}")
    lines.append(f"- Total sources: {report.get('total_sources', 0)}")

    # 2. Most accessed
    top = report.get("top_accessed", [])
    if top:
        lines.append(f"\n### Most Accessed (Top {len(top)})")
        for item in top:
            preview = item["content"].replace("\n", " ")[:50]
            lines.append(f"  {item['access_count']}x — {item['id'][:8]}... {preview}")

    # 3. Access coverage
    ac = report.get("access_coverage", {})
    total = ac.get("total", 0)
    if total > 0:
        never = total - ac["accessed"]
        dead_pct = report.get("dead_memories_pct", 0)
        lines.append("\n### Access Coverage")
        lines.append(f"- Never accessed: {never}/{total} ({dead_pct:.0f}%)")
        lines.append(f"- Accessed at least once: {ac['accessed']}/{total} ({ac['pct']:.0f}%)")

    # 4. Tag coverage
    tc = report.get("tag_coverage", {})
    if tc.get("total", 0) > 0:
        untagged = tc["total"] - tc["tagged"]
        lines.append("\n### Tag Coverage")
        lines.append(f"- Tagged: {tc['tagged']}/{tc['total']} ({tc['pct']:.0f}%)")
        lines.append(f"- Untagged: {untagged}/{tc['total']} ({100 - tc['pct']:.0f}%)")

    # 5. Namespace distribution
    ns_dist = report.get("namespace_distribution", [])
    if ns_dist:
        lines.append("\n### Namespace Distribution")
        for ns in ns_dist:
            lines.append(f"  {ns['namespace']}: {ns['count']} chunks")

    # 6. Session activity
    sess = report.get("sessions", {})
    if sess.get("total", 0) > 0:
        lines.append("\n### Session Activity")
        lines.append(f"- Total sessions: {sess['total']}")
        lines.append(f"- Last 7 days: {sess.get('recent_7d', 0)}")

    # 7. Working memory
    wm = report.get("working_memory", {})
    if wm.get("total", 0) > 0:
        lines.append("\n### Working Memory")
        lines.append(f"- Active entries: {wm['total']}")
        lines.append(f"- Promoted to long-term: {wm['promoted']}")

    # 8. Cross-references
    xrefs = report.get("cross_references", 0)
    if xrefs > 0:
        lines.append("\n### Cross-References")
        lines.append(f"- Total links: {xrefs}")

    return "\n".join(lines)
