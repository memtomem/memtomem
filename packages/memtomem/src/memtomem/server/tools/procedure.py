"""Tools: mem_procedure_save, mem_procedure_list."""

from __future__ import annotations

from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register


@mcp.tool()
@tool_handler
@register("procedures")
async def mem_procedure_save(
    name: str,
    steps: str,
    trigger: str | None = None,
    tags: list[str] | None = None,
    namespace: str | None = None,
    ctx: CtxType = None,
) -> str:
    """Save a reusable procedure (workflow/pattern) to memory.

    Procedures capture "how to do something" — successful workflows,
    tool usage patterns, or step-by-step processes for future reuse.

    Args:
        name: Name of the procedure (e.g. "deploy-to-production")
        steps: Step-by-step instructions (numbered list recommended)
        trigger: When to use this procedure (e.g. "when deploying a new version")
        tags: Additional tags (procedure tag is added automatically)
        namespace: Namespace for the procedure
    """
    from memtomem.server.tools.memory_crud import mem_add

    all_tags = list(tags or [])
    if "procedure" not in all_tags:
        all_tags.append("procedure")

    content_parts = [f"**Trigger**: {trigger or '(not specified)'}"]
    content_parts.append(f"**Steps**:\n{steps}")

    content = "\n".join(content_parts)

    return await mem_add(
        content=content,
        title=f"Procedure: {name}",
        tags=all_tags,
        file=f"procedures/{name.replace(' ', '-').lower()}.md",
        namespace=namespace,
        ctx=ctx,
    )


@mcp.tool()
@tool_handler
@register("procedures")
async def mem_procedure_list(
    ctx: CtxType = None,
) -> str:
    """List all saved procedures."""
    app = _get_app(ctx)

    # Find all chunks tagged with "procedure"
    results, _ = await app.search_pipeline.search(
        query="procedure workflow steps",
        top_k=50,
        tag_filter="procedure",
    )

    if not results:
        return "No procedures found."

    # Deduplicate by source file
    seen_sources: set[str] = set()
    lines = [f"Procedures: {len(results)} found\n"]
    for r in results:
        source = str(r.chunk.metadata.source_file)
        if source in seen_sources:
            continue
        seen_sources.add(source)
        heading = (
            " > ".join(r.chunk.metadata.heading_hierarchy)
            if r.chunk.metadata.heading_hierarchy
            else source.split("/")[-1]
        )
        tags = ", ".join(t for t in r.chunk.metadata.tags if t != "procedure")
        tag_suffix = f" [{tags}]" if tags else ""
        lines.append(f"  {heading}{tag_suffix}")

    return "\n".join(lines)
