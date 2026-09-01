"""Tests for ``memtomem.indexing.debounce``.

The debounce module is the file-system substrate behind ``mm index
--debounce-window`` (PR #536 documented gap close). The tests pin three
contracts:

1. **Enqueue semantics** — last-write-wins for namespace/force, ``last_seen``
   pushes forward on every call, ``first_seen`` is set once.
2. **Drain semantics** — ``drain_ready`` indexes only entries that have been
   silent at least ``window_seconds``; ``drain_all`` indexes everything;
   retryable errors stay queued while permanent errors are dropped immediately.
3. **Concurrency + persistence** — the queue persists across calls,
   ``_Lock``'s two layers (in-process ``threading.Lock`` + sidecar flock)
   serialize parallel mutations, and ``status_snapshot`` reads without a
   lock (race-prone by design).
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest

from memtomem.errors import PermanentError, RetryableError
from memtomem.indexing import debounce


@pytest.fixture
def queue_file(tmp_path: Path) -> Path:
    return tmp_path / "index_debounce_queue.json"


def _read_raw(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class TestEnqueue:
    def test_first_enqueue_creates_first_seen_and_last_seen(self, queue_file: Path) -> None:
        debounce.enqueue("/tmp/file.py", now=100.0, queue_file=queue_file)
        raw = _read_raw(queue_file)
        entry = raw["entries"]["/tmp/file.py"]
        assert entry["first_seen"] == 100.0
        assert entry["last_seen"] == 100.0
        assert entry["namespace"] is None
        assert entry["force"] is False

    def test_repeated_enqueue_updates_last_seen_only(self, queue_file: Path) -> None:
        debounce.enqueue("/tmp/file.py", now=100.0, queue_file=queue_file)
        debounce.enqueue("/tmp/file.py", now=105.0, queue_file=queue_file)
        debounce.enqueue("/tmp/file.py", now=110.0, queue_file=queue_file)
        entry = _read_raw(queue_file)["entries"]["/tmp/file.py"]
        assert entry["first_seen"] == 100.0
        assert entry["last_seen"] == 110.0

    def test_last_write_wins_for_namespace_and_force(self, queue_file: Path) -> None:
        """The most recent caller's intent applies on drain — same path
        enqueued twice with different flags resolves to the second call's
        values, not the first."""
        debounce.enqueue(
            "/tmp/file.py", now=100.0, namespace="foo", force=False, queue_file=queue_file
        )
        debounce.enqueue(
            "/tmp/file.py", now=105.0, namespace="bar", force=True, queue_file=queue_file
        )
        entry = _read_raw(queue_file)["entries"]["/tmp/file.py"]
        assert entry["namespace"] == "bar"
        assert entry["force"] is True

    def test_distinct_paths_kept_separately(self, queue_file: Path) -> None:
        debounce.enqueue("/tmp/a.py", now=100.0, queue_file=queue_file)
        debounce.enqueue("/tmp/b.py", now=101.0, queue_file=queue_file)
        entries = _read_raw(queue_file)["entries"]
        assert set(entries.keys()) == {"/tmp/a.py", "/tmp/b.py"}


class TestDrainReady:
    """``drain_ready`` is what ``mm index --debounce-window`` calls on every
    hook fire. The contract: index files silent ≥ ``window_seconds``, leave
    the rest. The caller's own enqueue (which set ``last_seen`` to ``now``)
    must not qualify on its own call — otherwise the debounce window
    collapses to zero and we re-index every Write immediately.
    """

    def test_recently_seen_entry_is_not_drained(self, queue_file: Path) -> None:
        debounce.enqueue("/tmp/file.py", now=100.0, queue_file=queue_file)
        indexed: list[str] = []

        async def indexer(p: str, ns: str | None, force: bool) -> None:
            indexed.append(p)

        # 2s after enqueue, window=5s → not ready.
        result = asyncio.run(
            debounce.drain_ready(
                window_seconds=5.0, indexer=indexer, now=102.0, queue_file=queue_file
            )
        )
        assert result.indexed == []
        assert result.remaining == 1

    def test_silent_entry_drains_after_window(self, queue_file: Path) -> None:
        debounce.enqueue("/tmp/file.py", now=100.0, queue_file=queue_file)
        indexed: list[str] = []

        async def indexer(p: str, ns: str | None, force: bool) -> None:
            indexed.append(p)

        # 6s later, window=5s → ready.
        result = asyncio.run(
            debounce.drain_ready(
                window_seconds=5.0, indexer=indexer, now=106.0, queue_file=queue_file
            )
        )
        assert result.indexed == ["/tmp/file.py"]
        assert result.remaining == 0
        assert "/tmp/file.py" not in _read_raw(queue_file)["entries"]

    def test_mixed_queue_drains_only_ready(self, queue_file: Path) -> None:
        """Entry A enqueued 10s ago is ready; entry B enqueued just now is
        not. Only A is indexed; B remains queued for the next call."""
        debounce.enqueue("/tmp/old.py", now=90.0, queue_file=queue_file)
        debounce.enqueue("/tmp/new.py", now=100.0, queue_file=queue_file)
        indexed: list[str] = []

        async def indexer(p: str, ns: str | None, force: bool) -> None:
            indexed.append(p)

        result = asyncio.run(
            debounce.drain_ready(
                window_seconds=5.0, indexer=indexer, now=100.5, queue_file=queue_file
            )
        )
        assert result.indexed == ["/tmp/old.py"]
        assert result.remaining == 1
        assert list(_read_raw(queue_file)["entries"].keys()) == ["/tmp/new.py"]

    def test_retryable_indexer_error_keeps_entry_for_retry(self, queue_file: Path) -> None:
        """A typed retryable failure stays queued for the next hook call."""
        debounce.enqueue("/tmp/broken.py", now=100.0, queue_file=queue_file)

        async def indexer(p: str, ns: str | None, force: bool) -> None:
            raise RetryableError("synthetic indexing failure")

        result = asyncio.run(
            debounce.drain_ready(
                window_seconds=5.0, indexer=indexer, now=110.0, queue_file=queue_file
            )
        )
        assert result.indexed == []
        assert len(result.errors) == 1
        assert result.errors[0][0] == "/tmp/broken.py"
        assert result.retryable_errors == result.errors
        assert result.remaining == 1

    def test_permanent_indexer_error_drops_immediately(self, queue_file: Path) -> None:
        debounce.enqueue("/tmp/broken.py", now=100.0, queue_file=queue_file)

        async def indexer(p: str, ns: str | None, force: bool) -> None:
            raise PermanentError("synthetic permanent failure")

        result = asyncio.run(
            debounce.drain_ready(
                window_seconds=5.0, indexer=indexer, now=110.0, queue_file=queue_file
            )
        )

        assert result.errors == []
        assert [p for p, _ in result.dropped] == ["/tmp/broken.py"]
        assert result.retryable_dropped == []
        assert result.remaining == 0
        assert _read_raw(queue_file)["entries"] == {}

    def test_unknown_indexer_error_uses_bounded_retry_budget(self, queue_file: Path) -> None:
        """Unclassified failures may be transient, so the cap—not the first
        attempt—is the safe fallback that prevents both loss and infinite retry."""
        debounce.enqueue("/tmp/unknown.py", now=100.0, queue_file=queue_file)

        async def indexer(p: str, ns: str | None, force: bool) -> None:
            raise RuntimeError("unclassified store failure")

        result = asyncio.run(
            debounce.drain_ready(
                window_seconds=5.0, indexer=indexer, now=110.0, queue_file=queue_file
            )
        )

        assert result.retryable_errors == result.errors
        assert result.dropped == []
        assert result.remaining == 1
        assert _read_raw(queue_file)["entries"]["/tmp/unknown.py"]["attempts"] == 1

    def test_indexer_receives_namespace_and_force_from_entry(self, queue_file: Path) -> None:
        debounce.enqueue(
            "/tmp/file.py",
            now=100.0,
            namespace="claude-memory:project-x",
            force=True,
            queue_file=queue_file,
        )
        captured: dict = {}

        async def indexer(p: str, ns: str | None, force: bool) -> None:
            captured.update(path=p, namespace=ns, force=force)

        asyncio.run(
            debounce.drain_ready(
                window_seconds=5.0, indexer=indexer, now=110.0, queue_file=queue_file
            )
        )
        assert captured == {
            "path": "/tmp/file.py",
            "namespace": "claude-memory:project-x",
            "force": True,
        }


class TestDrainAll:
    """``drain_all`` backs ``mm index --flush``. Every queued entry indexes
    regardless of last-seen age; the call blocks until done. Reserves the
    ``paths`` filter for RFC-B (PreCompact, deferred) selective payload."""

    def test_drains_every_entry(self, queue_file: Path) -> None:
        debounce.enqueue("/tmp/a.py", now=100.0, queue_file=queue_file)
        debounce.enqueue("/tmp/b.py", now=100.5, queue_file=queue_file)
        debounce.enqueue("/tmp/c.py", now=101.0, queue_file=queue_file)
        indexed: list[str] = []

        async def indexer(p: str, ns: str | None, force: bool) -> None:
            indexed.append(p)

        result = asyncio.run(debounce.drain_all(indexer=indexer, queue_file=queue_file))
        assert sorted(result.indexed) == ["/tmp/a.py", "/tmp/b.py", "/tmp/c.py"]
        assert result.remaining == 0
        assert _read_raw(queue_file)["entries"] == {}

    def test_paths_filter_drains_subset_only(self, queue_file: Path) -> None:
        """Future-extensibility check for RFC-B: passing ``paths=[...]``
        drains only those, leaves others. Today the CLI never passes
        ``paths``; this test pins the contract for the deferred handler."""
        debounce.enqueue("/tmp/a.py", now=100.0, queue_file=queue_file)
        debounce.enqueue("/tmp/b.py", now=100.0, queue_file=queue_file)
        indexed: list[str] = []

        async def indexer(p: str, ns: str | None, force: bool) -> None:
            indexed.append(p)

        result = asyncio.run(
            debounce.drain_all(indexer=indexer, paths=["/tmp/a.py"], queue_file=queue_file)
        )
        assert result.indexed == ["/tmp/a.py"]
        assert result.remaining == 1
        assert list(_read_raw(queue_file)["entries"].keys()) == ["/tmp/b.py"]

    def test_empty_queue_is_no_op(self, queue_file: Path) -> None:
        async def indexer(p: str, ns: str | None, force: bool) -> None:
            raise AssertionError("indexer must not be called on empty queue")

        result = asyncio.run(debounce.drain_all(indexer=indexer, queue_file=queue_file))
        assert result.indexed == []
        assert result.remaining == 0

    def test_terminal_noop_is_reported_as_skipped(self, queue_file: Path) -> None:
        debounce.enqueue("/tmp/removed.py", now=100.0, queue_file=queue_file)

        async def indexer(p: str, ns: str | None, force: bool) -> str:
            return "skipped"

        result = asyncio.run(debounce.drain_all(indexer=indexer, queue_file=queue_file))

        assert result.indexed == []
        assert result.skipped == ["/tmp/removed.py"]
        assert result.errors == []
        assert result.remaining == 0

    def test_permanent_failure_drops_immediately(self, queue_file: Path) -> None:
        debounce.enqueue("/tmp/broken.py", now=100.0, queue_file=queue_file)

        async def indexer(p: str, ns: str | None, force: bool) -> None:
            raise PermanentError("synthetic permanent failure")

        result = asyncio.run(debounce.drain_all(indexer=indexer, queue_file=queue_file))

        assert result.errors == []
        assert [p for p, _ in result.dropped] == ["/tmp/broken.py"]
        assert result.retryable_dropped == []
        assert result.remaining == 0
        assert _read_raw(queue_file)["entries"] == {}


class TestRetryableEntryCap:
    """A retryable-but-still-failing entry is retried up to
    ``_MAX_DRAIN_ATTEMPTS`` and then dropped loudly — logged, removed from
    the queue, and reported via ``DrainResult.dropped`` (#1574, #2026).
    Without the cap, every hook fire and every Stop-hook ``--flush`` retries
    the unavailable store forever."""

    @staticmethod
    def _failing_indexer():
        async def indexer(p: str, ns: str | None, force: bool) -> None:
            raise RetryableError("synthetic retryable failure")

        return indexer

    def test_entry_dropped_after_max_attempts(self, queue_file: Path, caplog) -> None:
        debounce.enqueue("/tmp/poison.py", now=100.0, queue_file=queue_file)
        indexer = self._failing_indexer()

        for attempt in range(1, debounce._MAX_DRAIN_ATTEMPTS):
            result = asyncio.run(
                debounce.drain_ready(
                    window_seconds=5.0, indexer=indexer, now=110.0, queue_file=queue_file
                )
            )
            assert result.dropped == []
            assert result.retryable_errors == result.errors
            assert result.remaining == 1
            # ``attempts`` persists on disk between drains (survives process
            # restarts — the queue outlives any one hook invocation).
            assert _read_raw(queue_file)["entries"]["/tmp/poison.py"]["attempts"] == attempt

        with caplog.at_level("ERROR", logger="memtomem.indexing.debounce"):
            result = asyncio.run(
                debounce.drain_ready(
                    window_seconds=5.0, indexer=indexer, now=110.0, queue_file=queue_file
                )
            )
        assert result.indexed == []
        assert result.errors == []
        assert len(result.dropped) == 1
        assert result.dropped[0][0] == "/tmp/poison.py"
        assert result.retryable_dropped == result.dropped
        assert result.remaining == 0
        assert "/tmp/poison.py" not in _read_raw(queue_file)["entries"]
        dropped_logs = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
        assert any("mm index /tmp/poison.py" in m for m in dropped_logs), (
            "the drop must be loud and name the remediation command"
        )

        # Genuinely gone: a later flush must see an empty queue, not replay the
        # entry whose retry budget was exhausted.
        empty = asyncio.run(debounce.drain_all(indexer=indexer, queue_file=queue_file))
        assert empty.indexed == []
        assert empty.errors == []
        assert empty.dropped == []
        assert empty.remaining == 0

    def test_drain_all_applies_the_same_cap(self, queue_file: Path) -> None:
        """``drain_all`` backs the Stop-hook ``mm index --flush`` — the
        highest-frequency automated drain — so it must not bypass the cap."""
        debounce.enqueue("/tmp/poison.py", now=100.0, queue_file=queue_file)
        indexer = self._failing_indexer()

        for _ in range(debounce._MAX_DRAIN_ATTEMPTS - 1):
            result = asyncio.run(debounce.drain_all(indexer=indexer, queue_file=queue_file))
            assert result.dropped == []
        result = asyncio.run(debounce.drain_all(indexer=indexer, queue_file=queue_file))
        assert [p for p, _ in result.dropped] == ["/tmp/poison.py"]
        assert result.retryable_dropped == result.dropped
        assert result.remaining == 0
        assert _read_raw(queue_file)["entries"] == {}

    def test_reenqueue_resets_attempts(self, queue_file: Path) -> None:
        """A re-enqueue is a real new write (the only enqueue caller is the
        PostToolUse[Write] hook), so the entry gets a fresh retry budget —
        the write may have fixed the failure."""
        debounce.enqueue("/tmp/flaky.py", now=100.0, queue_file=queue_file)
        indexer = self._failing_indexer()
        for _ in range(debounce._MAX_DRAIN_ATTEMPTS - 1):
            asyncio.run(
                debounce.drain_ready(
                    window_seconds=5.0, indexer=indexer, now=110.0, queue_file=queue_file
                )
            )
        entries = _read_raw(queue_file)["entries"]
        assert entries["/tmp/flaky.py"]["attempts"] == debounce._MAX_DRAIN_ATTEMPTS - 1

        debounce.enqueue("/tmp/flaky.py", now=120.0, queue_file=queue_file)
        assert _read_raw(queue_file)["entries"]["/tmp/flaky.py"]["attempts"] == 0

    def test_success_after_failures_clears_entry(self, queue_file: Path) -> None:
        """An entry that eventually succeeds is drained normally — the cap
        only ever drops entries that fail on their final attempt."""
        debounce.enqueue("/tmp/flaky.py", now=100.0, queue_file=queue_file)
        calls = {"n": 0}

        async def indexer(p: str, ns: str | None, force: bool) -> None:
            calls["n"] += 1
            if calls["n"] < 3:
                raise RetryableError("transient")

        for _ in range(3):
            result = asyncio.run(
                debounce.drain_ready(
                    window_seconds=5.0, indexer=indexer, now=110.0, queue_file=queue_file
                )
            )
        assert result.indexed == ["/tmp/flaky.py"]
        assert result.dropped == []
        assert _read_raw(queue_file)["entries"] == {}

    def test_timeout_error_is_retryable(self, queue_file: Path) -> None:
        """The bounded sidecar-lock timeout is transient even though the
        built-in exception does not subclass ``RetryableError``."""
        debounce.enqueue("/tmp/locked.py", now=100.0, queue_file=queue_file)

        async def indexer(p: str, ns: str | None, force: bool) -> None:
            raise TimeoutError("sidecar busy")

        result = asyncio.run(debounce.drain_all(indexer=indexer, queue_file=queue_file))

        assert result.retryable_errors == result.errors
        assert result.dropped == []
        assert result.remaining == 1

    def test_mixed_pass_indexes_retries_and_drops(self, queue_file: Path) -> None:
        for path in ("/tmp/ok.py", "/tmp/retry.py", "/tmp/permanent.py"):
            debounce.enqueue(path, now=100.0, queue_file=queue_file)

        async def indexer(p: str, ns: str | None, force: bool) -> None:
            if p.endswith("retry.py"):
                raise RetryableError("store unavailable")
            if p.endswith("permanent.py"):
                raise PermanentError("malformed input")

        result = asyncio.run(debounce.drain_all(indexer=indexer, queue_file=queue_file))

        assert result.indexed == ["/tmp/ok.py"]
        assert [p for p, _ in result.errors] == ["/tmp/retry.py"]
        assert result.retryable_errors == result.errors
        assert [p for p, _ in result.dropped] == ["/tmp/permanent.py"]
        assert result.retryable_dropped == []
        assert result.remaining == 1
        assert list(_read_raw(queue_file)["entries"]) == ["/tmp/retry.py"]

    def test_retryable_then_permanent_drops_without_spending_remaining_budget(
        self, queue_file: Path
    ) -> None:
        debounce.enqueue("/tmp/flaky.py", now=100.0, queue_file=queue_file)
        calls = {"n": 0}

        async def indexer(p: str, ns: str | None, force: bool) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RetryableError("store unavailable")
            raise PermanentError("malformed after retry")

        first = asyncio.run(debounce.drain_all(indexer=indexer, queue_file=queue_file))
        assert first.retryable_errors == first.errors
        assert _read_raw(queue_file)["entries"]["/tmp/flaky.py"]["attempts"] == 1

        second = asyncio.run(debounce.drain_all(indexer=indexer, queue_file=queue_file))
        assert second.errors == []
        assert [p for p, _ in second.dropped] == ["/tmp/flaky.py"]
        assert second.retryable_dropped == []
        assert second.remaining == 0

    def test_legacy_queue_file_without_attempts_loads_as_zero(self, queue_file: Path) -> None:
        """Queue files written before the ``attempts`` field must round-trip:
        missing key loads as 0 and the first failure counts from there."""
        queue_file.parent.mkdir(parents=True, exist_ok=True)
        queue_file.write_text(
            json.dumps(
                {
                    "version": 1,
                    "entries": {
                        "/tmp/legacy.py": {
                            "first_seen": 100.0,
                            "last_seen": 100.0,
                            "namespace": None,
                            "force": False,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        indexer = self._failing_indexer()
        result = asyncio.run(
            debounce.drain_ready(
                window_seconds=5.0, indexer=indexer, now=110.0, queue_file=queue_file
            )
        )
        assert result.dropped == []
        assert _read_raw(queue_file)["entries"]["/tmp/legacy.py"]["attempts"] == 1


class TestStatusSnapshot:
    """``status_snapshot`` is read-without-lock. Concurrent enqueues may
    race the read; the docstring on :func:`status_snapshot` flags this so
    callers don't try status-then-flush as a correctness pattern. The
    tests just pin the shape; the race is the *contract*, not a bug to
    catch."""

    def test_empty_queue_returns_zero_depth(self, queue_file: Path) -> None:
        snap = debounce.status_snapshot(queue_file=queue_file)
        assert snap.depth == 0
        assert snap.oldest_path is None
        assert snap.oldest_first_seen is None

    def test_oldest_entry_wins_by_first_seen(self, queue_file: Path) -> None:
        debounce.enqueue("/tmp/recent.py", now=200.0, queue_file=queue_file)
        debounce.enqueue("/tmp/old.py", now=100.0, queue_file=queue_file)
        debounce.enqueue("/tmp/middle.py", now=150.0, queue_file=queue_file)
        snap = debounce.status_snapshot(queue_file=queue_file)
        assert snap.depth == 3
        assert snap.oldest_path == "/tmp/old.py"
        assert snap.oldest_first_seen == 100.0


class TestPersistenceAndConcurrency:
    def test_queue_persists_across_calls(self, queue_file: Path) -> None:
        """Each ``enqueue`` round-trips through disk; the next call sees the
        previous state. This is the load-bearing property — without it,
        the hook caller would always start with an empty queue and the
        debounce window would never fire."""
        debounce.enqueue("/tmp/a.py", now=100.0, queue_file=queue_file)
        debounce.enqueue("/tmp/b.py", now=101.0, queue_file=queue_file)
        snap = debounce.status_snapshot(queue_file=queue_file)
        assert snap.depth == 2

    def test_concurrent_enqueue_does_not_lose_entries(self, queue_file: Path) -> None:
        """Threads enqueue distinct paths in parallel. ``_Lock`` guarantees
        every write lands — for same-process threads that is the
        in-process ``threading.Lock`` layer. Without it, the second
        writer's load+save would clobber the first writer's entry."""
        threads: list[threading.Thread] = []
        for i in range(20):
            t = threading.Thread(
                target=debounce.enqueue,
                args=(f"/tmp/file_{i:02d}.py",),
                kwargs={"now": 100.0 + i, "queue_file": queue_file},
            )
            threads.append(t)
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        entries = _read_raw(queue_file)["entries"]
        assert len(entries) == 20

    def test_intra_process_lock_serializes_threads_when_file_lock_is_noop(
        self, queue_file: Path, monkeypatch
    ) -> None:
        """Pin the in-process ``threading.Lock`` independently of the
        cross-process file lock.

        On Windows a contended blocking acquire raises ``AlreadyLocked``
        instead of waiting, so with the file lock as the only barrier the
        losing threads died mid-enqueue and their entries never landed
        (#759 failure 2: 11 of 20). Stubbing ``portalocker.lock`` /
        ``unlock`` to no-ops does not reproduce that failure mode; it
        removes the file-lock layer entirely, so the test passes on every
        platform only if the intra-process ``threading.Lock`` serializes
        on its own. Removing the
        threading.Lock layer from ``_Lock`` flips this to a flaky
        ``len(entries) < 20`` failure.
        """
        monkeypatch.setattr(debounce.portalocker, "lock", lambda *a, **kw: None)
        monkeypatch.setattr(debounce.portalocker, "unlock", lambda *a, **kw: None)

        threads: list[threading.Thread] = []
        for i in range(20):
            t = threading.Thread(
                target=debounce.enqueue,
                args=(f"/tmp/file_{i:02d}.py",),
                kwargs={"now": 100.0 + i, "queue_file": queue_file},
            )
            threads.append(t)
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        entries = _read_raw(queue_file)["entries"]
        assert len(entries) == 20

    def test_partial_drain_writes_remaining_back(self, queue_file: Path) -> None:
        """Two entries, one fails retryably. The successful one is gone from
        disk; the retryable one remains for the next hook call."""
        debounce.enqueue("/tmp/good.py", now=100.0, queue_file=queue_file)
        debounce.enqueue("/tmp/bad.py", now=100.0, queue_file=queue_file)

        async def indexer(p: str, ns: str | None, force: bool) -> None:
            if p == "/tmp/bad.py":
                raise RetryableError("boom")

        asyncio.run(
            debounce.drain_ready(
                window_seconds=5.0, indexer=indexer, now=110.0, queue_file=queue_file
            )
        )
        remaining = _read_raw(queue_file)["entries"]
        assert list(remaining.keys()) == ["/tmp/bad.py"]

    def test_index_callback_runs_without_holding_queue_lock(self, queue_file: Path) -> None:
        debounce.enqueue("/tmp/file.py", now=100.0, queue_file=queue_file)

        async def indexer(path: str, _ns: str | None, _force: bool) -> None:
            # This would deadlock when drain held _Lock across the await.
            debounce.enqueue(path, now=120.0, queue_file=queue_file)

        result = asyncio.run(
            debounce.drain_ready(
                window_seconds=5.0,
                indexer=indexer,
                now=110.0,
                queue_file=queue_file,
            )
        )
        assert result.indexed == ["/tmp/file.py"]
        remaining = _read_raw(queue_file)["entries"]
        assert remaining["/tmp/file.py"]["last_seen"] == 120.0
        assert remaining["/tmp/file.py"]["claim_id"] is None

    def test_expired_durable_claim_is_recovered(self, queue_file: Path) -> None:
        debounce.enqueue("/tmp/file.py", now=100.0, queue_file=queue_file)
        raw = _read_raw(queue_file)
        raw["entries"]["/tmp/file.py"].update({"claim_id": "crashed", "claimed_at": 101.0})
        queue_file.write_text(json.dumps(raw), encoding="utf-8")

        async def indexer(*_args) -> None:
            return None

        result = asyncio.run(
            debounce.drain_ready(
                window_seconds=5.0,
                indexer=indexer,
                now=101.0 + debounce._CLAIM_LEASE_S,
                queue_file=queue_file,
            )
        )
        assert result.indexed == ["/tmp/file.py"]

    def test_late_batch_row_is_not_run_after_another_drain_reclaims_it(
        self, queue_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        debounce.enqueue("/tmp/a.py", now=100.0, queue_file=queue_file)
        debounce.enqueue("/tmp/b.py", now=100.0, queue_file=queue_file)
        clock = [100.0]
        monkeypatch.setattr(debounce.time, "time", lambda: clock[0])

        async def scenario() -> None:
            first_started = asyncio.Event()
            release_first = asyncio.Event()
            calls: list[tuple[str, str]] = []

            async def first_indexer(path: str, *_args) -> None:
                calls.append(("first", path))
                if path == "/tmp/a.py":
                    first_started.set()
                    await release_first.wait()

            async def second_indexer(path: str, *_args) -> None:
                calls.append(("second", path))

            first_task = asyncio.create_task(
                debounce.drain_all(indexer=first_indexer, queue_file=queue_file)
            )
            await first_started.wait()

            # Only B is selected for the competing drain. Its batch-time lease
            # has expired while A is still running, so the second drainer owns
            # and settles it before the first drainer reaches it.
            clock[0] += debounce._CLAIM_LEASE_S
            second = await debounce.drain_all(
                indexer=second_indexer,
                paths=["/tmp/b.py"],
                queue_file=queue_file,
            )
            release_first.set()
            first = await first_task

            assert first.indexed == ["/tmp/a.py"]
            assert second.indexed == ["/tmp/b.py"]
            assert calls.count(("first", "/tmp/b.py")) == 0
            assert calls.count(("second", "/tmp/b.py")) == 1
            assert second.remaining == 1
            assert first.remaining == 0

        asyncio.run(scenario())

    def test_cancelled_drain_releases_owned_claims_immediately(self, queue_file: Path) -> None:
        debounce.enqueue("/tmp/file.py", now=100.0, queue_file=queue_file)

        async def scenario() -> None:
            started = asyncio.Event()
            parked = asyncio.Event()

            async def blocked_indexer(*_args) -> None:
                started.set()
                await parked.wait()

            task = asyncio.create_task(
                debounce.drain_all(indexer=blocked_indexer, queue_file=queue_file)
            )
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            released = _read_raw(queue_file)["entries"]["/tmp/file.py"]
            assert released["claim_id"] is None
            assert released["claimed_at"] is None

            async def succeeding_indexer(*_args) -> None:
                return None

            retried = await debounce.drain_all(indexer=succeeding_indexer, queue_file=queue_file)
            assert retried.indexed == ["/tmp/file.py"]
            assert retried.remaining == 0

        asyncio.run(scenario())

    @pytest.mark.parametrize(
        "payload",
        [
            "{broken",
            json.dumps({"version": debounce._QUEUE_VERSION + 1, "entries": {}}),
        ],
    )
    def test_corrupt_or_future_queue_fails_closed(self, queue_file: Path, payload: str) -> None:
        queue_file.write_text(payload, encoding="utf-8")
        with pytest.raises(debounce.DebounceQueueError):
            debounce.enqueue("/tmp/new.py", now=100.0, queue_file=queue_file)


class TestQueuePathOverride:
    def test_env_override_changes_queue_path(self, tmp_path: Path, monkeypatch) -> None:
        custom = tmp_path / "custom_queue.json"
        monkeypatch.setenv("MEMTOMEM_INDEX_DEBOUNCE_QUEUE", str(custom))
        assert debounce.queue_path() == custom

    def test_default_path_under_dot_memtomem(self, monkeypatch) -> None:
        monkeypatch.delenv("MEMTOMEM_INDEX_DEBOUNCE_QUEUE", raising=False)
        path = debounce.queue_path()
        assert path.name == "index_debounce_queue.json"
        assert ".memtomem" in str(path)


class TestCliQueueErrorBoundary:
    """A broken queue is a CLI error with a recovery hint, never a traceback.

    ``mm index --debounce-window`` is the PostToolUse hook. A transient
    ``EACCES`` on the queue file, or a clock step that backdates a live claim,
    raises ``DebounceQueueError`` from deep inside ``_load``/``_claimable``; an
    uncaught one would traceback on *every* subsequent invocation and stall
    reactive indexing until a human found and deleted the file.
    """

    def test_status_queue_error_surfaces_as_a_cli_error(self, tmp_path: Path, monkeypatch) -> None:
        """End-to-end through the read-only path, which needs no components."""
        from click.testing import CliRunner

        from memtomem.cli import cli

        qp = tmp_path / "queue.json"
        qp.write_text("{broken", encoding="utf-8")
        monkeypatch.setenv("MEMTOMEM_INDEX_DEBOUNCE_QUEUE", str(qp))

        result = CliRunner().invoke(cli, ["index", "--status"])

        assert result.exit_code != 0
        assert not isinstance(result.exception, debounce.DebounceQueueError)
        assert "debounce queue" in result.output
        assert str(qp) in result.output

    @pytest.mark.parametrize(
        ("args", "target"),
        [
            (["index", "--status"], "_print_status"),
            (["index", "--flush"], "_run_flush"),
            (["index", "--debounce-window", "1", "."], "_run_debounce"),
        ],
    )
    def test_every_queue_entry_point_is_inside_the_guard(self, args, target, monkeypatch) -> None:
        """The drain paths boot components, so the guard is pinned at the
        boundary rather than through a full runtime."""
        from click.testing import CliRunner

        from memtomem.cli import cli, indexing as indexing_mod

        def boom(**_kwargs):
            raise debounce.DebounceQueueError("debounce queue /q.json is unreadable: EACCES")

        monkeypatch.setattr(indexing_mod, target, boom)

        result = CliRunner().invoke(cli, args)

        assert result.exit_code != 0
        assert not isinstance(result.exception, debounce.DebounceQueueError)
        assert "debounce queue" in result.output
        assert "remove" in result.output
