"""CLI: mm agent — multi-agent namespace management."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from memtomem.storage.sqlite_backend import SqliteBackend

_LEGACY_PREFIX = "agent/"
_CURRENT_PREFIX = "agent-runtime:"


@click.group()
def agent() -> None:
    """Multi-agent memory management commands."""


@agent.command("migrate")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print the planned renames without making changes.",
)
def migrate(dry_run: bool) -> None:
    """Rename legacy ``agent/{id}`` namespaces to ``agent-runtime:{id}``.

    Moves multi-agent namespaces from the pre-#318 format (``agent/{id}``)
    to the current ``agent-runtime:{id}`` format. Safe to re-run — rows that
    are already in the new format are left untouched.
    """
    asyncio.run(_run_migrate(dry_run=dry_run))


async def _run_migrate(dry_run: bool) -> None:
    from memtomem.cli._bootstrap import cli_components

    async with cli_components() as comp:
        mapping = await _collect_legacy_mapping(comp.storage)
        if not mapping:
            click.echo("No legacy `agent/` namespaces found. Nothing to migrate.")
            return

        click.echo(f"Legacy namespaces to migrate: {len(mapping)}")
        for old, new in mapping:
            click.echo(f"  {old}  ->  {new}")

        if dry_run:
            click.echo("\n(dry-run — no changes made. Re-run without --dry-run to apply.)")
            return

        total = 0
        for old, new in mapping:
            renamed = await comp.storage.rename_namespace(old, new)
            total += renamed
            click.echo(f"Renamed: {old}  ->  {new}  ({renamed} chunk(s))")

        click.echo(f"\nMigration complete. {len(mapping)} namespace(s), {total} chunk(s) updated.")


async def _collect_legacy_mapping(storage: SqliteBackend) -> list[tuple[str, str]]:
    """Return ``[(old, new), ...]`` pairs for namespaces needing migration."""
    pairs = await storage.list_namespaces()
    out: list[tuple[str, str]] = []
    for ns, _count in pairs:
        if not ns.startswith(_LEGACY_PREFIX):
            continue
        suffix = ns[len(_LEGACY_PREFIX) :]
        out.append((ns, f"{_CURRENT_PREFIX}{suffix}"))
    return out
