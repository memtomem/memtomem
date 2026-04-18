"""CLI: mm purge --matching-excluded — delete chunks whose source matches exclude patterns."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from pathlib import Path

import click


def find_sources_matching_excluded(
    sources: Iterable[Path],
    user_patterns: Iterable[str],
) -> list[Path]:
    """Return source paths matching the current exclude set (built-in + user).

    Exposed for testing — the CLI calls this with storage.get_all_source_files().
    """
    from memtomem.indexing.engine import _BUILTIN_EXCLUDE_SPEC, _build_exclude_spec

    user_spec = _build_exclude_spec(user_patterns)
    matched: list[Path] = []
    for sf in sources:
        key = sf.as_posix().lower()
        if _BUILTIN_EXCLUDE_SPEC.match_file(key) or user_spec.match_file(key):
            matched.append(sf)
    return matched


@click.command("purge")
@click.option(
    "--matching-excluded",
    "matching_excluded",
    is_flag=True,
    help="Target chunks whose source_path matches built-in denylist or indexing.exclude_patterns.",
)
@click.option(
    "--apply",
    "apply_",
    is_flag=True,
    help="Actually delete. Without this flag, prints what would be deleted (dry-run).",
)
@click.option(
    "--sample",
    "sample_size",
    default=5,
    show_default=True,
    help="Number of sample paths to print in dry-run output.",
)
def purge(matching_excluded: bool, apply_: bool, sample_size: int) -> None:
    """Remove stored chunks matching a selector.

    Currently one selector is supported: ``--matching-excluded`` scans every
    source_file in storage and deletes chunks whose path matches the current
    exclude set (built-in secret/noise patterns + indexing.exclude_patterns).

    Default is dry-run. Pass ``--apply`` to execute deletion.
    """
    if not matching_excluded:
        raise click.UsageError("no selector given. See: mm purge --help")
    asyncio.run(_run_matching_excluded(apply_=apply_, sample_size=sample_size))


async def _run_matching_excluded(*, apply_: bool, sample_size: int) -> None:
    from memtomem.cli._bootstrap import cli_components

    async with cli_components() as comp:
        sources: set[Path] = await comp.storage.get_all_source_files()
        matched = find_sources_matching_excluded(sources, comp.config.indexing.exclude_patterns)

        if not matched:
            click.secho("No stored chunks match the current exclude set.", fg="green")
            return

        # Count chunks per matched file for the summary.
        chunks_by_source = await comp.storage.list_chunks_by_sources(matched)
        total_chunks = sum(len(v) for v in chunks_by_source.values())

        if not apply_:
            click.echo(f"Would delete {total_chunks} chunks across {len(matched)} files. Sample:")
            for sf in sorted(matched)[:sample_size]:
                click.echo(f"  {sf}")
            if len(matched) > sample_size:
                click.echo(f"  ... and {len(matched) - sample_size} more")
            click.echo("\nRun with --apply to execute.")
            return

        deleted_total = 0
        for sf in matched:
            deleted_total += await comp.storage.delete_by_source(sf)
        click.secho(
            f"Deleted {deleted_total} chunks across {len(matched)} files.",
            fg="green",
        )
