"""``mm wiki`` — manage the local wiki (``~/.memtomem-wiki/``).

See ADR-0008 for the wiki layer's role in the context-gateway pipeline.
"""

from __future__ import annotations

from typing import Literal

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


def _run_seed_override(
    asset_type: Literal["skills", "agents", "commands"],
    name: str,
    vendor: str,
    *,
    force: bool,
    editor: bool,
) -> None:
    """Shared body for ``mm wiki {skill,agent,command} override``.

    Mirrors the seed → stdout summary → optional stderr warning →
    optional ``$EDITOR`` flow across all three asset types so the trust-UX
    is identical: classified ClickException for known errors, no Python
    traceback leaks, and any vendor-renderer drops surface as a yellow
    stderr line so the user knows what the runtime won't see in the
    override.
    """
    store = WikiStore.at_default()
    try:
        result = seed_override(store, asset_type, name, vendor, force=force)
    except (
        WikiNotFoundError,
        OverrideExistsError,
        FileNotFoundError,
        InvalidNameError,
        NotImplementedError,
    ) as exc:
        # 5 sibling classes (verified disjoint: WikiNotFoundError /
        # OverrideExistsError / NotImplementedError -> RuntimeError;
        # FileNotFoundError -> OSError; InvalidNameError -> ValueError —
        # no cross-inheritance, ordering irrelevant). NotImplementedError
        # carries the ("commands", "codex") placeholder message from
        # seed_override; surfacing it as ClickException prints a classified
        # error rather than a Python traceback.
        raise click.ClickException(str(exc)) from exc

    # ``seed_override`` invariant: target lives under ``store.root``.
    # No is_relative_to fallback — a violation is a real bug worth
    # surfacing as ValueError, not a silent path mismatch to mask.
    rel = result.path.relative_to(store.root)
    ext = result.path.suffix.lstrip(".")
    # ``as_posix()`` matches the hardcoded ``/`` in the ``git add`` hint below.
    click.secho(f"Seeded {rel.as_posix()}", fg="green")
    click.echo(str(result.path))
    click.echo(
        f"# next: cd {store.root} && "
        f"git add {asset_type}/{name}/overrides/{vendor}.{ext} && git commit"
    )

    if result.dropped:
        click.secho(
            f"warning: vendor {vendor!r} will not represent these fields: "
            f"{', '.join(result.dropped)}",
            fg="yellow",
            err=True,
        )

    if editor:
        click.edit(filename=str(result.path), require_save=False)


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
    _run_seed_override("skills", name, vendor, force=force, editor=editor)


# ── Agent subgroup ──────────────────────────────────────────────────────


@wiki.group("agent")
def agent_group() -> None:
    """Per-agent operations on the wiki (override seeding, ...)."""


@agent_group.command("override")
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
def agent_override_cmd(name: str, vendor: str, force: bool, editor: bool) -> None:
    """Seed a wiki override file from the canonical agent content.

    ``mm wiki agent override <name> --vendor <claude|gemini|codex>`` writes
    ``<wiki>/agents/<name>/overrides/<vendor>.<ext>``. Bytes come from the
    vendor renderer applied to the canonical ``agent.md`` so the seed
    matches what the runtime would produce. Fields the vendor format
    cannot represent (e.g. gemini agents drop ``skills`` / ``isolation``)
    are surfaced via a stderr warning so the editor knows what the
    runtime won't see.
    """
    _run_seed_override("agents", name, vendor, force=force, editor=editor)


# ── Command subgroup ────────────────────────────────────────────────────


@wiki.group("command")
def command_group() -> None:
    """Per-command operations on the wiki (override seeding, ...)."""


@command_group.command("override")
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
def command_override_cmd(name: str, vendor: str, force: bool, editor: bool) -> None:
    """Seed a wiki override file from the canonical command content.

    ``mm wiki command override <name> --vendor <claude|gemini|codex>`` writes
    ``<wiki>/commands/<name>/overrides/<vendor>.<ext>``. ``--vendor codex``
    is a permanent placeholder (no ``codex_commands`` generator); the
    command surfaces a classified error rather than silently failing.
    Fields the vendor format cannot represent (e.g. gemini commands drop
    ``argument-hint`` / ``allowed-tools`` / ``model``) are surfaced via a
    stderr warning so the editor knows what the runtime won't see.
    """
    _run_seed_override("commands", name, vendor, force=force, editor=editor)
