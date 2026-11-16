"""``list_visible_chunks_by_id``: the pool of a bounded duplicate scan.

A scan bounded by ``max_scan`` used to fill its pool source by source in the
iteration order of a ``set``, so each process scanned a different sample. The
pool is now the first rows in ascending id order, read with the number of rows
it was taken from, in one statement.

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
    chunks, visible = await backend.list_visible_chunks_by_id(limit)
    return [c.id.int for c in chunks], visible


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

        chunks, visible = await backend.list_visible_chunks_by_id(100)

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
        chunks, _ = await DedupScanner(backend)._select_pool(int(sys.argv[2]))
        print(" ".join(str(c.id.int) for c in chunks))
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
