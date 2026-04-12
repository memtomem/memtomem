"""CLI: memtomem add / memtomem recall."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import click


@click.command()
@click.argument("content")
@click.option("--title", "-t", default=None, help="Entry title")
@click.option("--tags", default=None, help="Comma-separated tags")
@click.option(
    "--file", "file_name", default=None, help="Target file (relative to ~/.memtomem/memories/)"
)
def add(content: str, title: str | None, tags: str | None, file_name: str | None) -> None:
    """Add a memory entry and index it."""
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    try:
        asyncio.run(_add(content, title, tag_list, file_name))
    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(str(e)) from e


async def _add(content: str, title: str | None, tags: list[str], file_name: str | None) -> None:
    from memtomem.cli._bootstrap import cli_components
    from memtomem.tools.memory_writer import append_entry

    base = Path("~/.memtomem/memories").expanduser().resolve()
    if file_name:
        if file_name.startswith("/") or file_name.startswith("\\") or ".." in file_name:
            raise click.ClickException("File path must be relative and must not contain '..'")
        target = (base / file_name).resolve()
        try:
            target.relative_to(base)
        except ValueError:
            raise click.ClickException("File path escapes memory directory")
    else:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        target = (base / f"{date_str}.md").resolve()

    async with cli_components() as comp:
        target.parent.mkdir(parents=True, exist_ok=True)
        append_entry(target, content, title=title, tags=tags)
        stats = await comp.index_engine.index_file(target)

        # Apply tags to indexed chunks (chunker doesn't parse tag text from content)
        if tags and stats.indexed_chunks > 0:
            chunks = await comp.storage.list_chunks_by_source(target)
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
                await comp.storage.upsert_chunks(updated)

        click.echo(f"Added to {target} ({stats.indexed_chunks} chunks indexed)")


@click.command()
@click.option(
    "--since", default=None, help="Start date (YYYY, YYYY-MM, YYYY-MM-DD, or ISO datetime)"
)
@click.option("--until", default=None, help="End date (exclusive, same formats)")
@click.option("--limit", "-l", default=20, help="Number of recent chunks")
@click.option("--source-filter", "-s", default=None, help="Filter by source")
@click.option("--namespace", "-n", default=None, help="Namespace filter")
@click.option("--format", "fmt", type=click.Choice(["table", "json", "plain"]), default="table")
def recall(
    since: str | None,
    until: str | None,
    limit: int,
    source_filter: str | None,
    namespace: str | None,
    fmt: str,
) -> None:
    """Recall recent memory chunks."""
    try:
        asyncio.run(_recall(since, until, limit, source_filter, namespace, fmt))
    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(str(e)) from e


async def _recall(
    since: str | None,
    until: str | None,
    limit: int,
    source_filter: str | None,
    namespace: str | None,
    fmt: str,
) -> None:
    from memtomem.cli._bootstrap import cli_components
    from memtomem.models import NamespaceFilter
    from memtomem.server.helpers import _parse_recall_date

    since_dt = _parse_recall_date(since) if since else None
    until_dt = _parse_recall_date(until) if until else None

    async with cli_components() as comp:
        ns_filter = NamespaceFilter.parse(
            namespace,
            system_prefixes=tuple(comp.config.search.system_namespace_prefixes),
        )
        chunks = await comp.storage.recall_chunks(
            since=since_dt,
            until=until_dt,
            limit=limit,
            source_filter=source_filter,
            namespace_filter=ns_filter,
        )

    if fmt == "json":
        out = [
            {
                "id": str(c.id),
                "source": str(c.metadata.source_file),
                "content": c.content[:200],
                "created_at": c.created_at.isoformat(),
            }
            for c in chunks
        ]
        click.echo(json.dumps(out, indent=2, ensure_ascii=False))
    elif fmt == "plain":
        for c in chunks:
            click.echo(f"{c.metadata.source_file} ({c.created_at.isoformat()})")
            click.echo(c.content[:200])
            click.echo()
    else:
        click.echo(f"{'Source':<40}{'Created':<25}{'Content'}")
        click.echo("-" * 80)
        for c in chunks:
            src = str(c.metadata.source_file)
            if len(src) > 38:
                src = "..." + src[-35:]
            snippet = c.content[:40].replace("\n", " ")
            click.echo(f"{src:<40}{c.created_at.strftime('%Y-%m-%d %H:%M'):<25}{snippet}")
        click.echo(f"\n{len(chunks)} chunk(s)")
