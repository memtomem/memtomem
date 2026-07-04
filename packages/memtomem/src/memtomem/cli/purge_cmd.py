"""CLI: mm purge --matching-excluded — delete chunks whose source matches exclude patterns."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from pathlib import Path

import click


def find_sources_matching_excluded(
    sources: Iterable[Path],
    user_patterns: Iterable[str],
    memory_dirs: Iterable[str | Path],
) -> list[Path]:
    """Return source paths the indexer would now exclude.

    Routes through the indexer's own :func:`_path_is_excluded` so purge
    targets exactly what indexing skips: provider index-file conventions
    (e.g. a ``claude-memory`` root's ``MEMORY.md``/``README.md``), the
    built-in secret/noise denylist, and ``indexing.exclude_patterns``.
    Sharing the predicate is what lets ``mm purge --matching-excluded``
    reclaim chunks that were indexed before a convention/exclude was
    added. Exposed for testing — the CLI calls this with
    ``storage.get_all_source_files()`` and the configured index roots.
    """
    from memtomem.indexing.engine import _build_exclude_spec, _path_is_excluded

    user_spec = _build_exclude_spec(user_patterns)
    return [sf for sf in sources if _path_is_excluded(sf, memory_dirs, user_spec)]


@click.command("purge")
@click.option(
    "--matching-excluded",
    "matching_excluded",
    is_flag=True,
    help=(
        "Target chunks whose source_path matches built-in denylist, "
        "indexing.exclude_patterns, or a provider index-file convention "
        "(e.g. claude-memory MEMORY.md/README.md)."
    ),
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
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit a machine-readable JSON ack instead of text output.",
)
def purge(matching_excluded: bool, apply_: bool, sample_size: int, as_json: bool) -> None:
    """Remove stored chunks matching a selector.

    Currently one selector is supported: ``--matching-excluded`` scans every
    source_file in storage and deletes chunks whose path the indexer would
    now exclude — built-in secret/noise patterns, ``indexing.exclude_patterns``,
    and provider index-file conventions (e.g. a ``claude-memory`` root's
    ``MEMORY.md``/``README.md``). Use it to reclaim chunks indexed before a
    convention/exclude was added.

    Default is dry-run. Pass ``--apply`` to execute deletion.
    """
    if not matching_excluded:
        raise click.UsageError("no selector given. See: mm purge --help")
    asyncio.run(_run_matching_excluded(apply_=apply_, sample_size=sample_size, as_json=as_json))


async def _run_matching_excluded(*, apply_: bool, sample_size: int, as_json: bool = False) -> None:
    from memtomem.cli._bootstrap import cli_components

    async with cli_components() as comp:
        sources: set[Path] = await comp.storage.get_all_source_files()
        matched = find_sources_matching_excluded(
            sources,
            comp.config.indexing.exclude_patterns,
            comp.config.indexing.all_index_roots(),
        )

        if not matched:
            # Write-command JSON ack (CONTRIBUTING "JSON error shape"):
            # a no-match run is a successful no-op, not an error.
            if as_json:
                payload: dict = {"ok": True, "apply": apply_, "files": 0, "chunks": 0}
                if not apply_:
                    payload["sample"] = []
                click.echo(json.dumps(payload))
            else:
                click.secho("No stored chunks match the current exclude set.", fg="green")
            return

        # Count chunks per matched file for the summary.
        chunks_by_source = await comp.storage.list_chunks_by_sources(matched)
        total_chunks = sum(len(v) for v in chunks_by_source.values())

        if not apply_:
            sample = [str(sf) for sf in sorted(matched)[:sample_size]]
            if as_json:
                click.echo(
                    json.dumps(
                        {
                            "ok": True,
                            "apply": False,
                            "files": len(matched),
                            "chunks": total_chunks,
                            "sample": sample,
                        }
                    )
                )
                return
            click.echo(f"Would delete {total_chunks} chunks across {len(matched)} files. Sample:")
            for path_text in sample:
                click.echo(f"  {path_text}")
            if len(matched) > sample_size:
                click.echo(f"  ... and {len(matched) - sample_size} more")
            click.echo("\nRun with --apply to execute.")
            return

        deleted_total = 0
        for sf in matched:
            deleted_total += await comp.storage.delete_by_source(sf)
        if as_json:
            click.echo(
                json.dumps(
                    {"ok": True, "apply": True, "files": len(matched), "chunks": deleted_total}
                )
            )
            return
        click.secho(
            f"Deleted {deleted_total} chunks across {len(matched)} files.",
            fg="green",
        )
