"""Debounce queue for hook-driven ``mm index`` calls.

Backs ``mm index --debounce-window`` (PR #536 documented gap close) for the
plugin's ``PostToolUse[Write]`` hook. The hook fires on every ``Write`` tool
use; without debouncing, codegen loops re-index the same file many times
within a few seconds. This module persists a per-path queue so a hook firing
in a burst can record the path cheaply and the *last* hook in the burst, or
a later flush, drains the entries that have been silent for at least the
debounce window.

The queue is a single JSON file under ``~/.memtomem/``, guarded by a
threading.Lock plus a ``portalocker`` lock on a *sidecar* file (see
:class:`_Lock` for why the queue file itself is the wrong thing to lock).
Each entry tracks ``first_seen``, ``last_seen``, plus the ``namespace`` and
``force`` flags that should apply to the eventual indexing call. When the
same path is enqueued again with different flags, last-write wins (the most
recent caller's intent).

Synchronization model:

- Every queue mutation (enqueue, drain) goes through :class:`_Lock`: an
  in-process ``threading.Lock`` keyed by the sidecar path, then ``LOCK_EX``
  on the sidecar. Concurrent threads and concurrent ``mm index
  --debounce-window`` processes both serialize without losing entries.
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

import asyncio
import json
import logging
import math
import os
import secrets
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import IO, Awaitable, Callable, Iterable, Literal

import portalocker

from memtomem.errors import PermanentError, RetryableError

logger = logging.getLogger(__name__)


_DEFAULT_QUEUE_PATH = Path("~/.memtomem/index_debounce_queue.json").expanduser()
_QUEUE_VERSION = 2

# Retryable failures must not stay queued forever — drain runs on every hook
# fire and every ``Stop``-hook ``--flush``, so a store outage that never clears
# would otherwise turn each into a guaranteed failure. After this many
# retryable attempts the entry is dropped loudly (logger.error +
# ``DrainResult.dropped``); a later real write re-enqueues it with a fresh
# budget. Permanent failures (parser errors, redaction blocks, malformed files)
# bypass this budget and are dropped on their first drain (#2026).
_MAX_DRAIN_ATTEMPTS = 5
# A claimed entry remains durable while its index callback runs.  If the
# process crashes, a later drainer may reclaim it after this lease rather than
# losing the path forever.  Indexing one file should remain far below an hour;
# the generous bound avoids duplicate expensive work under normal load.
_CLAIM_LEASE_S = 3600.0
_CLAIM_FUTURE_SKEW_S = 5.0


class DebounceQueueError(RuntimeError):
    """The durable queue could not be interpreted safely."""


@dataclass
class QueueEntry:
    """One queued path with its first-seen / last-seen timestamps and the
    indexing flags that should apply when it eventually drains."""

    first_seen: float
    last_seen: float
    namespace: str | None = None
    force: bool = False
    attempts: int = 0  # retryable drain attempts so far (see _MAX_DRAIN_ATTEMPTS)
    claim_id: str | None = None
    claimed_at: float | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "QueueEntry":
        if not isinstance(d, dict):
            raise DebounceQueueError("queue entry is not a JSON object")
        first_seen = float(d["first_seen"])
        last_seen = float(d["last_seen"])
        attempts = int(d.get("attempts", 0))
        claimed_at_raw = d.get("claimed_at")
        claimed_at = float(claimed_at_raw) if claimed_at_raw is not None else None
        if not all(math.isfinite(value) for value in (first_seen, last_seen)):
            raise DebounceQueueError("queue timestamps must be finite")
        if claimed_at is not None and not math.isfinite(claimed_at):
            raise DebounceQueueError("claim timestamp must be finite")
        if attempts < 0:
            raise DebounceQueueError("queue attempts must be non-negative")
        force = d.get("force", False)
        if not isinstance(force, bool):
            raise DebounceQueueError("queue force flag must be boolean")
        claim_id = d.get("claim_id")
        if claim_id is not None and not isinstance(claim_id, str):
            raise DebounceQueueError("queue claim_id must be a string")
        return cls(
            first_seen=first_seen,
            last_seen=last_seen,
            namespace=d.get("namespace"),
            force=force,
            attempts=attempts,
            claim_id=claim_id,
            claimed_at=claimed_at,
        )


@dataclass
class DrainResult:
    """Summary of a drain pass — what was indexed, what errored, what's left."""

    indexed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)  # (path, message)
    # Ordered same-entry subset of ``errors``. Kept additive so callers that
    # consume the historical pair lists do not need an ABI migration.
    retryable_errors: list[tuple[str, str]] = field(default_factory=list)
    dropped: list[tuple[str, str]] = field(default_factory=list)  # (path, message)
    # Ordered same-entry subset of ``dropped`` whose retry budget was exhausted.
    retryable_dropped: list[tuple[str, str]] = field(default_factory=list)
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
    except (OSError, json.JSONDecodeError) as exc:
        raise DebounceQueueError(f"debounce queue {path} is unreadable: {exc}") from exc
    if not isinstance(raw, dict):
        raise DebounceQueueError(f"debounce queue {path} root is not a JSON object")
    version = raw.get("version", 1)
    if not isinstance(version, int) or version < 1 or version > _QUEUE_VERSION:
        raise DebounceQueueError(
            f"debounce queue {path} has unsupported version {version!r}; refusing data loss"
        )
    entries = raw.get("entries", {})
    if not isinstance(entries, dict):
        raise DebounceQueueError(f"debounce queue {path} entries are not a JSON object")
    try:
        return {p: QueueEntry.from_dict(d) for p, d in entries.items() if isinstance(p, str)}
    except (KeyError, TypeError, ValueError) as exc:
        raise DebounceQueueError(f"debounce queue {path} has an invalid entry: {exc}") from exc


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
# file lock (``portalocker.lock``) is the cross-process barrier, but a
# *blocking* acquire does not reliably block on Windows: with 20 threads in
# one Python process contending on this sidecar, 9 of their blocking
# ``portalocker.lock(LOCK_EX)`` calls raised ``AlreadyLocked``
# (``EDEADLK``) instead of waiting, killing those threads and losing their
# entries — 11 of 20 landed (#759 failure 2). The backend is
# ``msvcrt.locking`` (portalocker's Windows exclusive default then, 3.2.0,
# and now): contention is detected, but the caller gets an exception rather
# than a queue slot. Holding a
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
      Required because a blocking file-lock acquire raises rather than
      waits under Windows contention (#759 failure 2; see the module
      comment above) — without this layer, a multi-threaded plugin host
      (e.g. Claude Code's bursty ``Write`` hook fanout) loses queue
      entries on Windows.
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
            # A write that lands while an older revision is being indexed is
            # new work.  Detach it from that claim so the old completion can
            # never delete the newly enqueued revision.
            existing.claim_id = None
            existing.claimed_at = None
        _save(qp, entries)


def _ready(entry: QueueEntry, window_seconds: float, now: float) -> bool:
    return (now - entry.last_seen) >= window_seconds


def _claimable(entry: QueueEntry, now: float) -> bool:
    if entry.claim_id is None:
        return True
    if entry.claimed_at is None:
        raise DebounceQueueError("claimed queue entry is missing claimed_at")
    # A timestamp implausibly ahead of the current clock is corruption or
    # clock rollback.  Treating it as an eternal live claim would silently
    # strand work; stealing it immediately could duplicate an active index.
    if entry.claimed_at > now + _CLAIM_FUTURE_SKEW_S:
        raise DebounceQueueError("queue claim timestamp is in the future")
    return now - entry.claimed_at >= _CLAIM_LEASE_S


def _claim_entries(
    qp: Path,
    *,
    now: float,
    predicate: Callable[[str, QueueEntry], bool],
) -> dict[str, tuple[str, QueueEntry]]:
    """Persist claims under lock, then return immutable callback inputs."""
    claimed: dict[str, tuple[str, QueueEntry]] = {}
    with _Lock(qp):
        entries = _load(qp)
        for path_str, entry in entries.items():
            if not predicate(path_str, entry) or not _claimable(entry, now):
                continue
            claim_id = secrets.token_hex(16)
            entry.claim_id = claim_id
            entry.claimed_at = now
            claimed[path_str] = (claim_id, QueueEntry.from_dict(asdict(entry)))
        if claimed:
            _save(qp, entries)
    return claimed


def _refresh_claim_if_owned(qp: Path, path_str: str, claim_id: str) -> bool:
    """Refresh one lease immediately before its callback, if still ours.

    A drain claims a bounded batch before running callbacks without the queue
    lock. Slow earlier callbacks can outlive the initial lease of later rows;
    reloading under lock here prevents their stale owner from indexing after a
    second drainer has legitimately reclaimed them.
    """
    with _Lock(qp):
        entries = _load(qp)
        current = entries.get(path_str)
        if current is None or current.claim_id != claim_id:
            return False
        current.claimed_at = time.time()
        _save(qp, entries)
        return True


async def _run_claims(
    qp: Path,
    claims: dict[str, tuple[str, QueueEntry]],
    indexer: Callable[[str, str | None, bool], Awaitable[Literal["indexed", "skipped"] | None]],
) -> DrainResult:
    """Run callbacks lock-free and settle only claims still owned by us."""
    outcomes: dict[str, tuple[Literal["indexed", "skipped", "error"], Exception | None]] = {}
    result = DrainResult()
    try:
        for path_str, (claim_id, entry) in claims.items():
            if not _refresh_claim_if_owned(qp, path_str, claim_id):
                continue
            try:
                outcome = await indexer(path_str, entry.namespace, entry.force)
                outcomes[path_str] = ("skipped" if outcome == "skipped" else "indexed", None)
                if outcome == "skipped":
                    result.skipped.append(path_str)
                else:
                    result.indexed.append(path_str)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                outcomes[path_str] = ("error", exc)
    except asyncio.CancelledError:
        # Stop hooks and CLI flushes can be cancelled by their host timeout.
        # Claims are durable, so leaving them behind would make every later
        # drainer wait for the one-hour crash lease. Release only rows whose
        # token still belongs to this drain; a concurrent enqueue clears the
        # token and must remain untouched as newer work.
        with _Lock(qp):
            entries = _load(qp)
            changed = False
            for path_str, (claim_id, _entry) in claims.items():
                current = entries.get(path_str)
                if current is None or current.claim_id != claim_id:
                    continue
                current.claim_id = None
                current.claimed_at = None
                changed = True
            if changed:
                _save(qp, entries)
        raise

    with _Lock(qp):
        entries = _load(qp)
        for path_str, (claim_id, _claimed_entry) in claims.items():
            current = entries.get(path_str)
            # A concurrent enqueue cleared/replaced this claim.  Its newer
            # revision remains queued regardless of this callback's outcome.
            if current is None or current.claim_id != claim_id:
                continue
            settlement_outcome, settlement_exc = outcomes[path_str]
            if settlement_outcome != "error":
                del entries[path_str]
                continue
            assert settlement_exc is not None
            current.claim_id = None
            current.claimed_at = None
            _record_failure(entries, path_str, current, settlement_exc, result)
        result.remaining = len(entries)
        _save(qp, entries)
    return result


def _record_failure(
    entries: dict[str, QueueEntry],
    path_str: str,
    entry: QueueEntry,
    exc: Exception,
    result: DrainResult,
) -> None:
    """Record one classified failure and update the persistent queue.

    Typed/explicitly permanent failures are dropped immediately. Retryable and
    unknown failures consume the bounded budget: the cap already prevents an
    unclassified exception from retrying forever, while treating it as
    permanent could silently discard work during a transient outage.
    """
    message = repr(exc)
    item = (path_str, message)
    marker = getattr(exc, "retryable", None)
    explicitly_retryable = isinstance(exc, (RetryableError, TimeoutError)) or marker is True
    explicitly_permanent = isinstance(exc, PermanentError) or marker is False
    if explicitly_permanent and not explicitly_retryable:
        del entries[path_str]
        result.dropped.append(item)
        logger.error(
            "debounce queue: dropping %s after permanent indexing failure (%s); "
            "fix the underlying cause and re-run: mm index %s",
            path_str,
            message,
            path_str,
        )
        return

    entry.attempts += 1
    if entry.attempts < _MAX_DRAIN_ATTEMPTS:
        result.errors.append(item)
        result.retryable_errors.append(item)
        return
    del entries[path_str]
    result.dropped.append(item)
    result.retryable_dropped.append(item)
    logger.error(
        "debounce queue: dropping %s after %d retryable indexing attempts (%s); "
        "re-run once the transient cause clears: mm index %s",
        path_str,
        entry.attempts,
        message,
        path_str,
    )


async def drain_ready(
    *,
    window_seconds: float,
    indexer: Callable[[str, str | None, bool], Awaitable[Literal["indexed", "skipped"] | None]],
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
    claims = _claim_entries(
        qp,
        now=ts,
        predicate=lambda _path, entry: _ready(entry, window_seconds, ts),
    )
    return await _run_claims(qp, claims, indexer)


async def drain_all(
    *,
    indexer: Callable[[str, str | None, bool], Awaitable[Literal["indexed", "skipped"] | None]],
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
    selected = set(paths) if paths is not None else None
    now = time.time()
    claims = _claim_entries(
        qp,
        now=now,
        predicate=lambda path_str, _entry: selected is None or path_str in selected,
    )
    return await _run_claims(qp, claims, indexer)


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
