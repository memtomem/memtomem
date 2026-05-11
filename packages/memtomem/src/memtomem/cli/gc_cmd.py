"""CLI: ``mm gc`` — storage maintenance commands (ADR-0011 follow-up #884).

Currently exposes one subcommand:

* ``mm gc orphan-projects`` — find chunks whose recorded ``project_root``
  no longer exists on disk and, with ``--apply``, delete them. Default
  is dry-run; ``--apply`` requires interactive confirmation;
  ``--apply --yes`` is the non-interactive form.

Placed at the top level (not under ``mm context``) because the rows it
cleans live in user-local storage, not the Context Gateway artifact
tree. The ``gc`` group is the natural home for any future storage
maintenance subcommands (#884 review point 1).
"""

from __future__ import annotations

import asyncio

import click


@click.group("gc")
def gc() -> None:
    """Storage maintenance: garbage-collect stale rows in the user-local DB."""


@gc.command("orphan-projects")
@click.option(
    "--apply",
    "apply_",
    is_flag=True,
    help="Actually delete. Without this flag, prints what would be deleted (dry-run).",
)
@click.option(
    "--yes",
    "assume_yes",
    is_flag=True,
    help="Skip the per-root confirmation prompt. Requires --apply.",
)
def orphan_projects(apply_: bool, assume_yes: bool) -> None:
    """Remove chunks whose ``project_root`` directory no longer exists.

    When you index a folder under ``--scope project_shared`` or
    ``project_local`` and later delete the folder, the rows survive in
    ``~/.memtomem/memtomem.db`` with a now-vanished ``project_root``.
    They are hidden by the default in-project search filter but bloat
    the DB and surface under explicit cross-project queries
    (``--scope=project_shared`` from outside any project). This command
    detects and removes them.

    Removable disks and unmounted filesystems will be reported as
    orphaned. The per-root confirmation prompt (``--apply`` without
    ``--yes``) is the safety gate — answer ``n`` for any root whose
    disk you intend to plug back in.
    """
    if assume_yes and not apply_:
        raise click.UsageError("--yes requires --apply.")
    asyncio.run(_run(apply_=apply_, assume_yes=assume_yes))


async def _run(*, apply_: bool, assume_yes: bool) -> None:
    from memtomem.cli._bootstrap import cli_components

    async with cli_components() as comp:
        reports = await comp.storage.find_orphan_project_roots()

        if not reports:
            click.secho("No orphan project_root entries found.", fg="green")
            return

        total_rows = sum(r.total_rows for r in reports)
        click.echo(
            f"Found {len(reports)} orphan project_root "
            f"{'entry' if len(reports) == 1 else 'entries'} "
            f"({total_rows} total chunks):"
        )
        for report in reports:
            _print_report(report)

        if not apply_:
            click.echo("\nRun with --apply to delete (per-root confirmation).")
            click.echo("Run with --apply --yes for non-interactive deletion.")
            return

        deleted_chunks = 0
        deleted_roots = 0
        for report in reports:
            if not assume_yes:
                if not click.confirm(
                    f"Delete {report.total_rows} chunks under {report.project_root}?",
                    default=False,
                ):
                    click.echo(f"  skipped: {report.project_root}")
                    continue
            result = await comp.storage.sweep_orphan_project_root(report.project_root)
            click.echo(
                f"  deleted: {result.chunks_deleted} chunks, "
                f"{result.fts_deleted} fts rows, "
                f"{result.vec_deleted} vec rows, "
                f"{result.ai_summaries_deleted} ai_summary entries "
                f"({report.project_root})"
            )
            deleted_chunks += result.chunks_deleted
            deleted_roots += 1

        click.secho(
            f"\nDone: {deleted_chunks} chunks across {deleted_roots} project root"
            f"{'s' if deleted_roots != 1 else ''}.",
            fg="green",
        )


def _print_report(report) -> None:  # type: ignore[no-untyped-def]
    """Render one ``OrphanProjectReport`` to stdout."""
    scope_summary = ", ".join(
        f"{scope}={count}" for scope, count in sorted(report.scope_counts.items())
    )
    click.echo(f"\n  {report.project_root}")
    click.echo(f"    {report.total_rows} chunks ({scope_summary})")
    if report.sample_source_files:
        click.echo("    sample sources:")
        for src in report.sample_source_files:
            click.echo(f"      - {src}")
