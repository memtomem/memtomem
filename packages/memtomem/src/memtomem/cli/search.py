"""CLI: memtomem search <query>."""

from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from memtomem.models import SearchResult

import asyncio
import json

import click

from memtomem.cli._errors import raise_cli_error
from memtomem.services.search_service import run_search

# ADR-0011 §6: re-export the project-context resolver from the MCP tool so
# CLI and MCP surfaces share one resolution rule. Both ``app`` (server) and
# ``comp`` (CLI) carry the same ``.config.indexing.project_memory_dirs``
# field, so the helper is interchangeable.
from memtomem.server.tools.search import (
    _resolve_project_context_root as _resolve_project_context_root_from_cwd,
)

# ``\b`` is Click's raw-paragraph marker: without it Click rewraps the epilog as
# prose and the examples collapse into one run-on paragraph, which is exactly
# what makes them useless to copy-paste (#1667).
SEARCH_EPILOG = """\
Examples:

\b
  mm search "payment timeout"
  mm search "onboarding flow" --tag-filter onboarding --top-k 5
  mm search "incident" --scope project_shared --format context
"""


@click.command(epilog=SEARCH_EPILOG)
@click.argument("query")
@click.option("--top-k", "-k", default=10, help="Number of results")
@click.option("--source-filter", "-s", default=None, help="Source file filter")
@click.option("--tag-filter", "-t", default=None, help="Tag filter (comma-separated)")
@click.option("--namespace", "-n", default=None, help="Namespace filter")
@click.option(
    "--scope",
    default=None,
    help=(
        "Scope filter (ADR-0011): single value, comma list "
        "(``user,project_local``), or glob (``project_*``). When omitted, "
        "in-project searches return user + this project's tiers; out-of-"
        "project searches return user only."
    ),
)
@click.option(
    "--as-of",
    "as_of",
    default=None,
    help=(
        "Temporal bound (YYYY-MM-DD or YYYY-QN). Default = now. Filters "
        "validity windows and anchors time-decay scoring to that instant "
        "instead of the wall clock."
    ),
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["table", "json", "plain", "context", "smart"]),
    default="table",
    show_default=True,
    help=(
        "Output format: table (aligned columns), json (machine-readable), "
        "plain (bare source+content lines), context (markdown 'Relevant "
        "Memories' block ready to paste into a prompt), smart "
        "(namespace-grouped, relevance-tiered truncation)."
    ),
)
@click.option(
    "--no-rerank",
    "no_rerank",
    is_flag=True,
    default=False,
    help=(
        "Skip cross-encoder reranking for this query (faster, lower "
        "precision; otherwise follows server config)."
    ),
)
def search(
    query: str,
    top_k: int,
    source_filter: str | None,
    tag_filter: str | None,
    namespace: str | None,
    scope: str | None,
    as_of: str | None,
    fmt: str,
    no_rerank: bool,
) -> None:
    """Search the knowledge base."""
    try:
        asyncio.run(
            _search(
                query,
                top_k,
                source_filter,
                tag_filter,
                namespace,
                scope,
                as_of,
                fmt,
                rerank=False if no_rerank else None,
            )
        )
    except click.ClickException:
        raise
    except Exception as e:
        raise_cli_error(e)


async def _search(
    query: str,
    top_k: int,
    source_filter: str | None,
    tag_filter: str | None,
    namespace: str | None,
    scope: str | None,
    as_of: str | None,
    fmt: str,
    rerank: bool | None = None,
) -> None:
    from memtomem.cli._bootstrap import cli_components
    from memtomem.services.search_service import (
        InvalidTemporalBoundError,
        parse_as_of_bound,
    )

    # Validate before any component setup: a malformed bound must not pay for
    # opening the DB. ``parse_as_of_bound`` is exposed for exactly this, but
    # its message names the service argument, so re-word it with the CLI flag.
    try:
        parse_as_of_bound(as_of)
    except InvalidTemporalBoundError:
        raise click.ClickException(
            f"invalid --as-of value '{as_of}'. "
            "Accepted formats: 'YYYY-MM-DD' (date) or 'YYYY-QN' (quarter, N in 1-4)."
        ) from None

    async with cli_components() as comp:
        project_context_root = _resolve_project_context_root_from_cwd(comp)
        results, stats, hints = await run_search(
            comp.search_pipeline,
            query=query,
            top_k=top_k,
            source_filter=source_filter,
            tag_filter=tag_filter,
            namespace=namespace,
            current_namespace=None,
            as_of=as_of,
            scope=scope,
            project_context_root=project_context_root,
            rerank=rerank,
            origin="cli",
        )

    # Hints go to stderr for every format, and before any format-specific
    # return: ``context``/``smart`` return early on an empty result set, which
    # is exactly when a degradation notice matters most. stderr also keeps the
    # ``--format json`` payload a bare list, byte-compatible for pipes.
    for hint in hints:
        click.secho(f"({hint})", fg="yellow", err=True)

    if not results and fmt in ("table", "plain"):
        click.secho(
            "No results found. See `mm status` to confirm your index has chunks.",
            fg="yellow",
            err=True,
        )

    if fmt == "context":
        if not results:
            return
        lines = [f"## Relevant Memories (query: {query})", ""]
        for r in results:
            source = str(r.chunk.metadata.source_file)
            heading = (
                " > ".join(r.chunk.metadata.heading_hierarchy)
                if r.chunk.metadata.heading_hierarchy
                else ""
            )
            lines.append(f"### [{r.rank}] {heading or source} (score: {r.score:.3f})")
            lines.append(f"Source: {source}")
            lines.append("")
            lines.append(r.chunk.content.strip())
            lines.append("")
        click.echo("\n".join(lines))
        return

    if fmt == "smart":
        if not results:
            return
        # Group by namespace, show tags, adjust detail by relevance
        groups: dict[str, list[SearchResult]] = {}
        for r in results:
            ns = r.chunk.metadata.namespace or "default"
            groups.setdefault(ns, []).append(r)

        lines = [f"## Memory Context (query: {query})", ""]
        for ns, group in groups.items():
            lines.append(f"### [{ns}]")
            for r in group:
                source = str(r.chunk.metadata.source_file)
                heading = (
                    " > ".join(r.chunk.metadata.heading_hierarchy)
                    if r.chunk.metadata.heading_hierarchy
                    else ""
                )
                tags = ", ".join(r.chunk.metadata.tags) if r.chunk.metadata.tags else ""
                label = heading or source.split("/")[-1]
                tag_suffix = f" `{tags}`" if tags else ""

                # High relevance (top 3): full content; lower: truncated
                if r.rank <= 3:
                    content = r.chunk.content.strip()
                else:
                    content = r.chunk.content[:200].strip() + "..."

                lines.append(f"- **{label}** ({r.score:.2f}){tag_suffix}")
                lines.append(f"  {content}")
                lines.append("")
        click.echo("\n".join(lines))
        return

    if fmt == "json":
        # The payload is a bare list, so score provenance rides per item
        # (#1767): keys omitted when the pipeline produced no ranked scale.
        # ``reranker`` accompanies the "rerank" scale because rerank score
        # ranges are model-dependent — mirroring the MCP structured payload.
        # ``chunk_id`` uses the same key name and canonical UUID string as
        # the MCP structured payload (``server/formatters.py``) so a shell
        # pipeline can feed it straight to ``mm agent share`` (#2064).
        out = [
            {
                "rank": r.rank,
                "score": round(r.score, 4),
                **({"score_scale": stats.score_scale} if stats.score_scale is not None else {}),
                **({"reranker": stats.reranker_model} if stats.reranker_model is not None else {}),
                "source": str(r.chunk.metadata.source_file),
                "chunk_id": str(r.chunk.id),
                "content": r.chunk.content[:200],
            }
            for r in results
        ]
        click.echo(json.dumps(out, indent=2, ensure_ascii=False))
    elif fmt == "plain":
        for r in results:
            click.echo(f"[{r.rank}] {r.score:.4f} {r.chunk.metadata.source_file}")
            click.echo(r.chunk.content[:200])
            click.echo()
    else:
        # table
        click.echo(f"{'Rank':<6}{'Score':<10}{'Source':<40}{'Content'}")
        click.echo("-" * 80)
        for r in results:
            src = str(r.chunk.metadata.source_file)
            if len(src) > 38:
                src = "..." + src[-35:]
            snippet = r.chunk.content[:60].replace("\n", " ")
            click.echo(f"{r.rank:<6}{r.score:<10.4f}{src:<40}{snippet}")
        dense_note = " (suppressed: embedding mismatch)" if stats.dense_suppressed_mismatch else ""
        click.echo(
            f"\n{stats.bm25_candidates} BM25 + {stats.dense_candidates} dense{dense_note}"
            f" → {stats.final_total} results"
        )
