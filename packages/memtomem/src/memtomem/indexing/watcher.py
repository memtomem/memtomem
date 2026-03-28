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
    from memtomem.indexing.engine import IndexEngine

logger = logging.getLogger(__name__)


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
            self._loop.call_soon_threadsafe(self._queue.put_nowait, p)

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

    Runs as an asyncio task alongside the MCP server.
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
        self._observer: Observer | None = None
        self._queue: asyncio.Queue[Path] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        loop = asyncio.get_event_loop()
        handler = _MarkdownEventHandler(self._queue, loop, self._config.supported_extensions)
        self._observer = Observer()

        for watch_dir in self._config.memory_dirs:
            expanded = Path(watch_dir).expanduser().resolve()
            if expanded.exists():
                self._observer.schedule(handler, str(expanded), recursive=True)
                logger.info("Watching %s for changes", expanded)

        self._observer.start()
        self._task = asyncio.create_task(self._process_events())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._observer:
            self._observer.stop()
            self._observer.join()

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
