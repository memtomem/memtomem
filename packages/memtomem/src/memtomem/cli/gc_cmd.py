"""CLI: ``mm gc`` — storage maintenance commands (ADR-0011 follow-up #884).

Exposes two subcommands:

* ``mm gc orphan-projects`` — find chunks whose recorded ``project_root``
  no longer exists on disk and, with ``--apply``, delete them. Default
  is dry-run; ``--apply`` requires interactive confirmation;
  ``--apply --yes`` is the non-interactive form.
* ``mm gc orphan-sources`` — find indexed source files that no longer exist
  and optionally remove their chunks, confirming per owning memory dir.
  This is the terminal equivalent of ``mem_cleanup_orphans``.

Placed at the top level (not under ``mm context``) because the rows it
cleans live in user-local storage, not the Context Gateway artifact
tree. The ``gc`` group is the natural home for any future storage
maintenance subcommands (#884 review point 1).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from memtomem.storage.orphan_gc import OrphanProjectReport


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


def _under_dirs(_ctx: click.Context, _param: click.Parameter, values: tuple[str, ...]):
    # ``Path("")`` is the current directory, so an unset shell variable in
    # ``--under "$DIR"`` would select everything below wherever this runs.
    if any(not v.strip() for v in values):
        raise click.BadParameter("must name a directory, not an empty string.")
    return tuple(Path(v) for v in values)


@gc.command("orphan-sources")
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
    help="Skip the per-root confirmation prompts. Requires --apply.",
)
@click.option(
    "--under",
    "under",
    multiple=True,
    callback=_under_dirs,
    help="Only consider sources under this directory. Repeatable.",
)
def orphan_sources(apply_: bool, assume_yes: bool, under: tuple[Path, ...]) -> None:
    """Remove indexed chunks whose source file no longer exists.

    The scan checks missing sources twice to avoid treating a transiently
    unavailable cloud or removable volume as deleted. Orphans are grouped by
    the configured memory dir that owns them, plus one group for sources
    outside every root. Preview is the default; deletion requires ``--apply``
    and a confirmation per group unless ``--yes`` is also supplied.

    Some memory dirs belong to tools that delete their own files, and there
    the indexed chunks are the only copy left. Decline that group, or pass
    ``--under`` so that ``--apply --yes`` deletes only the directories named.
    """
    if assume_yes and not apply_:
        raise click.UsageError("--yes requires --apply.")
    asyncio.run(_run_orphan_sources(apply_=apply_, assume_yes=assume_yes, under=under))


def _archived_line(count: int) -> str:
    return f"{count} archived source(s) under retain_missing_dirs are kept."


def _archived_chunks_line(count: int) -> str:
    return f"{count} archived chunk(s) under retain_missing_dirs are kept."


async def _run_orphan_sources(
    *, apply_: bool, assume_yes: bool, under: tuple[Path, ...] = ()
) -> None:
    from memtomem.cli._bootstrap import cli_components
    from memtomem.storage.orphan_detect import (
        RetainRule,
        group_orphans,
        scan_orphans,
        source_state,
    )

    async with cli_components() as comp:
        result = await scan_orphans(comp.storage)
        unavailable = sorted(result.unavailable_sources)
        rule = await asyncio.to_thread(RetainRule.prepare, comp.config.indexing)
        archived, candidates = await asyncio.to_thread(
            rule.split_for_purge, result.confirmed_orphans
        )
        groups, excluded = await asyncio.to_thread(
            group_orphans,
            candidates,
            comp.config.indexing.all_index_roots(),
            under=under,
        )
        orphan_count = sum(len(g.sources) for g in groups)
        if not orphan_count:
            if excluded:
                click.secho(
                    "No orphaned sources under the given directories "
                    f"({excluded} orphaned elsewhere).",
                    fg="green",
                )
            else:
                click.secho(
                    f"No orphaned sources found ({result.total_sources} source files checked).",
                    fg="green",
                )
            if archived:
                click.echo(_archived_line(len(archived)))
            if unavailable:
                click.secho(
                    f"{len(unavailable)} source(s) could not be checked and are not eligible for purge.",
                    fg="yellow",
                )
            return

        click.echo(
            f"Found {orphan_count} orphaned source "
            f"{'file' if orphan_count == 1 else 'files'} "
            f"({result.total_sources} source files checked):"
        )
        for group in groups:
            click.echo(f"\n{group.label} ({len(group.sources)}):")
            for source in group.sources:
                click.echo(f"  - {source}")
        if excluded:
            click.echo(f"\n{excluded} more orphaned source(s) are outside the given directories.")
        if archived:
            click.echo(_archived_line(len(archived)))
        if unavailable:
            click.secho(
                f"{len(unavailable)} additional source(s) could not be checked and will not be purged.",
                fg="yellow",
            )

        if not apply_:
            click.echo(
                "\nRun with --apply to delete their indexed chunks (per-group confirmation)."
            )
            click.echo("Run with --apply --yes for non-interactive deletion.")
            if len(groups) > 1 and not under:
                click.echo("Add --under DIR to limit either to the sources under DIR.")
            return

        deleted = 0
        purged = 0
        skipped = 0
        declined = 0

        def summary() -> str:
            text = f"{deleted} chunks deleted from {purged} sources"
            if skipped:
                text += f" ({skipped} recovered or unavailable; skipped)"
            if declined:
                text += f"; {declined} group(s) declined"
            return text

        try:
            for group in groups:
                where = group.label if group.root is None else f"under {group.label}"
                if not assume_yes and not click.confirm(
                    f"Delete indexed chunks for {len(group.sources)} missing source file(s) "
                    f"{where}?",
                    default=False,
                ):
                    click.echo(f"  skipped: {group.label}")
                    declined += 1
                    continue
                for source in group.sources:
                    if await asyncio.to_thread(source_state, source) != "missing":
                        skipped += 1
                        continue
                    deleted += await comp.storage.delete_by_source(source)
                    purged += 1
        except click.Abort:
            # The groups confirmed before the interrupted prompt are already gone.
            click.secho(f"\nStopped: {summary()}.", fg="yellow", err=True)
            raise
        finally:
            if deleted and getattr(comp, "search_pipeline", None) is not None:
                comp.search_pipeline.invalidate_cache()
        if not purged and not skipped:
            click.echo(f"Cleanup cancelled; no chunks were deleted ({declined} group(s) declined).")
            return
        click.secho(f"Cleanup complete: {summary()}.", fg="green")


async def _run(*, apply_: bool, assume_yes: bool) -> None:
    from memtomem.cli._bootstrap import cli_components

    from memtomem.storage.orphan_detect import RetainRule

    async with cli_components() as comp:
        rule = await asyncio.to_thread(RetainRule.prepare, comp.config.indexing)
        keep = rule.retains_key if rule.retain else None
        reports = await comp.storage.find_orphan_project_roots(keep=keep)
        kept = sum(r.kept_rows for r in reports)
        reports = [r for r in reports if r.total_rows]

        if kept:
            click.echo(_archived_chunks_line(kept))
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
            result = await comp.storage.sweep_orphan_project_root(report.project_root, keep=keep)
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


def _print_report(report: OrphanProjectReport) -> None:
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
