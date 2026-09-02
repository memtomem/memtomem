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
@click.option(
    "--namespace",
    "-n",
    default=None,
    help=(
        "Namespace filter: single value, comma list (work,personal), or glob "
        "(proj:*) — the two spellings cannot be combined. When omitted, system "
        "namespaces (archive:*, agent-runtime:*) are hidden."
    ),
)
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
    from memtomem.models import (
        InvalidNamespaceFilterError,
        InvalidScopeFilterError,
        NamespaceFilter,
        ScopeFilter,
    )
    from memtomem.services.search_service import (
        InvalidTemporalBoundError,
        parse_as_of_bound,
        validate_scope_vocabulary,
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

    # Same reason, same re-wording: the parser's message names the API
    # argument, the user typed a flag.
    try:
        NamespaceFilter.parse(namespace)
    except InvalidNamespaceFilterError:
        raise click.ClickException(
            f"invalid --namespace value '{namespace}': a comma list and a glob "
            "cannot be combined. Use either '-n work,personal' or '-n proj:*', "
            "and run one query per pattern when you need several."
        ) from None
    # Two failures reach this catch now — a comma/glob mix and a value that is
    # not a tier — so the message has to be the exception's own. The previous
    # wording asserted the mix unconditionally and would have told someone who
    # typed ``--scope User`` to stop combining spellings they never combined.
    # ``validate_scope_vocabulary`` also normalizes, so the searched value is
    # kept apart from the typed one: ``run_search`` gets what was validated,
    # while the error above and the empty-result diagnostic below still quote
    # the command line as the user wrote it — ``--scope ""`` normalizes to no
    # filter at all, and a diagnostic that dropped it would leave the caller
    # looking for the option they actually typed.
    try:
        effective_scope = validate_scope_vocabulary(scope)
        ScopeFilter.parse(effective_scope)
    except InvalidScopeFilterError as e:
        raise click.ClickException(f"invalid --scope value '{scope}': {e}") from None

    async with cli_components() as comp:
        await _search_with_components(
            comp,
            query=query,
            top_k=top_k,
            source_filter=source_filter,
            tag_filter=tag_filter,
            namespace=namespace,
            scope=effective_scope,
            typed_scope=scope,
            as_of=as_of,
            fmt=fmt,
            rerank=rerank,
        )


async def _search_with_components(
    comp,
    *,
    query: str,
    top_k: int,
    source_filter: str | None,
    tag_filter: str | None,
    namespace: str | None,
    scope: str | None,
    as_of: str | None,
    fmt: str,
    typed_scope: str | None = None,
    rerank: bool | None = None,
) -> None:
    """Run one search against already-open components and render it.

    Split out of :func:`_search` so a sibling verb that has to resolve
    something from the store first — ``mm agent search`` reads the active
    session's agent binding — reuses this rendering instead of opening a
    second set of components or growing a third copy of the formats
    (``mm search`` and the interactive shell already have one each). The
    caller owns validation; by the time this runs, the flags parsed.

    ``scope`` is the value the search *runs with* — normalized by
    ``validate_scope_vocabulary`` — while ``typed_scope`` is what the command
    line carried. The empty-result diagnostic quotes the latter, so a caller
    who typed ``--scope User`` is not shown the ``user`` it was folded to.
    """
    from memtomem.cli._empty_results import explain_empty_result

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
    # Built inside the block: naming the filter that emptied the result
    # needs the store, and ``cli_components`` has closed it by the time
    # the message is printed (#2255). Gated on the format that prints it
    # so ``--format json`` keeps its bare ``[]`` — and cannot start
    # failing on a store read whose answer it would never show.
    # ``run_search`` resolves the namespace as ``namespace or
    # current_namespace``, so an empty one is no namespace at all — the
    # branch that names a namespace as the cause must see what the query
    # saw. ``filters`` is separate: it reports the command line as typed,
    # claiming nothing about which option narrowed anything.
    effective_namespace = namespace or None
    empty_message = (
        await explain_empty_result(
            comp.storage,
            namespace=effective_namespace,
            filters=[
                (flag, value)
                for flag, value in (
                    ("--source-filter", source_filter),
                    ("--tag-filter", tag_filter),
                    ("--namespace", namespace),
                    ("--scope", typed_scope),
                    ("--as-of", as_of),
                )
                if value is not None
            ],
            count_flag="-k",
        )
        if not results and fmt in ("table", "plain")
        else ""
    )

    # Hints go to stderr for every format, and before any format-specific
    # return: ``context``/``smart`` return early on an empty result set, which
    # is exactly when a degradation notice matters most. stderr also keeps the
    # ``--format json`` payload a bare list, byte-compatible for pipes.
    for hint in hints:
        click.secho(f"({hint})", fg="yellow", err=True)

    if not results and fmt in ("table", "plain"):
        click.secho(empty_message, fg="yellow", err=True)

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
