"""CLI: mm embedding-reset — resolve embedding model/dimension mismatches."""

from __future__ import annotations

import asyncio

import click


@click.command("embedding-reset")
@click.option(
    "--mode",
    type=click.Choice(["status", "apply-current", "revert-to-stored"]),
    default="status",
    help="status: show mismatch info, apply-current: reset DB (destructive), revert-to-stored: match DB",
)
def embedding_reset(mode: str) -> None:
    """Check or resolve embedding configuration mismatches.

    \b
    Modes:
      status          Show DB stored values vs current config (default)
      apply-current   Reset DB to current config — deletes all vectors, re-index required
      revert-to-stored  Switch runtime embedder to match DB stored values (non-destructive)
    """
    asyncio.run(_run(mode))


async def _run(mode: str) -> None:
    from memtomem.config import Mem2MemConfig, load_config_d, load_config_overrides
    from memtomem.storage.sqlite_backend import SqliteBackend

    cfg = Mem2MemConfig()
    load_config_d(cfg)
    load_config_overrides(cfg)

    storage = SqliteBackend(
        cfg.storage,
        dimension=cfg.embedding.dimension,
        embedding_provider=cfg.embedding.provider,
        embedding_model=cfg.embedding.model,
    )
    await storage.initialize()

    mismatch = getattr(storage, "embedding_mismatch", None)
    stored = getattr(storage, "stored_embedding_info", None)

    if mode == "status":
        click.echo(click.style("Embedding Status", bold=True))
        if stored:
            click.echo(
                f"  DB stored:  {stored['provider']}/{stored['model']} ({stored['dimension']}d)"
            )
        click.echo(
            f"  Config:     {cfg.embedding.provider}/{cfg.embedding.model} "
            f"({cfg.embedding.dimension}d)"
        )
        if mismatch is None:
            click.echo(click.style("\nNo mismatch — DB and config are in sync.", fg="green"))
        else:
            click.echo(click.style("\nMismatch detected!", fg="yellow"))
            click.echo(
                "  mm embedding-reset --mode apply-current    # reset DB (destructive, re-index needed)"
            )
            click.echo(
                "  mm embedding-reset --mode revert-to-stored # match DB settings (non-destructive)"
            )
        await storage.close()
        return

    # Convert CLI kebab-case to internal snake_case
    internal_mode = mode.replace("-", "_")

    if internal_mode == "apply_current":
        if not click.confirm(
            f"This will DELETE all vectors and reset DB to "
            f"{cfg.embedding.provider}/{cfg.embedding.model} ({cfg.embedding.dimension}d). "
            f"Re-indexing will be required. Continue?",
            default=False,
        ):
            click.echo("Cancelled.")
            await storage.close()
            return

        await storage.reset_embedding_meta(
            dimension=cfg.embedding.dimension,
            provider=cfg.embedding.provider,
            model=cfg.embedding.model,
        )
        click.echo(
            click.style(
                f"DB reset to {cfg.embedding.provider}/{cfg.embedding.model} "
                f"({cfg.embedding.dimension}d).",
                fg="green",
            )
        )
        click.echo("All vectors deleted — run 'mm index <path>' to re-index.")

    elif internal_mode == "revert_to_stored":
        if mismatch is None:
            click.echo("No mismatch — nothing to revert.")
        else:
            s = mismatch["stored"]
            click.echo(
                click.style(
                    f"Reverted runtime to DB settings: "
                    f"{s['provider']}/{s['model']} ({s['dimension']}d).",
                    fg="green",
                )
            )
            click.echo("Note: update your config to match if you want this to persist.")

    await storage.close()
