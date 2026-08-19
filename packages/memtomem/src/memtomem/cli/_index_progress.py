"""Shared streaming progress runner for ``mm index`` and the ``mm init``
seed flow.

Both call sites stream :meth:`IndexEngine.index_path_stream` and render the
same ``click.progressbar`` shape: file-unit length supplied by the engine's
``discovery`` event, ``progress`` events advance the bar by one and reset
the chunk-throttle clock, ``chunk_progress`` events refresh the sub-label
only (no advance), throttled to a 100 ms gap with a forced final-tick render
so ``(N/N)`` lands before the next file boundary. Issue #659 tracks
extracting the throttle into a helper shared with the web Index tab
(``web/static/app.js``); that's deferred to rule-of-three on the JS side.
The CLI side fires now (two callers: the wizard's
:func:`_seed_with_progress` and ``mm index``'s ``_index``).

Caller responsibilities (deliberately split out so the two surfaces can keep
their distinct UX):

* Catch :class:`KeyboardInterrupt` for the resume hint (``mm index <path>``
  vs ``mm web`` Reindex All — different copy per surface).
* Print the final summary line — ``mm index`` mirrors the legacy
  ``Indexed N file(s): …`` shape (stable for scripts that grep the output);
  the wizard prints a green "Seeded initial index" line plus zero-chunks
  warning. Different shapes, both want the same aggregate counters.

Helper guarantees: bar is always closed on exit (including raise), stream
runs serially over the supplied ``paths``, returned aggregate dict has
stable keys ``total_files``, ``indexed``, ``skipped``, ``deleted``,
``total_chunks``, ``duration_ms``, ``errors``, ``retryable_errors``,
``bar_rendered``, ``blocked``, ``blocked_paths``, ``blocked_project_shared``
(ADR-0006 PR-A).

:func:`print_blocked_summary` / :func:`print_index_errors` are the shared
post-run reporters for those counters — every CLI bulk-index surface
(``mm index``, the wizard seed, the interactive shell) prints the same
blocked-files block so the trust-boundary messaging can't drift per
surface; only the bypass hint differs (each surface names the command its
user can actually run)."""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

import click

from memtomem.constants import default_system_prefixes

#: Matches the redaction-block message ``IndexEngine`` synthesizes for a file
#: the privacy gate refused — ``f"{path.name}: redaction_blocked (hits=N,
#: scope=S, decision=D)"`` (``indexing/engine.py``, both the gather and the
#: stream branch). Anchored to that whole trailing shape rather than testing
#: for the ``redaction_blocked`` substring: a real failure on a file *named*
#: ``redaction_blocked.md`` renders as ``redaction_blocked.md: <cause>``, and
#: a substring test silently swallowed it — no error line, and no retry hint
#: even when the cause was retryable. ``test_index_privacy_block_surfaces``
#: pins this against drift in the engine's wording.
_REDACTION_BLOCKED_RE = re.compile(
    r": redaction_blocked \(hits=\d+, scope=[^,]+, decision=[^)]*\)$"
)


def _collect_seed_scale(memory_dir: Path) -> tuple[int, int]:
    """Count ``.md`` files and total bytes under ``memory_dir``, recursive.

    Decision input for :func:`_maybe_seed_initial_index`'s seed-or-skip
    threshold gate (file-count + size axis). ``.md`` only — other supported
    extensions (``.json``, ``.py``, etc.) exist but the dominant wizard
    workflow seeds human-written markdown memos. The progress-bar length is
    no longer derived here; the engine emits a ``discovery`` event with the
    actual file count it plans to process (issue #743). Silent on
    stat/permission errors: a dir the user can't read is one the index
    can't process either, so return (0, 0) and fall through.
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
    recursive: bool = True,
    force: bool = False,
    namespace: str | None = None,
    force_unsafe: bool = False,
    path_scope: Literal["configured", "explicit"] = "explicit",
    reassign_namespaces: bool = False,
) -> dict[str, Any]:
    """Stream ``index_path_stream`` across ``paths`` with a click.progressbar.

    Parameters
    ----------
    paths:
        Roots to stream serially. Each is passed to ``index_path_stream``
        in turn; complete-event counters aggregate across the run.
    label:
        Progress-bar label (e.g. ``"  Indexing"`` or ``"  Seeding"``).
    recursive, force, namespace, path_scope:
        Forwarded verbatim to ``index_path_stream``. ``namespace`` is
        ``None`` for the wizard seed (preserves prior behavior — namespace
        defaults are resolved server-side from indexing rules) and
        threadable from the ``mm index --namespace`` flag.

    Returns
    -------
    dict
        Aggregate of all ``complete`` events with keys ``total_files``,
        ``indexed``, ``skipped``, ``deleted``, ``total_chunks``,
        ``duration_ms``, ``errors`` (a list of human-readable strings),
        ``retryable_errors`` (the retryable same-string subset), and
        ``bar_rendered`` (bool — whether any event triggered bar
        creation; callers use this to gate trailing-newline output that
        would otherwise leave a stray blank line on empty discovers).
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
        "retryable_errors": [],
        "bar_rendered": False,
        # ADR-0006 PR-A: files skipped by the redaction gate (count + paths),
        # aggregated from each stream's ``complete`` event. ``blocked_project_shared``
        # is the subset that is hard-refused even with force_unsafe.
        "blocked": 0,
        "blocked_paths": [],
        "blocked_project_shared": 0,
        # #2061 namespace advisory, aggregated from each stream's ``complete``
        # event so the CLI reports the same numbers the non-stream
        # ``IndexingStats`` carries.
        "namespaces_preserved_against_rules": 0,
        "namespaces_reassigned": 0,
        "namespace_moves": [],
        # Captured from the bootstrapped config below. The engine deliberately
        # does not know about ``search.system_namespace_prefixes`` — it has no
        # search config, and threading one in would touch every construction
        # seam — so classifying a move as "this moved agent-scoped rows" is
        # the reporting layer's job. A components object that carries no
        # config falls back to the shipped defaults below rather than to an
        # empty list: setting the config to ``[]`` is an operator opting out
        # of system scoping, while carrying no config says nothing at all,
        # and silence must not read as "there is nothing to warn about".
        "system_namespace_prefixes": [],
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

    def _ensure_bar(length: int) -> None:
        # Lazy creation. Callers pass the file-unit length they know about
        # (engine ``discovery`` event → exact count for that stream;
        # progress / chunk_progress fallback → ``files_total`` from the
        # event, used when a stub engine doesn't emit ``discovery``).
        if bar_state["bar"] is None:
            bar_state["bar"] = click.progressbar(
                length=length,
                label=label,
                item_show_func=_format_item,
            ).__enter__()
            agg["bar_rendered"] = True

    def _grow_bar(extra: int) -> None:
        # Multi-path streams: each ``index_path_stream`` call emits its own
        # ``discovery``. The first sets the bar length; subsequent ones
        # extend it so the percent indicator stays accurate across the whole
        # run instead of resetting per path.
        if bar_state["bar"] is None:
            _ensure_bar(extra)
        else:
            bar_state["bar"].length += extra

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
            config = getattr(comp, "config", None)
            agg["system_namespace_prefixes"] = (
                list(config.search.system_namespace_prefixes)
                if config is not None
                else default_system_prefixes()
            )
            for p in paths:
                async for evt in comp.index_engine.index_path_stream(
                    p,
                    recursive=recursive,
                    force=force,
                    namespace=namespace,
                    force_unsafe=force_unsafe,
                    path_scope=path_scope,
                    reassign_namespaces=reassign_namespaces,
                ):
                    if evt["type"] == "discovery":
                        # Authoritative bar-length source. Engine emits this
                        # exactly once per stream call after ``_discover_files``
                        # has run, so the bar appears immediately even when
                        # the first file's embedding takes a while (no
                        # ``chunk_progress`` for small files, ``progress``
                        # only at file completion — without ``discovery``
                        # the bar would stay invisible until that point).
                        _grow_bar(evt["files_total"])
                    elif evt["type"] == "chunk_progress":
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
                        # Bar length normally comes from the discovery event
                        # above; this is just the lazy-create fallback for
                        # legacy stubs that skip discovery and jump straight
                        # to chunk_progress.
                        _ensure_bar(evt["files_total"])
                        # Refresh the sub-label without advancing the bar —
                        # length is in **file units**, so chunks must not
                        # double-count. ``update(0, item)`` re-renders with
                        # the new ``current_item`` only.
                        bar_state["bar"].update(0, (evt["file"], done, total))
                    elif evt["type"] == "progress":
                        # Reset throttle on file boundary so the next file's
                        # first chunk_progress renders immediately.
                        throttle_state["last_render"] = 0.0
                        _ensure_bar(evt["files_total"])
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
                        agg["blocked"] += evt.get("blocked_files", 0)
                        agg["blocked_project_shared"] += evt.get("blocked_project_shared_files", 0)
                        agg["blocked_paths"].extend(evt.get("blocked_paths") or [])
                        errs = evt.get("errors") or []
                        if errs:
                            agg["errors"].extend(errs)
                        retryable_errs = evt.get("retryable_errors") or []
                        if retryable_errs:
                            agg["retryable_errors"].extend(retryable_errs)
                        agg["namespaces_preserved_against_rules"] += evt.get(
                            "namespaces_preserved_against_rules", 0
                        )
                        agg["namespaces_reassigned"] += evt.get("namespaces_reassigned", 0)
                        agg["namespace_moves"].extend(evt.get("namespace_moves") or [])
    finally:
        _close_bar()

    return agg


def print_blocked_summary(
    *,
    blocked: int,
    blocked_paths: Sequence[str],
    blocked_project_shared: int,
    bypass_hint: str,
) -> None:
    """Print the redaction-blocked summary shared by the CLI bulk surfaces.

    ADR-0006 PR-A: name the secret-bearing files the gate skipped, with
    scope-correct guidance — ``project_shared`` is hard-refused even with
    ``--force-unsafe``, so the bypass hint covers only the bypassable rest.
    ``bypass_hint`` is surface-specific (``mm index`` says "re-run with
    --force-unsafe"; surfaces without their own flag name the command to
    run instead) and is printed after an arrow prefix. No-op when nothing
    was blocked.
    """
    if not blocked:
        return
    bypassable = blocked - blocked_project_shared
    click.secho(f"  {blocked} file(s) blocked by redaction guard:", fg="yellow")
    for p in blocked_paths:
        click.secho(f"    {p}", fg="yellow")
    if bypassable:
        click.secho(f"  → {bypass_hint}", fg="yellow")
    if blocked_project_shared:
        click.secho(
            f"  → {blocked_project_shared} file(s) are in the project_shared tier — "
            "hard-refused; --force-unsafe does not apply. Move them to "
            "user/project_local or remove the secret.",
            fg="yellow",
        )


def print_namespace_advisory(
    *,
    preserved_against_rules: int,
    reassigned: int,
    moves: Sequence[dict],
    reassign_hint: str,
    system_namespace_prefixes: Sequence[str] = (),
) -> None:
    """Report what a run did to stored namespaces (#2061).

    Two halves, and a run triggers at most one of them in practice:

    * ``preserved_against_rules`` — files that kept their stored namespace
      while the current path rules would have assigned a different one. This
      is what a user who edited their rules and re-ran ``--force`` used to get
      silently applied; now it is named, with the command that applies it.
    * ``reassigned`` / ``moves`` — what a ``--reassign-namespaces`` run
      actually moved, per ``old → new`` pair. Moves out of a system-scoped
      namespace (``agent-runtime:`` and friends) are called out separately:
      they are the deliberate form of the damage #2061 was filed about, so
      "you asked for this" still deserves to be legible after the fact.

    No-op when a run neither preserved against the rules nor moved anything.
    """
    if preserved_against_rules:
        click.secho(
            f"  {preserved_against_rules} file(s) kept their stored namespace; "
            "current rules would assign differently.",
            fg="yellow",
        )
        click.secho(f"  → To apply the rules: {reassign_hint}", fg="yellow")
    if not reassigned:
        return
    click.secho(f"  {reassigned} file(s) reassigned to a rule-resolved namespace:", fg="yellow")
    for move in moves:
        click.secho(
            f"    {move['from']} → {move['to']}: {move['files']} file(s)",
            fg="yellow",
        )

    def _is_system(namespace: object) -> bool:
        return any(str(namespace).startswith(prefix) for prefix in system_namespace_prefixes)

    # Only a system → non-system transition exposes anything: a move from one
    # system scope to another (``agent-runtime:a`` → ``archive:x``) stays
    # hidden from a default search. Files are summed, not lines — one line can
    # stand for many files.
    exposed = sum(
        int(move["files"])
        for move in moves
        if _is_system(move["from"]) and not _is_system(move["to"])
    )
    if exposed:
        click.secho(
            f"  → {exposed} file(s) moved out of a system-scoped namespace "
            "(agent session or archive scope). Those chunks are now visible "
            "to a default search.",
            fg="red",
        )


def print_index_errors(
    errors: Sequence[str],
    *,
    retryable_errors: Sequence[str] = (),
    retry_hint: str,
) -> None:
    """Print non-redaction per-file errors from a bulk index run.

    ``redaction_blocked`` entries are skipped — :func:`print_blocked_summary`
    already surfaced those files with a clearer message and hint. Retryable
    errors are a same-string subset of ``errors``; label them in place rather
    than printing the subset again.

    ``retry_hint`` is required rather than defaulted: the actionable resume
    command differs per surface (`mm index` vs. the init resume line vs. the
    shell's out-of-shell form), and a generic default would silently hand the
    wrong command to whichever caller forgot to pass one.
    """
    retryable_set = set(retryable_errors)
    rendered_retryable = False
    for err in errors:
        if _REDACTION_BLOCKED_RE.search(err):
            continue
        if err in retryable_set:
            click.echo(click.style(f"  ERROR (retryable): {err}", fg="yellow"))
            rendered_retryable = True
        else:
            click.echo(click.style(f"  ERROR: {err}", fg="red"))
    if rendered_retryable:
        click.secho(f"  → {retry_hint}", fg="yellow")


# Re-exported asyncio.run wrapper kept thin: callers want the surface-specific
# error handling around the await, so they manage ``asyncio.run`` themselves.
__all__ = [
    "print_blocked_summary",
    "print_index_errors",
    "print_namespace_advisory",
    "run_with_progress",
    "_collect_seed_scale",
]
