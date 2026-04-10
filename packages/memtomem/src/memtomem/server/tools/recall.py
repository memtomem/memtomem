"""Tool: mem_recall."""

from __future__ import annotations

from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app
from memtomem.server.error_handler import tool_handler
from memtomem.server.formatters import _display_path
from memtomem.server.helpers import _parse_recall_date


@mcp.tool()
@tool_handler
async def mem_recall(
    since: str | None = None,
    until: str | None = None,
    source_filter: str | None = None,
    namespace: str | None = None,
    limit: int = 20,
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Recall memories created within a date range.

    Returns chunks ordered by creation date (newest first).

    Args:
        since: Inclusive start date (YYYY, YYYY-MM, YYYY-MM-DD, or ISO datetime)
        until: Exclusive end date — same formats as *since*
        source_filter: Filter by source file path (substring match, or glob pattern with *, ?, [])
        namespace: Namespace scope — single, comma-separated, or glob (e.g. "project:*")
        limit: Maximum number of chunks to return (default 20)

    Examples::

        mem_recall(since="2025-01", until="2025-03")
        mem_recall(since="2025-06-01", source_filter="notes")
        mem_recall(namespace="work", limit=10)
    """
    if not 1 <= limit <= 500:
        return "Error: limit must be between 1 and 500."

    from memtomem.models import NamespaceFilter

    app = _get_app(ctx)

    try:
        since_dt = _parse_recall_date(since) if since else None
        until_dt = _parse_recall_date(until, end_of_period=True) if until else None
    except ValueError as exc:
        return f"Error: {exc}"

    if since_dt and until_dt and since_dt >= until_dt:
        return "Error: 'since' must be earlier than 'until'."

    effective_ns = namespace or app.current_namespace
    ns_filter = NamespaceFilter.parse(effective_ns)
    chunks = await app.storage.recall_chunks(
        since=since_dt,
        until=until_dt,
        source_filter=source_filter,
        limit=limit,
        namespace_filter=ns_filter,
    )

    if not chunks:
        filters = []
        if since:
            filters.append(f"since={since}")
        if until:
            filters.append(f"until={until}")
        if source_filter:
            filters.append(f"source={source_filter!r}")
        if effective_ns:
            filters.append(f"namespace={effective_ns!r}")
        suffix = f" ({', '.join(filters)})" if filters else ""
        return f"No memories found{suffix}."

    parts: list[str] = []
    for i, chunk in enumerate(chunks, 1):
        meta = chunk.metadata
        date_str = chunk.created_at.strftime("%Y-%m-%d")
        hierarchy = " > ".join(meta.heading_hierarchy) if meta.heading_hierarchy else ""
        tags_str = f"  tags: {', '.join(meta.tags)}" if meta.tags else ""
        ns_badge = f" [{meta.namespace}]" if meta.namespace != "default" else ""
        parts.append(
            f"**[{i}]** {date_str} |{ns_badge} {_display_path(meta.source_file)}"
            + (f" | {hierarchy}" if hierarchy else "")
            + tags_str
            + f"\n```\n{chunk.content[:400]}\n```"
        )

    header = f"Found {len(chunks)} memor{'y' if len(chunks) == 1 else 'ies'}"
    if since or until:
        header += f" ({since or '…'} → {until or 'now'})"
    return header + ":\n\n" + "\n\n".join(parts)
