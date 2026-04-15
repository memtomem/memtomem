"""Tool: mem_fetch — fetch a URL and index its content."""

from __future__ import annotations

from pathlib import Path

from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register


@mcp.tool()
@tool_handler
@register("importers")
async def mem_fetch(
    url: str,
    tags: list[str] | None = None,
    namespace: str | None = None,
    ctx: CtxType = None,
) -> str:
    """Fetch a URL, convert to markdown, and index it for search.

    Supports HTML pages (converted to markdown), plain text, and raw content.
    The fetched content is saved as a .md file in the first memory directory
    and immediately indexed.

    Args:
        url: The URL to fetch and index
        tags: Optional tags to apply to indexed chunks
        namespace: Namespace for indexed chunks (default: config default)
    """
    from memtomem.indexing.url_fetcher import fetch_url

    if not url.startswith(("http://", "https://")):
        return "Error: URL must start with http:// or https://"

    app = _get_app(ctx)
    if not app.config.indexing.memory_dirs:
        return "Error: no memory directories configured. Run 'mm init' first."
    memory_dir = Path(app.config.indexing.memory_dirs[0]).expanduser().resolve()
    output_dir = memory_dir / "_fetched"

    try:
        file_path = await fetch_url(url, output_dir)
    except Exception as exc:
        return f"Error fetching URL: {exc}"

    # Index the fetched file
    effective_ns = namespace or app.current_namespace
    stats = await app.index_engine.index_file(file_path, namespace=effective_ns)

    # Apply tags if provided
    if tags and stats.indexed_chunks > 0:
        chunks = await app.storage.list_chunks_by_source(file_path)
        updated = []
        for c in chunks:
            merged = set(c.metadata.tags) | set(tags)
            if merged != set(c.metadata.tags):
                c.metadata = c.metadata.__class__(
                    **{
                        **{f: getattr(c.metadata, f) for f in c.metadata.__dataclass_fields__},
                        "tags": tuple(sorted(merged)),
                    }
                )
                updated.append(c)
        if updated:
            await app.storage.upsert_chunks(updated)

    app.search_pipeline.invalidate_cache()

    return (
        f"Fetched and indexed: {url}\n"
        f"- Saved to: {file_path}\n"
        f"- Chunks indexed: {stats.indexed_chunks}"
    )
