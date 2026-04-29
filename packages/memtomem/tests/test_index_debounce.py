"""Tests for ``memtomem.indexing.debounce``.

The debounce module is the file-system substrate behind ``mm index
--debounce-window`` (PR #536 documented gap close). The tests pin three
contracts:

1. **Enqueue semantics** — last-write-wins for namespace/force, ``last_seen``
   pushes forward on every call, ``first_seen`` is set once.
2. **Drain semantics** — ``drain_ready`` indexes only entries that have been
   silent at least ``window_seconds``; ``drain_all`` indexes everything;
   indexer errors leave the entry in the queue for retry.
3. **Concurrency + persistence** — the queue persists across calls, the
   ``flock`` serializes parallel mutations, and ``status_snapshot`` reads
   without a lock (race-prone by design).
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest

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

    def test_indexer_error_keeps_entry_for_retry(self, queue_file: Path) -> None:
        """If indexing raises, the entry stays queued so the next hook call
        retries. Not silently lost."""
        debounce.enqueue("/tmp/broken.py", now=100.0, queue_file=queue_file)

        async def indexer(p: str, ns: str | None, force: bool) -> None:
            raise RuntimeError("synthetic indexing failure")

        result = asyncio.run(
            debounce.drain_ready(
                window_seconds=5.0, indexer=indexer, now=110.0, queue_file=queue_file
            )
        )
        assert result.indexed == []
        assert len(result.errors) == 1
        assert result.errors[0][0] == "/tmp/broken.py"
        assert result.remaining == 1

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
        """Two threads enqueue distinct paths in parallel. The flock guarantees
        both writes land. Without the lock, the second writer's load+save
        would clobber the first writer's entry."""
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
        """Two entries, one indexer-errors. The successful one is gone from
        disk; the failing one remains so the next hook call retries."""
        debounce.enqueue("/tmp/good.py", now=100.0, queue_file=queue_file)
        debounce.enqueue("/tmp/bad.py", now=100.0, queue_file=queue_file)

        async def indexer(p: str, ns: str | None, force: bool) -> None:
            if p == "/tmp/bad.py":
                raise RuntimeError("boom")

        asyncio.run(
            debounce.drain_ready(
                window_seconds=5.0, indexer=indexer, now=110.0, queue_file=queue_file
            )
        )
        remaining = _read_raw(queue_file)["entries"]
        assert list(remaining.keys()) == ["/tmp/bad.py"]


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
