"""Tool: mem_ask — memory-based Q&A with LLM answer generation."""

from __future__ import annotations

import logging

from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app
from memtomem.server.error_handler import tool_handler

logger = logging.getLogger(__name__)


def _webhook_error_cb(task: "asyncio.Task") -> None:  # noqa: F821
    """Log errors from fire-and-forget webhook tasks."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        logger.warning("Webhook fire failed: %s", exc)


@mcp.tool()
@tool_handler
async def mem_ask(
    question: str,
    top_k: int = 5,
    namespace: str | None = None,
    source_filter: str | None = None,
    tag_filter: str | None = None,
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Ask a question and get an answer grounded in your memories.

    Searches your indexed memories for relevant context, then presents
    the question with supporting evidence so the AI can synthesize
    an informed answer.

    Unlike mem_search which returns raw chunks, mem_ask structures the
    results as a Q&A prompt with cited sources.

    Args:
        question: The question to answer from your memories.
        top_k: Number of memory chunks to use as context (default 5).
        namespace: Scope to a specific namespace.
        source_filter: Filter by source file path.
        tag_filter: Filter by tags (comma-separated, OR logic).
    """
    if len(question) > 10_000:
        return "Error: question too long (max 10,000 characters)."
    if not 1 <= top_k <= 20:
        return "Error: top_k must be between 1 and 20."

    app = _get_app(ctx)
    effective_ns = namespace or app.current_namespace

    results, stats = await app.search_pipeline.search(
        query=question,
        top_k=top_k,
        source_filter=source_filter,
        tag_filter=tag_filter,
        namespace=effective_ns,
    )

    if not results:
        return (
            f'No relevant memories found for: "{question}"\n\n'
            "Try broader keywords, check `mem_status` for indexing state, "
            "or add relevant notes with `mem_add`."
        )

    # Build grounded Q&A context
    lines = [
        f"## Question: {question}",
        "",
        "## Relevant Memories",
        "",
    ]

    sources_cited = []
    for r in results:
        source = str(r.chunk.metadata.source_file)
        heading = (
            " > ".join(r.chunk.metadata.heading_hierarchy)
            if r.chunk.metadata.heading_hierarchy
            else ""
        )
        label = heading or source.split("/")[-1]
        tags = ", ".join(r.chunk.metadata.tags) if r.chunk.metadata.tags else ""

        lines.append(f"### [{r.rank}] {label} (relevance: {r.score:.2f})")
        if tags:
            lines.append(f"Tags: {tags}")
        lines.append(f"Source: {source}")
        lines.append("")
        lines.append(r.chunk.content.strip())
        lines.append("")

        sources_cited.append(f"[{r.rank}] {label} ({source})")

    lines.append("---")
    lines.append("")
    lines.append("## Instructions")
    lines.append("")
    lines.append(f'Answer the question "{question}" based on the memories above.')
    lines.append("Cite sources by their rank number [1], [2], etc.")
    lines.append("If the memories don't contain enough information, say so.")
    lines.append("")
    lines.append("## Sources")
    for s in sources_cited:
        lines.append(f"- {s}")

    # Fire webhook
    if app.webhook_manager:
        import asyncio

        task = asyncio.create_task(
            app.webhook_manager.fire(
                "ask",
                {
                    "question": question,
                    "context_chunks": len(results),
                },
            )
        )
        task.add_done_callback(_webhook_error_cb)

    return "\n".join(lines)
