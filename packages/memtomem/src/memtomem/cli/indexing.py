"""CLI: memtomem index <path>."""

from __future__ import annotations

import asyncio
from pathlib import Path

import click


@click.command()
@click.argument("path", default=".")
@click.option("--recursive/--no-recursive", default=True, help="Recurse into subdirectories")
@click.option("--force", is_flag=True, help="Force re-index (ignore hashes)")
@click.option("--namespace", "-n", default=None, help="Target namespace")
def index(path: str, recursive: bool, force: bool, namespace: str | None) -> None:
    """Index files at PATH into the knowledge base."""
    try:
        asyncio.run(_index(path, recursive, force, namespace))
    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(str(e)) from e


async def _index(path: str, recursive: bool, force: bool, namespace: str | None) -> None:
    from memtomem.cli._bootstrap import cli_components

    async with cli_components() as comp:
        resolved = Path(path).resolve()
        stats = await comp.index_engine.index_path(
            resolved,
            recursive=recursive,
            force=force,
            namespace=namespace,
        )

    click.echo(
        f"Indexed {stats.total_files} file(s): "
        f"{stats.indexed_chunks} new, {stats.skipped_chunks} unchanged, "
        f"{stats.deleted_chunks} deleted ({stats.duration_ms:.0f}ms)"
    )
    if stats.errors:
        for err in stats.errors:
            click.echo(click.style(f"  ERROR: {err}", fg="red"))
