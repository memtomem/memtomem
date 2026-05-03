"""Shared streaming progress runner for ``mm index`` and the ``mm init``
seed flow.

Both call sites stream :meth:`IndexEngine.index_path_stream` and render the
same ``click.progressbar`` shape: file-unit length pre-computed up front,
``progress`` events advance the bar by one and reset the chunk-throttle clock,
``chunk_progress`` events refresh the sub-label only (no advance), throttled
to a 100 ms gap with a forced final-tick render so ``(N/N)`` lands before the
next file boundary. Issue #659 tracks extracting the throttle into a helper
shared with the web Index tab (``web/static/app.js``); that's deferred to
rule-of-three on the JS side. The CLI side fires now (two callers: the
wizard's :func:`_seed_with_progress` and ``mm index``'s ``_index``).

Caller responsibilities (deliberately split out so the two surfaces can keep
their distinct UX):

* Compute ``expected_total`` (file-unit bar length) — the wizard sums it
  across multiple paths via :func:`_collect_seed_scale`; ``mm index`` passes
  the single-path count.
* Catch :class:`KeyboardInterrupt` for the resume hint (``mm index <path>``
  vs ``mm web`` Reindex All — different copy per surface).
* Print the final summary line — ``mm index`` mirrors the legacy
  ``Indexed N file(s): …`` shape (stable for scripts that grep the output);
  the wizard prints a green "Seeded initial index" line plus zero-chunks
  warning. Different shapes, both want the same aggregate counters.

Helper guarantees: bar is always closed on exit (including raise), stream
runs serially over the supplied ``paths``, returned aggregate dict has
stable keys ``total_files``, ``indexed``, ``skipped``, ``deleted``,
``total_chunks``, ``duration_ms``, ``errors``."""

from __future__ import annotations

import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import click


def _collect_seed_scale(memory_dir: Path) -> tuple[int, int]:
    """Count ``.md`` files and total bytes under ``memory_dir``, recursive.

    Two-axis decision input for :func:`_maybe_seed_initial_index` and the
    progress-bar length precomputation in :func:`run_with_progress`. ``.md``
    only — other supported extensions (``.json``, ``.py``, etc.) exist but
    the dominant CLI workflow indexes human-written markdown memos, and the
    bar length is "good enough" so long as it doesn't undercount the common
    case. Silent on stat/permission errors: a dir the user can't read is
    one the index can't process either, so return (0, 0) and fall through.
    """
    if not memory_dir.exists():
        return 0, 0
    count = 0
    total = 0
    try:
        for f in memory_dir.rglob("*.md"):
            try:
                total += f.stat().st_size
                count += 1
            except OSError:
                continue
    except OSError:
        return 0, 0
    return count, total


async def run_with_progress(
    paths: Sequence[Path],
    *,
    label: str,
    expected_total: int,
    recursive: bool = True,
    force: bool = False,
    namespace: str | None = None,
) -> dict[str, Any]:
    """Stream ``index_path_stream`` across ``paths`` with a click.progressbar.

    Parameters
    ----------
    paths:
        Roots to stream serially. Each is passed to ``index_path_stream``
        in turn; complete-event counters aggregate across the run.
    label:
        Progress-bar label (e.g. ``"  Indexing"`` or ``"  Seeding"``).
    expected_total:
        Pre-computed bar length in **file units**. Caller uses
        :func:`_collect_seed_scale` (or sums it across multiple paths) so
        the percent indicator is stable from the first event onwards.
    recursive, force, namespace:
        Forwarded verbatim to ``index_path_stream``. ``namespace`` is
        ``None`` for the wizard seed (preserves prior behavior — namespace
        defaults are resolved server-side from indexing rules) and
        threadable from the ``mm index --namespace`` flag.

    Returns
    -------
    dict
        Aggregate of all ``complete`` events with keys ``total_files``,
        ``indexed``, ``skipped``, ``deleted``, ``total_chunks``,
        ``duration_ms``, and ``errors`` (a list of human-readable strings).
        Caller renders its own summary line from this.

    Raises
    ------
    KeyboardInterrupt
        Propagated cleanly after the bar is torn down so the caller can
        print a surface-specific resume hint. Bar cleanup lives in this
        helper's ``finally`` so callers don't double-handle it.
    Exception
        Any other exception (component bootstrap failure, embedder error,
        IO) is propagated unchanged after bar cleanup.
    """
    bar_state: dict[str, Any] = {"bar": None}
    agg: dict[str, Any] = {
        "total_files": 0,
        "indexed": 0,
        "skipped": 0,
        "deleted": 0,
        "total_chunks": 0,
        "duration_ms": 0.0,
        "errors": [],
    }

    # Throttle clock for ``chunk_progress`` label refreshes. Mirrors the web
    # Index tab (``web/static/app.js`` ~L4219-4256): 100ms gap between
    # intermediate renders, final tick (chunks_done >= chunks_total) bypasses
    # the throttle so ``(N/N)`` lands before the next file boundary, and the
    # clock resets to 0 on every ``progress`` event so the next file's first
    # chunk renders immediately. ``time.monotonic()`` (not ``time.time()``)
    # so a wall-clock jump can't stall the bar. Issue #659 tracks extracting
    # this into a shared helper with the JS implementation once a third
    # call-site appears (rule-of-three).
    throttle_state: dict[str, float] = {"last_render": 0.0}

    def _format_item(item: object) -> str:
        if not item:
            return ""
        if isinstance(item, tuple):
            file, done, total = item
            return f"{Path(file).name} ({done}/{total})"[:60]
        # Legacy str case: the existing ``progress`` branch passes a path str.
        # ``Path(...).name`` (not ``rsplit("/", 1)``) handles Windows
        # backslash paths correctly.
        return Path(str(item)).name[:40]

    def _ensure_bar() -> None:
        # Lazy creation: the bar can come into existence on EITHER the first
        # ``chunk_progress`` OR the first ``progress`` event, whichever
        # arrives first. ``chunk_progress`` for a given file is emitted before
        # that file's ``progress`` summary, so for large files the bar
        # appears at chunk-level rather than waiting for the first file to
        # finish.
        if bar_state["bar"] is None:
            bar_state["bar"] = click.progressbar(
                length=expected_total,
                label=label,
                item_show_func=_format_item,
            ).__enter__()

    def _close_bar() -> None:
        if bar_state["bar"] is not None:
            try:
                bar_state["bar"].__exit__(None, None, None)
            except Exception:  # pragma: no cover - click bar cleanup
                pass
            bar_state["bar"] = None

    try:
        from memtomem.cli._bootstrap import cli_components

        async with cli_components() as comp:
            for p in paths:
                async for evt in comp.index_engine.index_path_stream(
                    p, recursive=recursive, force=force, namespace=namespace
                ):
                    if evt["type"] == "chunk_progress":
                        # Server-side gating in ``indexing/engine.py`` already
                        # filters out small files (``progress_threshold``,
                        # default 32), so we don't threshold here — small
                        # files simply won't emit these events, matching the
                        # web Index tab's quiet behavior.
                        done = evt["chunks_done"]
                        total = evt["chunks_total"]
                        is_final = done >= total
                        now = time.monotonic()
                        if not is_final and now - throttle_state["last_render"] < 0.1:
                            continue
                        throttle_state["last_render"] = now
                        _ensure_bar()
                        # Refresh the sub-label without advancing the bar —
                        # length is in **file units**, so chunks must not
                        # double-count. ``update(0, item)`` re-renders with
                        # the new ``current_item`` only.
                        bar_state["bar"].update(0, (evt["file"], done, total))
                    elif evt["type"] == "progress":
                        # Reset throttle on file boundary so the next file's
                        # first chunk_progress renders immediately.
                        throttle_state["last_render"] = 0.0
                        _ensure_bar()
                        bar_state["bar"].update(1, evt["file"])
                    elif evt["type"] == "complete":
                        agg["total_files"] += evt["total_files"]
                        agg["indexed"] += evt["indexed_chunks"]
                        agg["skipped"] += evt["skipped_chunks"]
                        agg["deleted"] += evt.get("deleted_chunks", 0)
                        agg["total_chunks"] += evt.get("total_chunks", 0)
                        # Multi-path runs: durations sum, errors concatenate.
                        # Single-path is the dominant case so this is a
                        # simple aggregation rather than tracking per-path.
                        agg["duration_ms"] += evt.get("duration_ms", 0.0)
                        errs = evt.get("errors") or []
                        if errs:
                            agg["errors"].extend(errs)
    finally:
        _close_bar()

    return agg


# Re-exported asyncio.run wrapper kept thin: callers want the surface-specific
# error handling around the await, so they manage ``asyncio.run`` themselves.
__all__ = ["run_with_progress", "_collect_seed_scale"]
