"""CLI: memtomem index <path>."""

from __future__ import annotations

import asyncio
import json as _json
from pathlib import Path

import click


@click.command()
@click.argument("path", default=".", required=False)
@click.option("--recursive/--no-recursive", default=True, help="Recurse into subdirectories")
@click.option("--force", is_flag=True, help="Force re-index (ignore hashes)")
@click.option("--namespace", "-n", default=None, help="Target namespace")
@click.option(
    "--debounce-window",
    "debounce_window",
    type=click.FloatRange(min=0.0),
    default=None,
    metavar="SECONDS",
    help=(
        "Record PATH in the debounce queue and drain entries that have been silent "
        "at least SECONDS. Designed for hook callers (PostToolUse[Write]); rapid "
        "consecutive writes restart the window so a burst is indexed once at the end."
    ),
)
@click.option(
    "--flush",
    "do_flush",
    is_flag=True,
    help=(
        "Synchronously drain the debounce queue. Blocks until every queued file "
        "has been indexed (or recorded as an error). Worst-case latency ≈ queue "
        "depth × per-file index cost."
    ),
)
@click.option(
    "--status",
    "do_status",
    is_flag=True,
    help=(
        "Print a snapshot of the debounce queue (depth, oldest entry). Race-prone: "
        "concurrent hooks may modify the queue between this read and any later "
        "action; for correctness use --flush, not status-then-flush."
    ),
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit one JSON line of the result (works with --debounce-window/--flush/--status).",
)
def index(
    path: str,
    recursive: bool,
    force: bool,
    namespace: str | None,
    debounce_window: float | None,
    do_flush: bool,
    do_status: bool,
    as_json: bool,
) -> None:
    """Index files at PATH into the knowledge base.

    The debounce flags (``--debounce-window`` / ``--flush`` / ``--status``)
    are mutually exclusive with each other; ``--debounce-window`` and the
    plain index path are also mutually exclusive — pick recording for a
    later drain or indexing now.
    """
    modes = [debounce_window is not None, do_flush, do_status]
    if sum(modes) > 1:
        raise click.UsageError("--debounce-window, --flush, and --status are mutually exclusive.")

    if do_status:
        _print_status(as_json=as_json)
        return

    if do_flush:
        _run_flush(as_json=as_json)
        return

    if debounce_window is not None:
        _run_debounce(
            path=path,
            window_seconds=debounce_window,
            namespace=namespace,
            force=force,
            as_json=as_json,
        )
        return

    try:
        asyncio.run(_index(path, recursive, force, namespace))
    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(str(e)) from e


async def _index(path: str, recursive: bool, force: bool, namespace: str | None) -> None:
    from memtomem.cli._bootstrap import cli_components

    async with cli_components() as comp:
        resolved = Path(path).resolve()
        stats = await comp.index_engine.index_path(
            resolved,
            recursive=recursive,
            force=force,
            namespace=namespace,
        )

    click.echo(
        f"Indexed {stats.total_files} file(s): "
        f"{stats.indexed_chunks} new, {stats.skipped_chunks} unchanged, "
        f"{stats.deleted_chunks} deleted ({stats.duration_ms:.0f}ms)"
    )
    if stats.errors:
        for err in stats.errors:
            click.echo(click.style(f"  ERROR: {err}", fg="red"))


def _print_status(*, as_json: bool) -> None:
    from memtomem.indexing import debounce

    snap = debounce.status_snapshot()
    if as_json:
        click.echo(
            _json.dumps(
                {
                    "depth": snap.depth,
                    "oldest_first_seen": snap.oldest_first_seen,
                    "oldest_path": snap.oldest_path,
                    "queue_path": str(snap.queue_path),
                }
            )
        )
        return
    click.echo(f"Debounce queue: {snap.queue_path}")
    click.echo(f"  Depth: {snap.depth}")
    if snap.oldest_path is not None:
        click.echo(f"  Oldest: {snap.oldest_path} (first seen {snap.oldest_first_seen})")


def _run_flush(*, as_json: bool) -> None:
    from memtomem.indexing import debounce

    async def _flush_async() -> debounce.DrainResult:
        from memtomem.cli._bootstrap import cli_components

        async with cli_components() as comp:
            return await debounce.drain_all(indexer=_make_indexer(comp))

    result = asyncio.run(_flush_async())
    _print_drain_result(result, as_json=as_json, label="Flushed")


def _run_debounce(
    *,
    path: str,
    window_seconds: float,
    namespace: str | None,
    force: bool,
    as_json: bool,
) -> None:
    from memtomem.indexing import debounce

    abs_path = str(Path(path).resolve())
    debounce.enqueue(abs_path, namespace=namespace, force=force)

    async def _drain_async() -> debounce.DrainResult:
        from memtomem.cli._bootstrap import cli_components

        async with cli_components() as comp:
            return await debounce.drain_ready(
                window_seconds=window_seconds, indexer=_make_indexer(comp)
            )

    result = asyncio.run(_drain_async())
    _print_drain_result(
        result,
        as_json=as_json,
        label=f"Debounced (window={window_seconds}s, queued={abs_path})",
    )


def _make_indexer(comp):
    """Return an awaitable that indexes a single absolute path with the
    queue entry's namespace/force. Closes over the bootstrapped components
    so the drain functions don't depend on the CLI bootstrap module."""

    async def _do(path_str: str, namespace: str | None, force: bool) -> None:
        await comp.index_engine.index_path(
            Path(path_str),
            recursive=False,
            force=force,
            namespace=namespace,
        )

    return _do


def _print_drain_result(result, *, as_json: bool, label: str) -> None:
    if as_json:
        click.echo(
            _json.dumps(
                {
                    "indexed": result.indexed,
                    "errors": [{"path": p, "message": m} for p, m in result.errors],
                    "remaining": result.remaining,
                }
            )
        )
        return
    click.echo(f"{label}")
    click.echo(f"  Indexed: {len(result.indexed)}")
    if result.errors:
        click.echo(f"  Errors: {len(result.errors)}")
        for p, m in result.errors:
            click.echo(click.style(f"    {p}: {m}", fg="red"))
    click.echo(f"  Remaining in queue: {result.remaining}")
