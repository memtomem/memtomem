"""``mm wiki`` — manage the local wiki (``~/.memtomem-wiki/``).

See ADR-0008 for the wiki layer's role in the context-gateway pipeline.
"""

from __future__ import annotations

import click

from memtomem.context._names import InvalidNameError
from memtomem.wiki import (
    WIKI_ASSET_TYPES,
    WikiAlreadyExistsError,
    WikiNotFoundError,
    WikiStore,
)
from memtomem.wiki.override import (
    OverrideExistsError,
    seed_override,
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


# ── Skill subgroup ──────────────────────────────────────────────────────


@wiki.group("skill")
def skill_group() -> None:
    """Per-skill operations on the wiki (override seeding, ...)."""


@skill_group.command("override")
@click.argument("name")
@click.option(
    "--vendor",
    "-v",
    type=click.Choice(["claude", "gemini", "codex"]),
    required=True,
    help="Which runtime this override targets.",
)
@click.option(
    "--force",
    "-f",
    is_flag=True,
    help="Overwrite existing override file in the wiki (creates .bak).",
)
@click.option(
    "--editor",
    "-e",
    is_flag=True,
    help="Open $EDITOR on the seeded file after writing.",
)
def skill_override_cmd(name: str, vendor: str, force: bool, editor: bool) -> None:
    """Seed a wiki override file from the canonical skill content.

    ``mm wiki skill override <name> --vendor <claude|gemini|codex>`` writes
    ``<wiki>/skills/<name>/overrides/<vendor>.md`` using the canonical
    ``SKILL.md`` as the working baseline. Edit the file (``--editor`` opens
    ``$EDITOR``) and commit it inside the wiki repo so that future
    ``mm context install`` snapshots pick it up.
    """
    store = WikiStore.at_default()
    try:
        target = seed_override(store, "skills", name, vendor, force=force)
    except WikiNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc
    except OverrideExistsError as exc:
        raise click.ClickException(str(exc)) from exc
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc
    except InvalidNameError as exc:
        raise click.ClickException(str(exc)) from exc

    rel = target.relative_to(store.root) if target.is_relative_to(store.root) else target
    click.secho(f"Seeded {rel}", fg="green")
    click.echo(str(target))
    click.echo(
        f"# next: cd {store.root} && git add skills/{name}/overrides/{vendor}.md && git commit"
    )

    if editor:
        click.edit(filename=str(target), require_save=False)
