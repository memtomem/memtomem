"""``mm wiki`` — manage the local wiki (``~/.memtomem-wiki/``).

See ADR-0008 for the wiki layer's role in the context-gateway pipeline.
"""

from __future__ import annotations

import click

from memtomem.wiki import (
    WIKI_ASSET_TYPES,
    WikiAlreadyExistsError,
    WikiNotFoundError,
    WikiStore,
)


@click.group("wiki")
def wiki() -> None:
    """Manage the local memtomem wiki (skills/agents/commands library)."""


@wiki.command("init")
@click.option(
    "--from",
    "from_url",
    metavar="GIT_URL",
    default=None,
    help="Clone the wiki from a git URL instead of initializing from scratch.",
)
def init_cmd(from_url: str | None) -> None:
    """Create or clone the wiki at ``~/.memtomem-wiki/``."""
    store = WikiStore.at_default()
    try:
        if from_url:
            store.init_from_url(from_url)
            click.secho(f"Cloned wiki from {from_url} → {store.root}", fg="green")
        else:
            store.init()
            click.secho(f"Initialized wiki at {store.root}", fg="green")
            click.echo("  Layout: skills/, agents/, commands/")
            click.echo("  Run `mm wiki list` or `mm wiki --help` to see what is available.")
    except WikiAlreadyExistsError as exc:
        raise click.ClickException(str(exc)) from exc
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc


@wiki.command("list")
@click.option(
    "--type",
    "asset_type",
    type=click.Choice(WIKI_ASSET_TYPES),
    default=None,
    help="Restrict output to one asset kind.",
)
def list_cmd(asset_type: str | None) -> None:
    """List skills, agents, and commands in the wiki."""
    store = WikiStore.at_default()
    try:
        assets = store.list_assets(asset_type)
    except WikiNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc

    if not assets:
        scope = asset_type or "any kind"
        click.echo(f"  (no assets of {scope})")
        return

    click.secho(f"Wiki: {store.root}", fg="cyan")
    click.echo(f"  HEAD: {store.current_commit()[:12]}")
    click.echo("")
    last_type: str | None = None
    for asset in assets:
        if asset.type != last_type:
            click.secho(f"  {asset.type}/", fg="cyan")
            last_type = asset.type
        click.echo(f"    {asset.name}")
