"""Tool: mem_ask — memory-based Q&A with LLM answer generation."""

from __future__ import annotations

import logging

from memtomem.models import InvalidFilterSyntaxError, ScopeFilter
from memtomem.runtime.project_context import _resolve_project_context_root
from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app_initialized
from memtomem.server.error_handler import tool_handler
from memtomem.server.helpers import _announce_dim_mismatch_once
from memtomem.server.tool_registry import register
from memtomem.server.validation import MAX_QUERY_LENGTH
from memtomem.services.search_service import (
    InvalidTemporalBoundError,
    parse_as_of_bound,
    run_search,
)

logger = logging.getLogger(__name__)


@mcp.tool()
@tool_handler
@register("search")
async def mem_ask(
    question: str,
    top_k: int = 5,
    namespace: str | None = None,
    source_filter: str | None = None,
    tag_filter: str | None = None,
    as_of: str | None = None,
    scope: str | None = None,
    ctx: CtxType = None,
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
        as_of: Temporal bound for retroactive search — ``YYYY-MM-DD`` or
            ``YYYY-QN``, default now. Chunks whose ``valid_from`` /
            ``valid_to`` frontmatter excludes that point drop out; chunks
            without those keys are always valid. Time decay anchors here,
            not to the wall clock.
        scope: ADR-0011 tier filter — value, comma list (``user,project_local``)
            or glob (``project_*``), not both. Omitted, the default merge
            applies: inside a project ``user`` + that project's tiers, outside
            one ``user`` only. Pass ``project_shared`` from outside a project to
            search across projects.
    """
    if not question.strip():
        return "Error: question cannot be empty."
    if len(question) > MAX_QUERY_LENGTH:
        return f"Error: question too long (max 10,000 characters, got {len(question)})."
    if not 1 <= top_k <= 20:
        return f"Error: top_k must be between 1 and 20, got {top_k}."

    # Reject malformed arguments before touching the app, the way mem_search
    # does — validation here runs without an initialized server, and the core
    # re-derives both from the raw values so there is one definition of what
    # is accepted.
    try:
        parse_as_of_bound(as_of)
        ScopeFilter.parse(scope)
    except (InvalidTemporalBoundError, InvalidFilterSyntaxError) as e:
        return f"Error: {e}"

    app = await _get_app_initialized(ctx)

    # ADR-0011 PR-D round 9: same project-context threading mem_search
    # uses, so mem_ask in a registered project does not silently lose
    # project-tier rows on the always-on scope filter.
    project_context_root = _resolve_project_context_root(app)

    # ``run_search`` owns the ``as_of`` parse and the
    # ``namespace or current_namespace`` fallback this tool used to hand-roll.
    # Its result-derived hints are rendered below: a grounded prompt built
    # from a keyword-only pool reads exactly like one from a healthy hybrid
    # pool, so dropping the degradation notice would hide the one signal that
    # tells the answer apart.
    results, stats, hints = await run_search(
        app.search_pipeline,
        query=question,
        top_k=top_k,
        source_filter=source_filter,
        tag_filter=tag_filter,
        namespace=namespace,
        current_namespace=app.current_namespace,
        as_of=as_of,
        scope=scope,
        project_context_root=project_context_root,
        origin="mcp",
    )

    # The dimension-mismatch notice is per-process announcement state, not a
    # property of this query, so it is appended here rather than in the core.
    # Skipped when this query already carries the per-search degradation hint
    # (#2063): that hint names the same dimensions and the same fix, so
    # emitting both duplicates one notice. The announce flag is deliberately
    # left unconsumed in that branch, so mem_add / mem_recall still get their
    # one-shot on the write side.
    if not stats.dense_suppressed_mismatch:
        dim_notice = await _announce_dim_mismatch_once(app)
        if dim_notice:
            hints.append(dim_notice)

    if not results:
        # Hints matter most on an empty result set, and ``hints`` can carry the
        # one-shot dimension notice this call already consumed — returning
        # without it would destroy the only announcement the process was going
        # to make. Rendered exactly as mem_search renders its empty-result tail.
        tail = "\n\n" + "\n".join(f"({h})" for h in hints) if hints else ""
        return (
            f'No relevant memories found for: "{question}"\n\n'
            "Try broader keywords, check `mem_status` for indexing state, "
            "or add relevant notes with `mem_add`." + tail
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

    # Fire webhook. ``fire`` only builds the request and hands it to a task the
    # manager tracks, so awaiting it costs nothing and drops the untracked outer
    # task that could otherwise fire after ``close()`` (#2185).
    if app.webhook_manager:
        await app.webhook_manager.fire(
            "ask",
            {
                "question": question,
                "context_chunks": len(results),
            },
        )

    output = "\n".join(lines)
    for hint in hints:
        output += f"\n\n({hint})"

    return output
