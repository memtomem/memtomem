"""CLI: mm reset — drop all data and reinitialize the database."""

from __future__ import annotations

import asyncio

import click


@click.command("reset")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
def reset(yes: bool) -> None:
    """Delete ALL data (chunks, sessions, history, etc.) and reinitialize the DB.

    Embedding configuration is preserved — no need to re-configure after reset.
    A re-index is required to repopulate memory.
    """
    asyncio.run(_run(yes))


async def _run(yes: bool) -> None:
    from memtomem.config import Mem2MemConfig, load_config_overrides
    from memtomem.storage.sqlite_backend import SqliteBackend

    cfg = Mem2MemConfig()
    load_config_overrides(cfg)

    storage = SqliteBackend(
        cfg.storage,
        dimension=cfg.embedding.dimension,
        embedding_provider=cfg.embedding.provider,
        embedding_model=cfg.embedding.model,
    )
    await storage.initialize()

    stats = await storage.get_stats()
    total = stats.get("total_chunks", 0)

    if total == 0:
        click.echo("Database is already empty — nothing to reset.")
        await storage.close()
        return

    if not yes:
        if not click.confirm(
            f"This will permanently delete ALL data ({total} chunks, sessions, "
            f"history, etc.) from the database. Continue?",
            default=False,
        ):
            click.echo("Cancelled.")
            await storage.close()
            return

    deleted = await storage.reset_all()
    await storage.close()

    click.secho("Database reset complete.", fg="green")
    for table, count in deleted.items():
        if count > 0:
            click.echo(f"  {table}: {count} rows deleted")
    click.echo("\nRun 'mm index <path>' to re-index your memories.")
