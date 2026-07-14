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
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import IO, Awaitable, Callable, Iterable, Literal

import portalocker

logger = logging.getLogger(__name__)


_DEFAULT_QUEUE_PATH = Path("~/.memtomem/index_debounce_queue.json").expanduser()
_QUEUE_VERSION = 1

# A deterministically-failing entry (permission error, parser bug, redaction
# block) must not be retried forever — drain runs on every hook fire and every
# ``Stop``-hook ``--flush``, so a poison entry turns each of those into a
# guaranteed failure. After this many failed drain attempts the entry is
# dropped loudly (logger.error + ``DrainResult.dropped``). A later re-enqueue
# (i.e. a real new write to the file) resets the counter. This cap applies to
# redaction-blocked files too — exempting them would reintroduce the
# unbounded-retry class this exists to remove (#1574 item 3).
_MAX_DRAIN_ATTEMPTS = 5


@dataclass
class QueueEntry:
    """One queued path with its first-seen / last-seen timestamps and the
    indexing flags that should apply when it eventually drains."""

    first_seen: float
    last_seen: float
    namespace: str | None = None
    force: bool = False
    attempts: int = 0  # failed drain attempts so far (see _MAX_DRAIN_ATTEMPTS)

    @classmethod
    def from_dict(cls, d: dict) -> "QueueEntry":
        return cls(
            first_seen=float(d["first_seen"]),
            last_seen=float(d["last_seen"]),
            namespace=d.get("namespace"),
            force=bool(d.get("force", False)),
            attempts=int(d.get("attempts", 0)),
        )


@dataclass
class DrainResult:
    """Summary of a drain pass — what was indexed, what errored, what's left."""

    indexed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)  # (path, message)
    dropped: list[tuple[str, str]] = field(default_factory=list)  # (path, message)
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


# Per-lockfile ``threading.Lock`` for intra-process serialization. The
# file lock (``portalocker.lock``) is the cross-process barrier, but on
# Windows ``LockFileEx`` does not reliably block a *second handle* from
# the *same* process holding the file open through ``open(..., "a+b")`` —
# two threads in one Python process each opening the sidecar lockfile
# can both pass ``portalocker.lock(LOCK_EX)`` and race the read-modify-
# write of the queue, losing entries (#759 failure 2). Holding a
# threading.Lock keyed to the lockfile path before acquiring the file
# lock collapses same-process contention to a single waiter, so
# portalocker only ever sees one handle per process competing.
_intra_process_locks: dict[Path, threading.Lock] = {}
_intra_process_locks_guard = threading.Lock()


def _intra_process_lock_for(path: Path) -> threading.Lock:
    """Return the threading.Lock associated with ``path``, creating it
    on first use. The dict accumulates one entry per distinct lockfile
    path observed in the process — bounded in practice by the number of
    queue files (one in normal usage)."""
    with _intra_process_locks_guard:
        lock = _intra_process_locks.get(path)
        if lock is None:
            lock = threading.Lock()
            _intra_process_locks[path] = lock
        return lock


class _Lock:
    """``portalocker.lock(LOCK_EX)`` on a sidecar lockfile next to the queue.

    The lockfile is deliberately *not* the queue file itself. ``_save``
    replaces the queue via ``os.replace``, which rebinds the path to a
    fresh inode mid-critical-section. If we locked the queue file, the
    lock would attach to the now-unlinked old inode while later callers
    open the new inode and obtain an uncontended lock — concurrent
    writers would lose entries.

    The sidecar (``.<queue_name>.lock``) is never replaced; every
    process locks the same inode for the duration of its critical
    section, so serialization is correct across processes.

    Two-layer locking (#759):

    - **Intra-process** ``threading.Lock`` keyed by the lockfile path
      (``_intra_process_lock_for``). Acquired first; serializes threads
      inside a single Python process before any file-handle work.
      Required because Windows ``LockFileEx`` does not reliably block a
      second handle from the same process — without this layer, a
      multi-threaded plugin host (e.g. Claude Code's bursty ``Write``
      hook fanout) loses queue entries on Windows.
    - **Cross-process** ``portalocker.lock(LOCK_EX)`` on the sidecar
      lockfile. Acquired second; serializes parallel ``mm index
      --debounce-window`` invocations that don't share Python state.

    Both layers release in reverse order in ``__exit__``.
    """

    def __init__(self, path: Path) -> None:
        self._lock_path = path.parent / f".{path.name}.lock"
        self._intra_lock = _intra_process_lock_for(self._lock_path)
        self._fp: IO[bytes] | None = None

    def __enter__(self) -> "_Lock":
        self._intra_lock.acquire()
        try:
            self._lock_path.parent.mkdir(parents=True, exist_ok=True)
            self._fp = open(self._lock_path, "a+b")
            portalocker.lock(self._fp, portalocker.LOCK_EX)
        except BaseException:
            # File-lock acquisition failed — must release the intra
            # lock so other threads aren't permanently blocked.
            self._intra_lock.release()
            raise
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self._fp is None:
                return
            portalocker.unlock(self._fp)
            self._fp.close()
            self._fp = None
        finally:
            self._intra_lock.release()


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
            # A re-enqueue is a real new write (the only caller is the
            # PostToolUse[Write] hook), so give the entry a fresh retry
            # budget — the failure may have been fixed by this write.
            existing.attempts = 0
        _save(qp, entries)


def _ready(entry: QueueEntry, window_seconds: float, now: float) -> bool:
    return (now - entry.last_seen) >= window_seconds


def _record_failure(
    entries: dict[str, QueueEntry],
    path_str: str,
    entry: QueueEntry,
    exc: Exception,
    result: DrainResult,
) -> None:
    """Count one failed drain attempt; keep the entry for retry until the
    attempt cap is hit, then drop it loudly (log + ``result.dropped``)."""
    message = repr(exc)
    entry.attempts += 1
    if entry.attempts < _MAX_DRAIN_ATTEMPTS:
        result.errors.append((path_str, message))
        return
    del entries[path_str]
    result.dropped.append((path_str, message))
    logger.error(
        "debounce queue: dropping %s after %d failed indexing attempts (%s); "
        "fix the underlying cause and re-run: mm index %s",
        path_str,
        entry.attempts,
        message,
        path_str,
    )


async def drain_ready(
    *,
    window_seconds: float,
    indexer: Callable[
        [str, str | None, bool], Awaitable[Literal["indexed", "skipped"] | None]
    ],
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
                outcome = await indexer(p, entry.namespace, entry.force)
                if outcome == "skipped":
                    result.skipped.append(p)
                else:
                    # ``None`` remains the backward-compatible success value
                    # for existing callback implementations and test doubles.
                    result.indexed.append(p)
                del entries[p]
            except Exception as e:
                # Keep the entry so the next hook call retries — until the
                # attempt cap drops it (poison-entry guard, #1574 item 3).
                _record_failure(entries, p, entry, e, result)
        result.remaining = len(entries)
        _save(qp, entries)
    return result


async def drain_all(
    *,
    indexer: Callable[
        [str, str | None, bool], Awaitable[Literal["indexed", "skipped"] | None]
    ],
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
                outcome = await indexer(p, entry.namespace, entry.force)
                if outcome == "skipped":
                    result.skipped.append(p)
                else:
                    result.indexed.append(p)
                del entries[p]
            except Exception as e:
                # Same poison-entry cap as drain_ready — this path runs on
                # every Stop-hook ``--flush``, the highest-frequency
                # automated drain, so it must not bypass the cap.
                _record_failure(entries, p, entry, e, result)
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
