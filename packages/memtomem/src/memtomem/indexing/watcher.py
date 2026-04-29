"""File watcher: auto-reindex markdown files on change using watchdog."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from memtomem.config import IndexingConfig

if TYPE_CHECKING:
    from watchdog.observers.api import BaseObserver

    from memtomem.indexing.engine import IndexEngine

logger = logging.getLogger(__name__)

_STOP_SENTINEL = Path("/dev/null/__stop__")

# Max pending file-change events buffered between the watchdog thread and the
# async processor. When this fills (indexer is slower than the change rate),
# new events — including the shutdown sentinel — are dropped and a warning
# is logged. Raise this if you watch a very large tree with a slow indexer.
_WATCHER_QUEUE_MAXSIZE = 1000


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
            try:
                self._loop.call_soon_threadsafe(self._queue.put_nowait, p)
            except asyncio.QueueFull:
                logger.warning("File watcher queue full, dropping event for %s", p.name)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._enqueue(str(event.src_path))

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._enqueue(str(event.src_path))

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
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
    ) -> None:
        self._engine = index_engine
        self._config = config
        self._debounce_s = debounce_ms / 1000.0
        self._observer: BaseObserver | None = None
        self._queue: asyncio.Queue[Path] = asyncio.Queue(maxsize=_WATCHER_QUEUE_MAXSIZE)
        self._task: asyncio.Task[None] | None = None
        self._backfill_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        handler = _MarkdownEventHandler(self._queue, loop, self._config.supported_extensions)
        self._observer = Observer()

        watched: list[Path] = []
        for watch_dir in self._config.memory_dirs:
            expanded = Path(watch_dir).expanduser().resolve()
            if expanded.exists():
                self._observer.schedule(handler, str(expanded), recursive=True)
                logger.info("Watching %s for changes", expanded)
                watched.append(expanded)

        self._observer.start()
        self._task = asyncio.create_task(self._process_events())
        if watched and self._config.startup_backfill:
            self._backfill_task = asyncio.create_task(self._backfill_existing(watched))

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

        Per-dir errors are logged and don't abort siblings.

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
            try:
                stats = await self._engine.index_path(d, recursive=True)
                total_indexed += stats.indexed_chunks
                if stats.indexed_chunks or stats.deleted_chunks:
                    logger.info(
                        "Startup backfill %s: indexed=%d skipped=%d deleted=%d",
                        d,
                        stats.indexed_chunks,
                        stats.skipped_chunks,
                        stats.deleted_chunks,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Startup backfill failed for %s: %s", d, exc)
        logger.info("Startup backfill complete: %d new chunks indexed", total_indexed)

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
                    # Flush remaining pending files before exiting
                    if pending:
                        batch = list(pending)
                        pending.clear()
                        await asyncio.gather(
                            *(self._reindex(p) for p in batch),
                            return_exceptions=True,
                        )
                    return
                pending.add(file_path)
            except TimeoutError:
                if pending:
                    batch = list(pending)
                    pending.clear()
                    await asyncio.gather(
                        *(self._reindex(p) for p in batch),
                        return_exceptions=True,
                    )
                continue

    async def _reindex(self, file_path: Path) -> None:
        try:
            stats = await self._engine.index_file(file_path)
            logger.info(
                "Auto-reindexed %s: indexed=%d skipped=%d deleted=%d",
                file_path.name,
                stats.indexed_chunks,
                stats.skipped_chunks,
                stats.deleted_chunks,
            )
        except Exception as exc:
            logger.error("Auto-reindex failed for %s: %s", file_path, exc)
