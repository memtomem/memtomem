"""``list_visible_chunks_by_id``: the pool of a bounded duplicate scan.

A scan bounded by ``max_scan`` used to fill its pool source by source in the
iteration order of a ``set``, so each process scanned a different sample. The
pool is now the first rows in ascending id order, read with the number of rows
it was taken from, in one statement. A cursor moves the pool on: the rows with an
id above it, and how many of those there are.

Rows are seeded with SQL so that each case controls the ids, the insertion
order, the timestamps and the holds it names.
"""

from __future__ import annotations

import os
import subprocess
import sys
from contextlib import asynccontextmanager
from uuid import UUID

import pytest

from memtomem.config import StorageConfig
from memtomem.models import CONSOLIDATED_SUFFIX, ORIGIN_CONSOLIDATION_POLICY
from memtomem.search.dedup import DedupScanner
from memtomem.storage.sqlite_backend import SqliteBackend

pytestmark = pytest.mark.asyncio

_STAMP = "2026-01-01T00:00:00"


def _open(path) -> SqliteBackend:
    return SqliteBackend(
        config=StorageConfig(sqlite_path=path),
        dimension=0,
        embedding_provider="none",
        embedding_model="",
    )


@pytest.fixture
async def backend(tmp_path):
    be = _open(tmp_path / "pool.db")
    await be.initialize()
    yield be
    await be.close()


def _seed(
    backend: SqliteBackend,
    n: int,
    source: str,
    *,
    created_at: str = _STAMP,
    origin: str | None = None,
) -> UUID:
    chunk_id = UUID(int=n)
    db = backend._get_db()
    db.execute(
        "INSERT INTO chunks (id, content, content_hash, source_file, start_line, end_line, "
        "origin, created_at, updated_at) VALUES (?, ?, ?, ?, 1, 2, ?, ?, ?)",
        (str(chunk_id), f"body {n}", f"hash-{n}", source, origin, created_at, created_at),
    )
    db.commit()
    return chunk_id


def _hide(backend: SqliteBackend, source: str, kind: str) -> None:
    db = backend._get_db()
    if kind == "pending":
        db.execute("INSERT INTO pending_source_checks VALUES (?, ?)", (source, _STAMP))
    else:
        db.execute("INSERT INTO held_sources VALUES (?, ?, ?, ?)", (source, _STAMP, _STAMP, kind))
    db.commit()


def _release(backend: SqliteBackend, source: str) -> None:
    db = backend._get_db()
    db.execute("DELETE FROM held_sources WHERE source_file=?", (source,))
    db.commit()


async def _pool(backend: SqliteBackend, limit: int) -> tuple[list[int], int]:
    pool = await backend.list_visible_chunks_by_id(limit)
    return [c.id.int for c in pool.chunks], pool.visible


async def _pool_after(
    backend: SqliteBackend, limit: int, after: int | None
) -> tuple[list[int], int, int]:
    """Ids of the pool, the visible count and the count above the cursor."""
    cursor = None if after is None else str(UUID(int=after))
    pool = await backend.list_visible_chunks_by_id(limit, cursor)
    return [c.id.int for c in pool.chunks], pool.visible, pool.above_cursor


def _five_in_four_orders(backend: SqliteBackend) -> None:
    """Five chunks whose id, path, insertion and timestamp orders all differ.

    ============  =====================
    by id         10, 20, 30, 40, 50
    by path       50, 30, 40, 20, 10
    as inserted   30, 10, 50, 20, 40
    oldest first  30, 10, 40, 50, 20
    newest first  20, 50, 40, 10, 30
    ============  =====================
    """
    for n, source, minute in (
        (30, "/m/b.md", 1),
        (10, "/m/e.md", 2),
        (50, "/m/a.md", 4),
        (20, "/m/d.md", 5),
        (40, "/m/c.md", 3),
    ):
        _seed(backend, n, source, created_at=f"2026-01-01T00:0{minute}:00")


class TestOrder:
    async def test_rows_come_back_in_ascending_id_order(self, backend):
        _five_in_four_orders(backend)

        assert await _pool(backend, 5) == ([10, 20, 30, 40, 50], 5)

    async def test_a_bounded_read_keeps_the_lowest_ids(self, backend):
        _five_in_four_orders(backend)

        assert await _pool(backend, 2) == ([10, 20], 5)

    @pytest.mark.parametrize(
        ("limit", "rows"),
        [(1, 1), (4, 4), (5, 5), (6, 5), (9, 5)],
        ids=["one", "one-short", "exactly-all", "one-over", "well-over"],
    )
    async def test_the_limit_bounds_the_rows_and_not_the_count(self, backend, limit, rows):
        _five_in_four_orders(backend)

        ids, visible = await _pool(backend, limit)

        assert (len(ids), visible) == (rows, 5)

    async def test_the_pool_moves_only_when_the_visible_set_does(self, backend):
        for n in (20, 30, 40):
            _seed(backend, n, f"/m/{n}.md")
        _seed(backend, 5, "/m/held.md")
        _hide(backend, "/m/held.md", "scan_missing")
        assert await _pool(backend, 2) == ([20, 30], 3)
        assert await _pool(backend, 2) == ([20, 30], 3)

        _seed(backend, 10, "/m/new.md")
        assert await _pool(backend, 2) == ([10, 20], 4)

        _release(backend, "/m/held.md")
        assert await _pool(backend, 2) == ([5, 10], 5)


class TestVisibility:
    async def test_rows_and_count_follow_the_path_level_hold_rule(self, backend):
        summary = CONSOLIDATED_SUFFIX
        policy = ORIGIN_CONSOLIDATION_POLICY
        seeded = {
            "plain": _seed(backend, 1, "/m/plain.md"),
            "held": _seed(backend, 2, "/m/held.md"),
            "pending": _seed(backend, 3, "/m/pending.md"),
            "held parent": _seed(backend, 4, "/m/parent_held.md"),
            "summary of a held parent": _seed(
                backend, 5, f"/m/parent_held.md{summary}", origin=policy
            ),
            "pending parent": _seed(backend, 6, "/m/parent_pending.md"),
            "summary of a pending parent": _seed(
                backend, 7, f"/m/parent_pending.md{summary}", origin=policy
            ),
            # The row that tells the path-level rule from the row-level one: an
            # ordinary chunk filed on the path of a hidden summary.
            "ordinary chunk on a hidden summary's path": _seed(
                backend, 8, f"/m/parent_held.md{summary}"
            ),
            "second held parent": _seed(backend, 9, "/m/parent_held2.md"),
            # The suffix alone hides nothing: no policy row sits on this path.
            "ordinary chunk on a suffix-only path": _seed(
                backend, 10, f"/m/parent_held2.md{summary}"
            ),
            "open parent": _seed(backend, 11, "/m/parent_open.md"),
            "summary of an open parent": _seed(
                backend, 12, f"/m/parent_open.md{summary}", origin=policy
            ),
        }
        for source, kind in (
            ("/m/held.md", "scan_missing"),
            ("/m/pending.md", "pending"),
            ("/m/parent_held.md", "scan_missing"),
            ("/m/parent_pending.md", "pending"),
            ("/m/parent_held2.md", "scan_missing"),
        ):
            _hide(backend, source, kind)
        label = {chunk_id: name for name, chunk_id in seeded.items()}

        pool = await backend.list_visible_chunks_by_id(100)
        chunks, visible = pool.chunks, pool.visible

        expected = [
            "plain",
            "ordinary chunk on a suffix-only path",
            "open parent",
            "summary of an open parent",
        ]
        assert [label[c.id] for c in chunks] == expected
        assert visible == len(expected)

    async def test_the_count_is_taken_under_the_same_rule_when_the_rows_are_cut(self, backend):
        _seed(backend, 1, "/m/plain.md")
        _seed(backend, 2, "/m/held.md")
        _seed(backend, 3, "/m/parent.md")
        _seed(backend, 4, f"/m/parent.md{CONSOLIDATED_SUFFIX}", origin=ORIGIN_CONSOLIDATION_POLICY)
        _seed(backend, 5, f"/m/parent.md{CONSOLIDATED_SUFFIX}")
        _seed(backend, 6, "/m/also_plain.md")
        _hide(backend, "/m/held.md", "scan_missing")
        _hide(backend, "/m/parent.md", "scan_missing")

        assert await _pool(backend, 1) == ([1], 2)

    async def test_an_empty_store(self, backend):
        assert await _pool(backend, 10) == ([], 0)

    async def test_a_store_whose_every_source_is_hidden(self, backend):
        _seed(backend, 1, "/m/held.md")
        _seed(backend, 2, "/m/pending.md")
        _hide(backend, "/m/held.md", "scan_missing")
        _hide(backend, "/m/pending.md", "pending")

        assert await _pool(backend, 10) == ([], 0)


class TestOneSnapshot:
    async def test_rows_and_count_come_from_one_statement(self, backend, monkeypatch):
        _five_in_four_orders(backend)
        real = backend._lease_txn_read_db
        statements: list[str] = []

        @asynccontextmanager
        async def traced():
            # Installed after the lease is handed out: acquiring one runs the
            # schema check's own statements, which are not the read's.
            async with real() as db:
                db.set_trace_callback(statements.append)
                try:
                    yield db
                finally:
                    db.set_trace_callback(None)

        monkeypatch.setattr(backend, "_lease_txn_read_db", traced)

        assert await _pool(backend, 2) == ([10, 20], 5)
        assert len(statements) == 1, statements

    @pytest.mark.parametrize(
        ("after", "expected"),
        [(20, ([30, 40], 5, 3)), (50, ([], 5, 0))],
        ids=["mid-store", "empty-pool"],
    )
    async def test_a_cursor_read_is_one_statement_too(self, backend, monkeypatch, after, expected):
        _five_in_four_orders(backend)
        real = backend._lease_txn_read_db
        statements: list[str] = []

        @asynccontextmanager
        async def traced():
            async with real() as db:
                db.set_trace_callback(statements.append)
                try:
                    yield db
                finally:
                    db.set_trace_callback(None)

        monkeypatch.setattr(backend, "_lease_txn_read_db", traced)

        assert await _pool_after(backend, 2, after) == expected
        assert len(statements) == 1, statements


class TestCursor:
    async def test_no_cursor_counts_every_visible_chunk_as_above_it(self, backend):
        _five_in_four_orders(backend)

        assert await _pool_after(backend, 2, None) == ([10, 20], 5, 5)

    async def test_the_cursor_is_exclusive(self, backend):
        _five_in_four_orders(backend)

        assert await _pool_after(backend, 2, 20) == ([30, 40], 5, 3)

    async def test_the_cursor_need_not_be_a_stored_id(self, backend):
        _five_in_four_orders(backend)

        assert await _pool_after(backend, 2, 25) == ([30, 40], 5, 3)
        assert await _pool_after(backend, 2, 1) == ([10, 20], 5, 5)

    async def test_a_walk_continues_after_a_deleted_cursor_row(self, backend):
        _five_in_four_orders(backend)
        assert await _pool_after(backend, 2, None) == ([10, 20], 5, 5)
        db = backend._get_db()
        db.execute("DELETE FROM chunks WHERE id=?", (str(UUID(int=20)),))
        db.commit()

        assert await _pool_after(backend, 2, 20) == ([30, 40], 4, 3)

    @pytest.mark.parametrize("after", [50, 51, 99], ids=["last-id", "just-past", "well-past"])
    async def test_an_empty_pool_still_reports_the_visible_count(self, backend, after):
        _five_in_four_orders(backend)

        assert await _pool_after(backend, 2, after) == ([], 5, 0)

    async def test_hidden_chunks_are_counted_on_neither_side_of_the_cursor(self, backend):
        _five_in_four_orders(backend)
        _seed(backend, 5, "/m/held-below.md")
        _seed(backend, 35, "/m/held-above.md")
        _seed(backend, 45, "/m/pending-above.md")
        _hide(backend, "/m/held-below.md", "scan_missing")
        _hide(backend, "/m/held-above.md", "scan_missing")
        _hide(backend, "/m/pending-above.md", "pending")

        assert await _pool_after(backend, 2, 20) == ([30, 40], 5, 3)
        assert await _pool_after(backend, 2, 40) == ([50], 5, 1)

    @pytest.mark.parametrize("step", [1, 2, 3, 5, 7])
    async def test_a_walk_visits_every_visible_chunk_once_and_in_order(self, backend, step):
        _five_in_four_orders(backend)
        _seed(backend, 25, "/m/held.md")
        _hide(backend, "/m/held.md", "scan_missing")
        walked: list[int] = []
        after: int | None = None
        reads = 0

        while True:
            ids, visible, above = await _pool_after(backend, step, after)
            reads += 1
            assert visible == 5
            # What the read says is above the cursor is what is left to walk.
            assert above == 5 - len(walked)
            walked.extend(ids)
            if above == len(ids):
                break
            after = ids[-1]

        assert walked == [10, 20, 30, 40, 50]
        # The end is seen on the read that reaches it: no empty read after it.
        assert reads == -(-5 // step)


_PRINT_POOL = """
import asyncio, sys
from memtomem.config import StorageConfig
from memtomem.search.dedup import DedupScanner
from memtomem.storage.sqlite_backend import SqliteBackend


async def main():
    backend = SqliteBackend(
        config=StorageConfig(sqlite_path=sys.argv[1]),
        dimension=0,
        embedding_provider="none",
        embedding_model="",
    )
    await backend.initialize()
    try:
        pool = await DedupScanner(backend)._select_pool(int(sys.argv[2]))
        print(" ".join(str(c.id.int) for c in pool.chunks))
    finally:
        await backend.close()


asyncio.run(main())
"""


class TestAcrossProcesses:
    async def test_two_processes_with_different_hash_seeds_select_the_same_pool(self, tmp_path):
        path = tmp_path / "shared.db"
        seeded = _open(path)
        await seeded.initialize()
        try:
            # One chunk per source: a pool filled in the hash order of the
            # source set would differ between the two seeds.
            for n in range(1, 25):
                _seed(seeded, n * 7 % 25, f"/m/source-{n:02d}.md")
            assert await _pool(seeded, 6) == ([1, 2, 3, 4, 5, 6], 24)
        finally:
            await seeded.close()

        def pool_in_a_process(seed: str) -> str:
            done = subprocess.run(
                [sys.executable, "-c", _PRINT_POOL, str(path), "6"],
                env={**os.environ, "PYTHONHASHSEED": seed},
                capture_output=True,
                text=True,
                check=True,
                timeout=120,
            )
            return done.stdout.strip()

        first, second = pool_in_a_process("1"), pool_in_a_process("2")

        assert first == "1 2 3 4 5 6"
        assert second == first


class TestScannerUsesIt:
    async def test_the_scanner_reports_the_pool_and_what_it_was_taken_from(self, backend):
        _five_in_four_orders(backend)
        _seed(backend, 5, "/m/held.md")
        _hide(backend, "/m/held.md", "scan_missing")

        candidates, coverage = await DedupScanner(backend).scan_with_coverage(max_scan=2)

        assert candidates == []
        assert (coverage.pool, coverage.eligible) == (2, 5)

    async def test_the_scanner_walks_the_store_with_the_cursor_it_reports(self, backend):
        _five_in_four_orders(backend)
        _seed(backend, 5, "/m/held.md")
        _hide(backend, "/m/held.md", "scan_missing")
        scanner = DedupScanner(backend)
        steps: list[tuple[int, int, int | None]] = []
        after = None

        while True:
            _, coverage = await scanner.scan_with_coverage(max_scan=2, after=after)
            assert coverage.after == after
            assert coverage.eligible == 5
            cursor = coverage.next_after
            steps.append((coverage.pool, coverage.remaining, cursor.int if cursor else None))
            if cursor is None:
                break
            after = cursor

        assert steps == [(2, 3, 20), (2, 1, 40), (1, 0, None)]

    async def test_a_pool_that_ends_on_the_last_chunk_reports_the_end(self, backend):
        for n in (10, 20, 30, 40):
            _seed(backend, n, f"/m/{n}.md")
        scanner = DedupScanner(backend)

        _, first = await scanner.scan_with_coverage(max_scan=2)
        _, last = await scanner.scan_with_coverage(max_scan=2, after=first.next_after)

        assert (first.pool, first.remaining, first.next_after) == (2, 2, UUID(int=20))
        assert (last.pool, last.remaining, last.next_after) == (2, 0, None)

    async def test_a_cursor_past_the_end_scans_nothing_and_reports_the_end(self, backend):
        _five_in_four_orders(backend)

        candidates, coverage = await DedupScanner(backend).scan_with_coverage(
            max_scan=2, after=UUID(int=50)
        )

        assert candidates == []
        assert (coverage.pool, coverage.eligible) == (0, 5)
        assert (coverage.remaining, coverage.next_after) == (0, None)
