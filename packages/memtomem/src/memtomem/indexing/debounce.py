"""Debounce queue for hook-driven ``mm index`` calls.

Backs ``mm index --debounce-window`` (PR #536 documented gap close) for the
plugin's ``PostToolUse[Write]`` hook. The hook fires on every ``Write`` tool
use; without debouncing, codegen loops re-index the same file many times
within a few seconds. This module persists a per-path queue so a hook firing
in a burst can record the path cheaply and the *last* hook in the burst, or
a later flush, drains the entries that have been silent for at least the
debounce window.

The queue is a single JSON file under ``~/.memtomem/`` guarded by ``flock``.
Each entry tracks ``first_seen``, ``last_seen``, plus the ``namespace`` and
``force`` flags that should apply to the eventual indexing call. When the
same path is enqueued again with different flags, last-write wins (the most
recent caller's intent).

Synchronization model:

- Every queue mutation (enqueue, drain) takes ``LOCK_EX`` on the queue file.
  Concurrent ``mm index --debounce-window`` calls serialize cleanly without
  losing entries.
- ``--status`` deliberately skips the lock and reads a snapshot. The
  docstring on :func:`status_snapshot` flags the race so callers don't try
  to use status as a decision input — the only correct flush primitive is
  :func:`drain_all`.

Future-extensibility (RFC-B PreCompact, deferred): :func:`drain_all` is
defined to take an optional ``paths`` filter that's currently always
``None``. When the PreCompact payload contract lands and a checkpoint
handler wants to flush only the files Claude Code reports as in-flight,
``drain_all(paths=[...])`` becomes the entry point — no second ABI change
needed.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import IO, Awaitable, Callable, Iterable

logger = logging.getLogger(__name__)


_DEFAULT_QUEUE_PATH = Path("~/.memtomem/index_debounce_queue.json").expanduser()
_QUEUE_VERSION = 1


@dataclass
class QueueEntry:
    """One queued path with its first-seen / last-seen timestamps and the
    indexing flags that should apply when it eventually drains."""

    first_seen: float
    last_seen: float
    namespace: str | None = None
    force: bool = False

    @classmethod
    def from_dict(cls, d: dict) -> "QueueEntry":
        return cls(
            first_seen=float(d["first_seen"]),
            last_seen=float(d["last_seen"]),
            namespace=d.get("namespace"),
            force=bool(d.get("force", False)),
        )


@dataclass
class DrainResult:
    """Summary of a drain pass — what was indexed, what errored, what's left."""

    indexed: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)  # (path, message)
    remaining: int = 0


@dataclass
class StatusSnapshot:
    """Race-prone snapshot of the queue for ``mm index --status``.

    Concurrent hook callers may modify the queue between the read and any
    subsequent caller action. Use this only for telemetry / human-readable
    inspection, never as the input to a "is the queue empty?" decision —
    for that, call :func:`drain_all` (which is synchronous and gives a
    post-drain guarantee).
    """

    depth: int
    oldest_first_seen: float | None
    oldest_path: str | None
    queue_path: Path


def queue_path() -> Path:
    """Return the queue file path, honoring ``MEMTOMEM_INDEX_DEBOUNCE_QUEUE``
    if set (test-only override; matches the pattern used by
    ``stm_feedback_db_path`` in STM)."""
    override = os.environ.get("MEMTOMEM_INDEX_DEBOUNCE_QUEUE")
    if override:
        return Path(override).expanduser()
    return _DEFAULT_QUEUE_PATH


def _load(path: Path) -> dict[str, QueueEntry]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("debounce queue %s unreadable (%s); treating as empty", path, e)
        return {}
    entries = raw.get("entries", {}) if isinstance(raw, dict) else {}
    return {p: QueueEntry.from_dict(d) for p, d in entries.items()}


def _save(path: Path, entries: dict[str, QueueEntry]) -> None:
    """Atomic JSON write — same pattern as :func:`memtomem.config._atomic_write_json`,
    inlined here to avoid a cross-module private import."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": _QUEUE_VERSION,
        "entries": {p: asdict(e) for p, e in entries.items()},
    }
    tmp_fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".debounce.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


class _Lock:
    """``flock(LOCK_EX)`` on a sidecar lockfile next to the queue.

    The lockfile is deliberately *not* the queue file itself. ``_save``
    replaces the queue via ``os.replace``, which rebinds the path to a
    fresh inode mid-critical-section. If we locked the queue file, the
    lock would attach to the now-unlinked old inode while later callers
    open the new inode and obtain an uncontended lock — concurrent
    writers would lose entries.

    The sidecar (``.<queue_name>.lock``) is never replaced; every
    process locks the same inode for the duration of its critical
    section, so serialization is correct.

    POSIX only; on Windows the lock is a no-op and concurrent callers
    may interleave (acceptable: hooks fire serially per Claude Code
    session, the supported case).
    """

    def __init__(self, path: Path) -> None:
        self._lock_path = path.parent / f".{path.name}.lock"
        self._fp: IO[bytes] | None = None

    def __enter__(self) -> "_Lock":
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fp = open(self._lock_path, "a+b")
        if sys.platform != "win32":
            import fcntl

            fcntl.flock(self._fp, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc) -> None:
        if self._fp is None:
            return
        if sys.platform != "win32":
            import fcntl

            fcntl.flock(self._fp, fcntl.LOCK_UN)
        self._fp.close()
        self._fp = None


def enqueue(
    path_str: str,
    *,
    namespace: str | None = None,
    force: bool = False,
    now: float | None = None,
    queue_file: Path | None = None,
) -> None:
    """Record one path's most recent write timestamp. Last-write wins for
    ``namespace``/``force`` so the most recent caller's intent applies on
    drain. Idempotent — repeated calls just push ``last_seen`` forward."""
    qp = queue_file or queue_path()
    ts = time.time() if now is None else now
    with _Lock(qp):
        entries = _load(qp)
        existing = entries.get(path_str)
        if existing is None:
            entries[path_str] = QueueEntry(
                first_seen=ts, last_seen=ts, namespace=namespace, force=force
            )
        else:
            existing.last_seen = ts
            existing.namespace = namespace
            existing.force = force
        _save(qp, entries)


def _ready(entry: QueueEntry, window_seconds: float, now: float) -> bool:
    return (now - entry.last_seen) >= window_seconds


async def drain_ready(
    *,
    window_seconds: float,
    indexer: Callable[[str, str | None, bool], Awaitable[None]],
    now: float | None = None,
    queue_file: Path | None = None,
) -> DrainResult:
    """Drain entries that have been silent for at least ``window_seconds``.

    Called from ``mm index --debounce-window``. The caller's own enqueue
    happened just before this; that entry's ``last_seen`` equals ``now``,
    so it never qualifies on its own call (correct — this hook fired
    *because* the file was just written, so the window restarts).
    """
    qp = queue_file or queue_path()
    ts = time.time() if now is None else now
    result = DrainResult()
    with _Lock(qp):
        entries = _load(qp)
        ready_paths = [p for p, e in entries.items() if _ready(e, window_seconds, ts)]
        for p in ready_paths:
            entry = entries[p]
            try:
                await indexer(p, entry.namespace, entry.force)
                result.indexed.append(p)
                del entries[p]
            except Exception as e:
                result.errors.append((p, repr(e)))
                # Keep the entry so the next hook call retries.
        result.remaining = len(entries)
        _save(qp, entries)
    return result


async def drain_all(
    *,
    indexer: Callable[[str, str | None, bool], Awaitable[None]],
    paths: Iterable[str] | None = None,
    queue_file: Path | None = None,
) -> DrainResult:
    """Synchronously drain every queued entry (or only ``paths`` when set).

    Blocks until every targeted entry has been indexed (or recorded as an
    error). Worst-case latency ≈ ``len(targets) × per_file_index_cost``.

    ``paths`` is reserved for RFC-B (PreCompact, deferred): when that
    contract specifies an in-flight file list at checkpoint time, the
    handler will pass it here for selective drain. Until then ``paths`` is
    always ``None`` and every queued entry drains.
    """
    qp = queue_file or queue_path()
    result = DrainResult()
    selected = set(paths) if paths is not None else None
    with _Lock(qp):
        entries = _load(qp)
        targets = [p for p in entries if (selected is None or p in selected)]
        for p in targets:
            entry = entries[p]
            try:
                await indexer(p, entry.namespace, entry.force)
                result.indexed.append(p)
                del entries[p]
            except Exception as e:
                result.errors.append((p, repr(e)))
        result.remaining = len(entries)
        _save(qp, entries)
    return result


def status_snapshot(*, queue_file: Path | None = None) -> StatusSnapshot:
    """Read-only snapshot — no lock, race-prone by design.

    Concurrent hook callers may add or drain entries between this read and
    whatever the caller does next. Treat the result as telemetry: queue
    depth and oldest entry give an operator a rough sense of how far behind
    the debounce queue is, but never use them to decide "is it safe to
    skip a flush?" — for that, call :func:`drain_all`, which gives a
    post-drain guarantee.
    """
    qp = queue_file or queue_path()
    entries = _load(qp)
    if not entries:
        return StatusSnapshot(depth=0, oldest_first_seen=None, oldest_path=None, queue_path=qp)
    oldest_path, oldest_entry = min(entries.items(), key=lambda kv: kv[1].first_seen)
    return StatusSnapshot(
        depth=len(entries),
        oldest_first_seen=oldest_entry.first_seen,
        oldest_path=oldest_path,
        queue_path=qp,
    )
