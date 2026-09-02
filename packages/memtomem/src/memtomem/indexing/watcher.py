"""File watcher: auto-reindex markdown files on change using watchdog."""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

from memtomem.config import IndexingConfig
from memtomem.errors import NamespaceResolutionError, RetryableError

if TYPE_CHECKING:
    from watchdog.observers.api import BaseObserver, ObservedWatch

    from memtomem.indexing.engine import IndexEngine
    from memtomem.models import IndexingStats
    from memtomem.search.pipeline import SearchPipeline

logger = logging.getLogger(__name__)

_STOP_SENTINEL = Path("/dev/null/__stop__")

# Max pending file-change events buffered between the watchdog thread and the
# async processor. When this fills (indexer is slower than the change rate),
# new events — including the shutdown sentinel — are dropped and a warning
# is logged. Raise this if you watch a very large tree with a slow indexer.
_WATCHER_QUEUE_MAXSIZE = 1000

# Startup-backfill retry budget for retryable failures (issue #2021). A root
# whose walk fails with a ``RetryableError`` (namespace preservation could not
# read the store, issue #2005/#2018) — or whose run reports files in
# ``stats.retryable_errors`` — is re-walked up to this many attempts total,
# with exponential backoff between them. Re-running a root is cheap: the
# content-hash dedup skips unchanged chunks, so a retry costs a walk, not a
# re-embed. Bounded because backfill runs while the process warms up — a store
# that is still down after the last attempt gets the same log-and-continue the
# per-dir handler always applied, and the file waits for the next filesystem
# event or restart.
_BACKFILL_MAX_ATTEMPTS = 3
_BACKFILL_RETRY_BASE_S = 5.0


def effective_watcher_backend(config: IndexingConfig) -> str:
    """Return the concrete watchdog backend selected for this runtime."""
    if config.watcher_backend == "auto":
        return "polling" if sys.platform == "darwin" else "native"
    return config.watcher_backend


def _create_observer(config: IndexingConfig) -> BaseObserver:
    if effective_watcher_backend(config) == "polling":
        return PollingObserver()
    return Observer()


class _MarkdownEventHandler(FileSystemEventHandler):
    """Watchdog event handler that enqueues changed .md files."""

    def __init__(
        self,
        queue: asyncio.Queue[Path],
        loop: asyncio.AbstractEventLoop,
        supported_extensions: frozenset[str],
    ) -> None:
        super().__init__()
        self._queue = queue
        self._loop = loop
        self._supported = supported_extensions

    def _enqueue(self, path: str) -> None:
        p = Path(path)
        if p.suffix in self._supported:
            # ``call_soon_threadsafe`` only *schedules* the put — a full queue
            # raises ``QueueFull`` later, inside the event loop's callback
            # runner, so the try/except must live in the callback itself
            # (catching it here is dead code and drops surface as unhandled
            # "Exception in callback" tracebacks instead of the warning).
            self._loop.call_soon_threadsafe(self._put_or_warn, p)

    def _put_or_warn(self, p: Path) -> None:
        """Runs on the event loop thread; drop loudly when the queue is full."""
        try:
            self._queue.put_nowait(p)
        except asyncio.QueueFull:
            logger.warning("File watcher queue full, dropping event for %s", p.name)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._enqueue(str(event.src_path))

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._enqueue(str(event.src_path))

    def on_deleted(self, event: FileSystemEvent) -> None:
        # #1566: a deleted .md is enqueued like any other path — the consumer
        # sees it no longer exists on disk and ``index_file`` purges its stale
        # chunks (delete-by-source). Without this a deleted file's content
        # stayed searchable until the opt-in orphan-compaction pass ran.
        if not event.is_directory:
            self._enqueue(str(event.src_path))

    def on_moved(self, event: FileSystemEvent) -> None:
        # #1566: enqueue BOTH paths. The old path no longer exists → its chunks
        # are deleted-by-source; the new path is indexed. The suffix filter in
        # ``_enqueue`` still admits a .md ``src`` even when the file was renamed
        # away to a non-.md ``dest`` (rename-away), so the stale chunks are
        # cleaned in that case too. Previously only ``dest`` was enqueued,
        # orphaning the old path's chunks on every rename/move.
        if not event.is_directory:
            self._enqueue(str(event.src_path))
            self._enqueue(str(event.dest_path))


class FileWatcher:
    """Watches configured directories and triggers re-indexing on file changes.

    Runs as an asyncio task alongside the MCP server and the web server
    (``memtomem.web.app``). ``start()`` does two things:

    1. Registers a ``recursive=True`` ``watchdog`` ``Observer`` on each
       existing ``memory_dir`` so future create/modify/move events trigger
       a debounced re-index. Always on — this is the ambient behavior
       that lets the running server pick up edits.
    2. **Opt-in startup backfill** (gated by
       ``IndexingConfig.startup_backfill``, default False): when enabled,
       walks each watched dir via ``IndexEngine.index_path(recursive=True)``
       to catch files the observer didn't see (server was down when they
       landed, or the dir was newly added to ``memory_dirs``). Idempotent
       via content-hash dedup; runs as a background task so a slow walk
       doesn't block startup. Default False because an unconditional
       startup walk reintroduces the PR #295 failure mode — a silent
       multi-minute CPU embed job blocking the server on first install.
       Users opt in via the ``mm init`` wizard's seed prompt or by
       editing ``indexing.startup_backfill`` directly.
    """

    def __init__(
        self,
        index_engine: IndexEngine,
        config: IndexingConfig,
        debounce_ms: int = 1500,
        *,
        search_pipeline: SearchPipeline | None = None,
    ) -> None:
        self._engine = index_engine
        self._config = config
        # Optional so the many test constructors keep working; both production
        # call sites (server/context.py, web/app.py) pass the live pipeline so
        # a watched-file edit drops the search result cache (#2141).
        self._search_pipeline = search_pipeline
        self._debounce_s = debounce_ms / 1000.0
        self._observer: BaseObserver | None = None
        # Track what the live observer actually is instead of deriving it from
        # ``_config``: callers may replace or mutate the config before asking
        # us to reconfigure, while the observer itself keeps its old backend.
        self._observer_backend: str | None = None
        self._queue: asyncio.Queue[Path] = asyncio.Queue(maxsize=_WATCHER_QUEUE_MAXSIZE)
        self._task: asyncio.Task[None] | None = None
        self._backfill_task: asyncio.Task[None] | None = None
        self._handler: _MarkdownEventHandler | None = None
        self._watches: dict[Path, ObservedWatch] = {}
        self._reconfigure_lock = asyncio.Lock()

    def _invalidate_if_mutated(self, stats: IndexingStats) -> None:
        """Drop the search result cache when a run actually wrote something.

        The cache is keyed on query + filters, never on content (#2141), so
        without this a query warmed just before a watched-file edit keeps
        answering from the pre-edit cache for up to ``search.cache_ttl``.
        Gated on ``mutated`` rather than the counters because a tag-only or
        validity-only edit is deliberately reported as ``skipped``.
        """
        if self._search_pipeline is not None and stats.mutated:
            self._search_pipeline.invalidate_cache()

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        handler = _MarkdownEventHandler(self._queue, loop, self._config.supported_extensions)
        self._handler = handler
        backend = effective_watcher_backend(self._config)
        self._observer = _create_observer(self._config)
        self._observer_backend = backend
        logger.info("File watcher backend: %s", backend)

        watched: list[Path] = []
        # ADR-0011: watch every index root (user-tier ``memory_dirs`` and
        # project-tier ``project_memory_dirs``) so files dropped into a
        # registered project_shared / project_local dir trigger reindex
        # the same way user-tier files do.
        for watch_dir in self._config.all_index_roots():
            expanded = Path(watch_dir).expanduser().resolve()
            if expanded.exists():
                watch = self._observer.schedule(handler, str(expanded), recursive=True)
                self._watches[expanded] = watch
                logger.info("Watching %s for changes", expanded)
                watched.append(expanded)

        self._observer.start()
        self._task = asyncio.create_task(self._process_events())
        if watched and self._config.startup_backfill:
            self._backfill_task = asyncio.create_task(self._backfill_existing(watched))

    async def reconfigure(self, config: IndexingConfig) -> None:
        """Reconcile live watchdog roots after a successful config change."""
        async with self._reconfigure_lock:
            observer = self._observer
            handler = self._handler
            if observer is None or handler is None:
                self._config = config
                return

            desired = {
                Path(path).expanduser().resolve()
                for path in config.all_index_roots()
                if Path(path).expanduser().resolve().exists()
            }
            desired_backend = effective_watcher_backend(config)
            current_backend = self._observer_backend or effective_watcher_backend(self._config)
            if desired_backend != current_backend:
                # Watchdog cannot change an Observer's implementation in place.
                # Build and start the replacement first so a construction or
                # scheduling failure leaves the old observer fully operational.
                replacement = _create_observer(config)
                replacement_watches: dict[Path, ObservedWatch] = {}
                try:
                    for path in sorted(desired):
                        replacement_watches[path] = replacement.schedule(
                            handler, str(path), recursive=True
                        )
                    replacement.start()
                except BaseException:
                    # ``start`` can fail after partially starting its thread.
                    # Best-effort cleanup must not mask the original failure.
                    try:
                        replacement.stop()
                        if replacement.is_alive():
                            replacement.join()
                    except Exception:
                        logger.warning(
                            "Failed to clean up replacement file watcher",
                            exc_info=True,
                        )
                    raise

                try:
                    observer.stop()
                    observer.join()
                except BaseException:
                    # Preserve the old published observer/config on failure and
                    # avoid leaking the successfully started replacement.
                    try:
                        replacement.stop()
                        replacement.join()
                    except Exception:
                        logger.warning(
                            "Failed to clean up replacement file watcher",
                            exc_info=True,
                        )
                    raise

                self._observer = replacement
                self._observer_backend = desired_backend
                self._watches = replacement_watches
                handler._supported = config.supported_extensions
                self._config = config
                logger.info("File watcher backend changed to: %s", desired_backend)
                return

            current = set(self._watches)
            added: list[Path] = []
            removed: list[Path] = []
            try:
                for path in sorted(desired - current):
                    watch = observer.schedule(handler, str(path), recursive=True)
                    self._watches[path] = watch
                    added.append(path)
                for path in sorted(current - desired):
                    observer.unschedule(self._watches[path])
                    self._watches.pop(path)
                    removed.append(path)
            except Exception:
                for path in added:
                    rollback_watch = self._watches.pop(path, None)
                    if rollback_watch is not None:
                        try:
                            observer.unschedule(rollback_watch)
                        except Exception:
                            logger.debug("Failed to rollback watcher schedule", exc_info=True)
                for path in removed:
                    try:
                        self._watches[path] = observer.schedule(handler, str(path), recursive=True)
                    except Exception:
                        logger.error("Failed to restore watcher during rollback: %s", path)
                raise
            handler._supported = config.supported_extensions
            self._config = config

    def rebind(
        self,
        index_engine: IndexEngine,
        search_pipeline: SearchPipeline | None,
    ) -> None:
        """Point future events at a freshly swapped engine/pipeline pair.

        A component swap (``mem_embedding_reset(mode="revert_to_stored")``)
        replaces ``Components.index_engine`` / ``search_pipeline``, but this
        watcher captured the originals at construction — without a rebind
        every subsequent auto-reindex runs through the retired engine and its
        retired embedder, and ``_invalidate_if_mutated`` drops the cache of a
        pipeline nobody queries anymore. An event already being processed
        finishes on the engine it started with; only future events see the
        new pair.
        """
        self._engine = index_engine
        self._search_pipeline = search_pipeline

    async def stop(self) -> None:
        if self._backfill_task is not None and not self._backfill_task.done():
            # Cancel — the backfill walk can take a while on large trees and
            # we don't want shutdown to block on it.
            self._backfill_task.cancel()
            try:
                await self._backfill_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning("Startup backfill task error during stop: %s", exc)
        if self._task is not None:
            # Signal graceful shutdown — flush pending before exit
            try:
                self._queue.put_nowait(_STOP_SENTINEL)
            except asyncio.QueueFull:
                logger.warning("File watcher queue full; could not signal graceful shutdown")
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
        if self._observer:
            self._observer.stop()
            self._observer.join()
        self._observer = None
        self._observer_backend = None
        self._handler = None
        self._watches.clear()
        self._task = None
        self._backfill_task = None

    async def _backfill_existing(self, dirs: list[Path]) -> None:
        """Index pre-existing files the observer can't see.

        The watchdog observer only fires on change events from the moment
        it's scheduled, so files that landed while the server was down (or
        before the dir was added to ``memory_dirs``) are invisible to it.
        This walks each watched dir once at startup and lets
        ``IndexEngine.index_path`` decide what's new — already-indexed
        files are skipped via content-hash dedup, so the cost is bounded
        by the changed-file count rather than the total tree size on
        every restart.

        Per-dir errors are logged and don't abort siblings. Retryable
        failures — the typed ``RetryableError`` the namespace-preservation
        prepass raises before any write (issue #2018), and per-file entries
        in ``stats.retryable_errors`` — get a bounded per-root re-walk with
        backoff instead of the log-and-continue that permanent failures get
        (issue #2021). ``NamespaceResolutionError``'s contract is that the
        watcher re-queues rather than drops; before this the per-file event
        path kept that promise and this entry point did not.

        Logs a single ``Startup backfill: walking N memory_dir(s)...``
        line at the start so opt-in users can tell whether the (potentially
        slow) walk is running or already finished — without this the only
        backfill-related logs were per-dir summary lines, and a quiet log
        looks identical to a hung server (the same UX failure mode that
        killed the PR #295 silent startup scan).
        """
        logger.info("Startup backfill: walking %d memory_dir(s)...", len(dirs))
        total_indexed = 0
        for d in dirs:
            total_indexed += await self._backfill_one_dir(d)
        logger.info("Startup backfill complete: %d new chunks indexed", total_indexed)

    async def _backfill_one_dir(self, d: Path) -> int:
        """Walk one root, retrying retryable failures; return chunks indexed.

        Indexed counts accumulate across attempts without double-counting:
        a re-walk skips already-upserted chunks via the content-hash dedup,
        so each attempt's ``indexed_chunks`` covers only what that attempt
        actually wrote.

        ``reported`` carries the permanent failures and blocked paths already
        named for this root, so a retryable file sharing the root with a
        permanently broken one doesn't reprint the broken one's warning once
        per attempt — the retry is for the retryable file, not for it.
        """
        indexed = 0
        reported: set[str] = set()
        for attempt in range(1, _BACKFILL_MAX_ATTEMPTS + 1):
            try:
                stats = await self._engine.index_path(d, recursive=True)
            except asyncio.CancelledError:
                raise
            except RetryableError as exc:
                # The pre-write namespace prepass failed the whole run before
                # any durable write (issue #2018) — nothing was indexed, so a
                # re-walk retries every file, not a partial remainder.
                if attempt < _BACKFILL_MAX_ATTEMPTS:
                    delay = _BACKFILL_RETRY_BASE_S * 2 ** (attempt - 1)
                    logger.warning(
                        "Startup backfill %s: retryable failure (%s); "
                        "retrying in %.0fs (attempt %d/%d)",
                        d,
                        exc,
                        delay,
                        attempt,
                        _BACKFILL_MAX_ATTEMPTS,
                    )
                    await asyncio.sleep(delay)
                    continue
                logger.error(
                    "Startup backfill failed for %s after %d attempt(s): %s",
                    d,
                    attempt,
                    exc,
                )
                return indexed
            except Exception as exc:
                logger.error("Startup backfill failed for %s: %s", d, exc)
                return indexed

            indexed += stats.indexed_chunks
            # Per attempt, not once at the end: a retry that later fails must
            # not swallow the writes an earlier attempt already committed.
            self._invalidate_if_mutated(stats)
            self._log_backfill_stats(d, stats, reported)
            if not stats.retryable_errors:
                return indexed
            if attempt < _BACKFILL_MAX_ATTEMPTS:
                delay = _BACKFILL_RETRY_BASE_S * 2 ** (attempt - 1)
                logger.warning(
                    "Startup backfill %s: %d file(s) failed retryably; "
                    "retrying in %.0fs (attempt %d/%d): %s",
                    d,
                    len(stats.retryable_errors),
                    delay,
                    attempt,
                    _BACKFILL_MAX_ATTEMPTS,
                    "; ".join(stats.retryable_errors),
                )
                await asyncio.sleep(delay)
            else:
                logger.error(
                    "Startup backfill %s: %d file(s) still failing retryably "
                    "after %d attempt(s): %s",
                    d,
                    len(stats.retryable_errors),
                    attempt,
                    "; ".join(stats.retryable_errors),
                )
        return indexed

    def _log_backfill_stats(self, d: Path, stats: IndexingStats, reported: set[str]) -> None:
        """Per-attempt summary lines for one root's walk.

        ``reported`` accumulates the blocked paths and permanent errors this
        root has already named, so a re-walk driven by a *retryable* file
        doesn't reprint them (issue #2021). Mutated in place — the caller
        keeps one set per root.
        """
        blocked_paths = [p for p in stats.blocked_paths if p not in reported]
        if blocked_paths:
            # ADR-0006 PR-A: secret-bearing files skipped during
            # backfill — name them so the log is actionable.
            reported.update(blocked_paths)
            logger.warning(
                "Startup backfill %s: %d file(s) blocked by redaction guard: %s",
                d,
                len(blocked_paths),
                ", ".join(blocked_paths),
            )
        # Retryable entries get their own retry-status line in
        # ``_backfill_one_dir``; repeating them here would log the same
        # failure twice per attempt at two different severities.
        retryable = set(stats.retryable_errors)
        other_errors = [
            e
            for e in stats.errors
            if "redaction_blocked" not in e and e not in retryable and e not in reported
        ]
        if other_errors:
            reported.update(other_errors)
            # Non-redaction per-file failures/skips (too-large, binary,
            # backend errors) — previously dropped. One aggregated line
            # per dir bounds the per-restart log noise.
            logger.warning(
                "Startup backfill %s: %d file(s) skipped or failed: %s",
                d,
                len(other_errors),
                "; ".join(other_errors),
            )
        if stats.indexed_chunks or stats.deleted_chunks:
            logger.info(
                "Startup backfill %s: indexed=%d skipped=%d deleted=%d",
                d,
                stats.indexed_chunks,
                stats.skipped_chunks,
                stats.deleted_chunks,
            )

    async def _process_events(self) -> None:
        """Consume changed file paths with batch debouncing.

        Collects changed files into a set.  When no new events arrive for
        ``_debounce_s`` seconds, all accumulated files are reindexed in a
        single batch before the set is cleared.
        """
        pending: set[Path] = set()

        while True:
            try:
                file_path = await asyncio.wait_for(self._queue.get(), timeout=self._debounce_s)
                if file_path == _STOP_SENTINEL:
                    # Flush remaining pending files before exiting. A file whose
                    # reindex times out on the sidecar is dropped here (we are
                    # shutting down; the next start's backfill will catch it).
                    if pending:
                        await self._flush_batch(pending)
                    return
                pending.add(file_path)
            except TimeoutError:
                if pending:
                    # Reindex the batch and carry forward any file whose sidecar
                    # acquire timed out, so the next debounce window retries it.
                    pending = await self._flush_batch(pending)
                continue

    async def _flush_batch(self, pending: set[Path]) -> set[Path]:
        """Reindex every file in *pending*; return the set to retry next window.

        ``_reindex`` returns a path when its sidecar acquire timed out (a CRUD
        span or ``memory-migrate`` held the file past the budget) and ``None``
        otherwise. Timed-out paths are re-queued by merging them back into the
        in-memory ``pending`` set — NOT by ``put_nowait`` onto the bounded
        watchdog queue, which drops under pressure and would silently lose the
        event (#1587). The next ``_debounce_s`` timeout is the natural retry
        backoff.
        """
        batch = list(pending)
        results = await asyncio.gather(
            *(self._reindex(p) for p in batch),
            return_exceptions=True,
        )
        retry: set[Path] = set()
        for path, result in zip(batch, results):
            if isinstance(result, Path):
                retry.add(result)
            elif isinstance(result, BaseException) and not isinstance(result, Exception):
                # Re-raise CancelledError / KeyboardInterrupt / SystemExit —
                # ``return_exceptions=True`` captured them but they must not be
                # swallowed as an ordinary reindex failure.
                raise result
        return retry

    async def _reindex(self, file_path: Path) -> Path | None:
        """Reindex one changed file. Returns ``file_path`` when the reindex
        timed out acquiring the file's cross-process sidecar (so the caller can
        retry it next window), else ``None``."""
        from memtomem.indexing.engine import PrivacyRejection

        try:
            stats = await self._engine.index_file(file_path)
            self._invalidate_if_mutated(stats)
            if stats.deleted_chunks and not stats.indexed_chunks and not file_path.exists():
                # #1566: pure delete pass (file gone from disk) — "Auto-reindexed
                # ... indexed=0 ... deleted=N" reads as a no-op in logs a user
                # greps during a privacy check. Name it for what it is.
                logger.info(
                    "Removed deleted file from index: %s (%d stale chunk(s))",
                    file_path.name,
                    stats.deleted_chunks,
                )
            else:
                logger.info(
                    "Auto-reindexed %s: indexed=%d skipped=%d deleted=%d",
                    file_path.name,
                    stats.indexed_chunks,
                    stats.skipped_chunks,
                    stats.deleted_chunks,
                )
        except PrivacyRejection as exc:
            # ADR-0006 PR-A: a watched file gained secret-class content; it is
            # left un-indexed (not a failure — the boundary is doing its job).
            logger.warning(
                "Auto-reindex blocked by redaction guard for %s: %d hit(s)",
                file_path.name,
                exc.hit_count,
            )
        except TimeoutError:
            # #1587: a CRUD span or ``memory-migrate`` held this file's sidecar
            # past the bounded acquire budget. The change is NOT lost — return
            # the path so ``_process_events`` retries it next debounce window
            # (the debounce interval is the natural backoff). Warn, not error:
            # this is expected contention, not a failure.
            logger.warning(
                "Auto-reindex deferred for %s: file locked by another writer; will retry",
                file_path.name,
            )
            return file_path
        except NamespaceResolutionError as exc:
            # Issue #2005: the engine refuses to re-resolve a namespace it
            # could not read the stored one for, rather than silently moving
            # the file's chunks. That refusal is transient by nature, so it
            # gets the retry path rather than the drop path — swallowing it
            # would leave the edit unindexed until something else touched the
            # file.
            logger.warning("Auto-reindex deferred for %s: %s; will retry", file_path.name, exc)
            return file_path
        except Exception as exc:
            logger.error("Auto-reindex failed for %s: %s", file_path, exc)
        return None


@dataclass(frozen=True)
class ResumedWatcher:
    """What became of a watcher a degraded startup left constructed but stopped.

    ``retryable`` is about the *instance*, not the failure: unlike the
    schedulers, a watcher is reused across attempts, and ``stop`` clears its
    observer and task handles only on the way out. So a start that fails and
    then cannot be stopped leaves both live where a later ``start`` would
    overwrite them, and nothing would ever stop what the first attempt left
    running. Reporting ``retryable=False`` is what keeps shutdown able to.
    """

    started: bool
    retryable: bool


async def resume_after_recovery(watcher: FileWatcher) -> ResumedWatcher:
    """Start a watcher that a degraded startup skipped (#2181, #2188).

    Both server surfaces come here once an embedding reset has cleared the
    mismatch: the MCP server through ``AppContext.recover_from_degraded`` and
    ``mm web`` through ``POST /api/embedding-reset``. They construct and hold
    the watcher differently, but the start and its failure policy are the same
    — and having been written twice is how ``mm web`` came to have no recovery
    at all.

    A failure is reported, never raised. The recovery the user asked for — a
    working index — has already happened by the time this runs, and losing
    auto-indexing must not turn a successful reset into a failed call.
    """
    try:
        await watcher.start()
    except asyncio.CancelledError:
        await _stop_failed_start(watcher)
        raise
    except Exception:
        logger.warning(
            "Failed to start the file watcher after embedding recovery — "
            "file edits are not auto-indexed until the next reset or restart",
            exc_info=True,
        )
        # ``start`` publishes the observer and the processor task before it can
        # fail, so whatever this attempt left running is stopped here or never.
        return ResumedWatcher(started=False, retryable=await _stop_failed_start(watcher))
    return ResumedWatcher(started=True, retryable=True)


async def _stop_failed_start(watcher: FileWatcher) -> bool:
    """Stop what a failed ``start`` left running; ``False`` if it could not be."""
    try:
        await watcher.stop()
    except asyncio.CancelledError:
        logger.warning("Recovery cleanup of the file watcher was cancelled")
        raise
    except Exception:
        logger.warning(
            "Recovery cleanup of the file watcher failed — no further in-process "
            "retry will start over it; restart the server to recover auto-indexing",
            exc_info=True,
        )
        return False
    return True
