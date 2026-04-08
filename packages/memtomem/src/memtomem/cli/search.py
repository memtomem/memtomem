"""CLI: memtomem search <query>."""

from __future__ import annotations

import asyncio
import json

import click


@click.command()
@click.argument("query")
@click.option("--top-k", "-k", default=10, help="Number of results")
@click.option("--source-filter", "-s", default=None, help="Source file filter")
@click.option("--tag-filter", "-t", default=None, help="Tag filter (comma-separated)")
@click.option("--namespace", "-n", default=None, help="Namespace filter")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["table", "json", "plain", "context", "smart"]),
    default="table",
)
def search(
    query: str,
    top_k: int,
    source_filter: str | None,
    tag_filter: str | None,
    namespace: str | None,
    fmt: str,
) -> None:
    """Search the knowledge base."""
    try:
        asyncio.run(_search(query, top_k, source_filter, tag_filter, namespace, fmt))
    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(str(e)) from e


async def _search(
    query: str,
    top_k: int,
    source_filter: str | None,
    tag_filter: str | None,
    namespace: str | None,
    fmt: str,
) -> None:
    from memtomem.cli._bootstrap import cli_components

    async with cli_components() as comp:
        results, stats = await comp.search_pipeline.search(
            query,
            top_k=top_k,
            source_filter=source_filter,
            tag_filter=tag_filter,
            namespace=namespace,
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
        groups: dict[str, list] = {}
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
        out = [
            {
                "rank": r.rank,
                "score": round(r.score, 4),
                "source": str(r.chunk.metadata.source_file),
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
        click.echo(
            f"\n{stats.bm25_candidates} BM25 + {stats.dense_candidates} dense → {stats.final_total} results"
        )
