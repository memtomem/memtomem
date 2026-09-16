"""CLI: mm purge --matching-excluded — delete chunks whose source matches exclude patterns."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import click

logger = logging.getLogger(__name__)

#: Cap on sample paths printed for sources that could not be classified.
UNCLASSIFIED_SAMPLE = 5


@dataclass
class PurgeClassification:
    """What a purge scan decided about every stored source.

    ``unclassified`` holds the sources the exclusion predicate raised on. A scan
    with any of them is incomplete: callers must not report it as a clean result
    or delete on it.
    """

    matched: list[Path] = field(default_factory=list)
    unclassified: list[Path] = field(default_factory=list)


class UnclassifiableSourcesError(RuntimeError):
    """Raised by :func:`find_sources_matching_excluded` when a scan is incomplete."""

    def __init__(self, unclassified: list[Path]):
        self.unclassified = unclassified
        super().__init__(f"{len(unclassified)} stored source(s) could not be classified")


def classify_sources_for_purge(
    sources: Iterable[Path],
    user_patterns: Iterable[str],
    memory_dirs: Iterable[str | Path],
) -> PurgeClassification:
    """Classify every source, collecting those the predicate cannot classify.

    See :func:`find_sources_matching_excluded` for what "excluded" means. A
    source the predicate raises on — an embedded NUL, a configured root with a
    symlink loop — goes to ``unclassified`` and the scan continues. The
    predicate raises rather than answering "not excluded" because that would
    let the indexer read a denylisted file.
    """
    from memtomem.indexing.engine import WorktreeMemo, _build_exclude_spec, _path_is_excluded

    user_spec = _build_exclude_spec(user_patterns)
    roots = list(memory_dirs)
    memo: WorktreeMemo = {}
    result = PurgeClassification()
    for sf in sources:
        try:
            if _path_is_excluded(sf, roots, user_spec, worktree_cache=memo):
                result.matched.append(sf)
        except (OSError, ValueError, RuntimeError):
            result.unclassified.append(sf)
            logger.warning("purge: could not classify %r", str(sf), exc_info=True)
    result.unclassified.sort(key=str)
    return result


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

    A stored row has no walk root, so a source outside every configured root
    is judged the way indexing that file on its own would judge it: bounded by
    its enclosing repository (#2486). One memo serves the whole pass, so sources
    in the same directories do not re-probe the filesystem.

    Raises :class:`UnclassifiableSourcesError` when any source could not be
    classified, rather than returning a list that silently omits it. Callers
    that must report an incomplete scan use :func:`classify_sources_for_purge`.
    """
    result = classify_sources_for_purge(sources, user_patterns, memory_dirs)
    if result.unclassified:
        raise UnclassifiableSourcesError(result.unclassified)
    return result.matched


@click.command("purge")
@click.option(
    "--matching-excluded",
    "matching_excluded",
    is_flag=True,
    help=(
        "Target chunks whose source_path matches built-in denylist, "
        "indexing.exclude_patterns, a provider index-file convention "
        "(e.g. claude-memory MEMORY.md/README.md), or a nested git worktree."
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
    provider index-file conventions (e.g. a ``claude-memory`` root's
    ``MEMORY.md``/``README.md``), and git worktrees nested under an indexed
    root or, outside every root, inside their enclosing repository. Use it to
    reclaim chunks indexed before a convention/exclude was added.

    Default is dry-run. Pass ``--apply`` to execute deletion.
    """
    if not matching_excluded:
        raise click.UsageError("no selector given. See: mm purge --help")
    asyncio.run(_run_matching_excluded(apply_=apply_, sample_size=sample_size, as_json=as_json))


async def _report_incomplete_scan(
    comp, scan: PurgeClassification, apply_: bool, sample_size: int, as_json: bool
) -> None:
    """Report a scan with unclassifiable sources. Nothing is deleted, even with ``--apply``.

    Deleting only the matches would announce a finished purge while sources the
    selector may cover stay stored — with a looping configured root, every
    source. The refusal is the narrow guarantee "deletion does not start", not a
    transaction; the classified matches are still listed so the operator sees
    what a clean run would remove.
    """
    matched = scan.matched
    counts = await comp.storage.count_chunks_by_sources(matched) if matched else {}
    matched_chunks = sum(counts.values())
    matched_sample = [str(sf) for sf in sorted(matched)[:sample_size]]
    unclassified_sample = [str(sf) for sf in scan.unclassified[:UNCLASSIFIED_SAMPLE]]
    if as_json:
        click.echo(
            json.dumps(
                {
                    "ok": False,
                    "reason": "unclassifiable_sources",
                    "apply": apply_,
                    "files": len(matched),
                    "chunks": matched_chunks,
                    "sample": matched_sample,
                    "deleted_chunks": 0,
                    "unclassified": len(scan.unclassified),
                    "unclassified_sample": unclassified_sample,
                }
            )
        )
        return
    outcome = "nothing was deleted" if apply_ else "the scan is incomplete"
    click.secho(
        f"Could not classify {len(scan.unclassified)} stored source(s); {outcome}.",
        fg="red",
        err=True,
    )
    for path_text in unclassified_sample:
        click.echo(f"  {path_text}", err=True)
    if len(scan.unclassified) > len(unclassified_sample):
        click.echo(f"  ... and {len(scan.unclassified) - len(unclassified_sample)} more", err=True)
    click.echo(
        "Check indexing.memory_dirs for a path that cannot be resolved (for example a "
        "symlink loop); the log names each source's error.",
        err=True,
    )
    click.echo(
        f"Classified matches: {matched_chunks} chunks across {len(matched)} files "
        "(not deleted until every source classifies)."
    )


async def _run_matching_excluded(*, apply_: bool, sample_size: int, as_json: bool = False) -> None:
    from memtomem.cli._bootstrap import cli_components

    async with cli_components() as comp:
        sources: set[Path] = await comp.storage.get_all_source_files()
        scan = classify_sources_for_purge(
            sources,
            comp.config.indexing.exclude_patterns,
            comp.config.indexing.all_index_roots(),
        )
        matched = scan.matched

        if scan.unclassified:
            await _report_incomplete_scan(comp, scan, apply_, sample_size, as_json)
            raise click.exceptions.Exit(1)

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

        if not apply_:
            # Counted, not listed. Reading the chunks to take ``len()`` capped
            # the preview at 10,000 per file while the delete it previews has
            # no such cap, so a big file was announced as smaller than it was
            # about to be deleted (#2261). Only the preview needs this — the
            # apply path reports what ``delete_by_source`` actually removed.
            counts = await comp.storage.count_chunks_by_sources(matched)
            total_chunks = sum(counts.values())
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
