"""Concurrency hardening tests for SqliteBackend's reader pool.

Covers:
1. ``transaction(wait=...)`` (#2860): competing tasks that opt in queue in
   FIFO order. A waiter that times out or is cancelled, before or after it was
   granted the slot, does not stall the ones behind it. Newcomers never
   overtake the queue, including in the moment after the owner exits and
   before the next waiter resumes. ``close()`` refuses queued waiters. The
   default ``transaction()`` never queues. Every production call site is
   classified as opted in or not.
2. Same-task nested ``transaction()`` fails immediately with ``StorageError``
   (never self-deadlocks in the writer queue).
3. Unowned ``db.in_transaction`` on the writer connection fails immediately
   with ``StorageError``.
4. Concurrent ``bm25_search`` and ``dense_search`` calls lease distinct pool
   connections, queue when concurrency exceeds pool size, and run in worker
   threads without sharing a single ``sqlite3.Connection`` concurrently.
5. A sync reader never receives a leased pool connection.
6. Cancelling a caller whose read runs in a worker thread keeps the lease until
   the worker returns, so neither another lease nor ``close()`` gets the
   connection while the worker still uses it -- including when every Task is
   cancelled at once, as ``asyncio.run`` does on shutdown.
7. ``close()`` waits for in-flight leased readers to drain before closing pool
   connections, still closes them when it is cancelled mid-drain, and rejects
   new read/write requests during shutdown with
   ``StorageError("Database is closing")``.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import sqlite3
import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest

import memtomem
from memtomem.config import StorageConfig
from memtomem.errors import StorageError, TransactionOwnedError
from memtomem.models import Chunk, ChunkMetadata, ChunkType
from memtomem.storage.base import WRITER_WAIT_S
from memtomem.storage.sqlite_backend import SqliteBackend


def _make_chunk(
    content: str,
    *,
    source: Path,
    line: int = 1,
    embedding: list[float] | None = None,
) -> Chunk:
    return Chunk(
        id=uuid4(),
        content=content,
        embedding=embedding or [0.1, 0.2, 0.3, 0.4],
        metadata=ChunkMetadata(
            source_file=source,
            heading_hierarchy=("Test",),
            chunk_type=ChunkType.RAW_TEXT,
            start_line=line,
            end_line=line + 2,
            namespace="default",
        ),
    )


@pytest.fixture
async def storage(tmp_path: Path):
    cfg = StorageConfig(sqlite_path=str(tmp_path / "phase2_concurrency.db"))
    backend = SqliteBackend(cfg, dimension=4)
    await backend.initialize()
    try:
        yield backend
    finally:
        await backend.close()


async def _until(predicate, timeout: float = 2.0) -> None:
    """Yield to the loop until ``predicate()`` holds; queue order is never left to a sleep."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0)


def _live_waiters(storage: SqliteBackend) -> list[asyncio.Future]:
    """The writer lock's queued futures that are not cancelled (test-only peek at asyncio)."""
    return [w for w in (storage._writer_lock._waiters or ()) if not w.cancelled()]


async def _queue_writer(storage: SqliteBackend, name: str, order: list[str], tmp_path: Path):
    """Queue behind the current owner, record entry, and commit one chunk."""
    async with storage.transaction(wait=5.0):
        order.append(name)
        chunk = _make_chunk(f"{name} queued write", source=tmp_path / f"{name}.md")
        await storage.upsert_chunks([chunk])


async def _enter(storage: SqliteBackend, wait: float) -> None:
    async with storage.transaction(wait=wait):
        pass


def _assert_writer_idle(storage: SqliteBackend) -> None:
    assert storage._transaction_owner is None
    assert not storage._writer_lock.locked()
    assert not _live_waiters(storage)


class _Holder:
    """A task that owns the writer until ``release`` is set, then runs ``after``.

    ``before_exit`` runs synchronously as the last step inside the transaction,
    so nothing else gets the loop between it and the exit.
    """

    def __init__(self, storage: SqliteBackend, after=None, before_exit=None) -> None:
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self._storage, self._after, self._before_exit = storage, after, before_exit
        self.task: asyncio.Task[None] | None = None

    async def _run(self) -> None:
        async with self._storage.transaction():
            self.entered.set()
            await self.release.wait()
            if self._before_exit is not None:
                self._before_exit()
        # No await since the exit: the lock has been handed to the head waiter,
        # which has not resumed yet.
        if self._after is not None:
            await self._after()

    async def start(self) -> _Holder:
        self.task = asyncio.create_task(self._run())
        entered = asyncio.ensure_future(self.entered.wait())
        await asyncio.wait({entered, self.task}, timeout=5, return_when=asyncio.FIRST_COMPLETED)
        if not self.entered.is_set():
            entered.cancel()
            if self.task.done():
                self.task.result()  # the holder's own failure
            raise AssertionError("holder never entered its transaction")
        return self


async def _queue(storage: SqliteBackend, coro, count: int) -> asyncio.Task:
    """Start ``coro`` and wait until the writer lock has ``count`` live waiters."""
    task = asyncio.create_task(coro)
    await _until(lambda: len(_live_waiters(storage)) == count)
    return task


async def test_queued_writers_enter_in_fifo_order(storage: SqliteBackend, tmp_path: Path):
    order: list[str] = []
    holder = await _Holder(storage).start()
    waiters = [
        await _queue(storage, _queue_writer(storage, name, order, tmp_path), i)
        for i, name in enumerate(["w1", "w2", "w3"], start=1)
    ]
    holder.release.set()
    await asyncio.wait_for(asyncio.gather(holder.task, *waiters), 5)
    assert order == ["w1", "w2", "w3"]
    _assert_writer_idle(storage)
    assert (await storage.get_stats())["total_chunks"] == 3


async def test_timed_out_waiter_does_not_stall_the_one_behind_it(
    storage: SqliteBackend, tmp_path: Path
):
    order: list[str] = []
    holder = await _Holder(storage).start()
    impatient = await _queue(storage, _enter(storage, 0.05), 1)
    follower = await _queue(storage, _queue_writer(storage, "follower", order, tmp_path), 2)

    with pytest.raises(TransactionOwnedError, match="timed out waiting for writer lock"):
        await impatient
    assert len(_live_waiters(storage)) == 1

    holder.release.set()
    await asyncio.wait_for(asyncio.gather(holder.task, follower), 5)
    assert order == ["follower"]
    _assert_writer_idle(storage)
    assert (await storage.get_stats())["total_chunks"] == 1


async def test_cancelled_queued_waiter_leaves_the_queue(storage: SqliteBackend, tmp_path: Path):
    order: list[str] = []
    holder = await _Holder(storage).start()
    first = await _queue(storage, _queue_writer(storage, "first", order, tmp_path), 1)
    second = await _queue(storage, _queue_writer(storage, "second", order, tmp_path), 2)

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert len(_live_waiters(storage)) == 1

    holder.release.set()
    await asyncio.wait_for(asyncio.gather(holder.task, second), 5)
    assert order == ["second"]
    _assert_writer_idle(storage)


async def test_waiter_cancelled_after_its_grant_hands_the_lock_on(
    storage: SqliteBackend, tmp_path: Path
):
    """The owner's exit grants ``first``; ``first`` is cancelled before it resumes."""
    order: list[str] = []
    tasks: dict[str, asyncio.Task] = {}
    granted_before_cancel: list[bool] = []

    async def after() -> None:
        granted_before_cancel.append(_live_waiters(storage)[0].done())
        tasks["first"].cancel()

    holder = await _Holder(storage, after).start()
    tasks["first"] = await _queue(storage, _queue_writer(storage, "first", order, tmp_path), 1)
    second = await _queue(storage, _queue_writer(storage, "second", order, tmp_path), 2)

    holder.release.set()
    await holder.task
    with pytest.raises(asyncio.CancelledError):
        await tasks["first"]
    await asyncio.wait_for(second, 5)
    assert granted_before_cancel == [True]
    assert order == ["second"]
    _assert_writer_idle(storage)


async def test_default_transaction_is_refused_without_an_await_while_held(
    storage: SqliteBackend,
):
    """While a task owns the writer, ``wait <= 0`` (and NaN) refuse synchronously, as on main."""
    holder = await _Holder(storage).start()
    for wait in (0.0, -1.0, float("nan")):
        enter = storage.transaction(wait=wait).__aenter__()
        # The first step must raise: a suspension here would be a queued wait.
        with pytest.raises(TransactionOwnedError, match="retry after it completes"):
            enter.send(None)
        enter.close()
        assert not _live_waiters(storage)
    holder.release.set()
    await holder.task
    _assert_writer_idle(storage)


async def test_default_newcomer_in_the_handoff_window_is_refused(
    storage: SqliteBackend, tmp_path: Path
):
    """Lock handed to ``queued`` but not yet resumed: a ``wait=0`` entry must not take it."""
    order: list[str] = []
    refusal: list[BaseException] = []

    async def after() -> None:
        assert storage._transaction_owner is None
        assert not storage._writer_lock.locked() and _live_waiters(storage)[0].done()
        try:
            async with storage.transaction():
                order.append("newcomer-default")
        except TransactionOwnedError as exc:
            refusal.append(exc)

    holder = await _Holder(storage, after).start()
    queued = await _queue(storage, _queue_writer(storage, "queued", order, tmp_path), 1)
    holder.release.set()
    await asyncio.wait_for(asyncio.gather(holder.task, queued), 5)
    # Order only: the refusal yields once, so ``queued`` may finish meanwhile.
    assert len(refusal) == 1
    assert order == ["queued"]
    _assert_writer_idle(storage)


async def test_waiting_newcomer_in_the_handoff_window_enters_after_the_head(
    storage: SqliteBackend, tmp_path: Path
):
    """A ``wait > 0`` entry arriving while the grant is pending queues behind it."""
    order: list[str] = []

    async def after() -> None:
        assert storage._transaction_owner is None
        assert not storage._writer_lock.locked() and _live_waiters(storage)[0].done()
        async with storage.transaction(wait=5.0):
            order.append("newcomer-waiting")

    holder = await _Holder(storage, after).start()
    queued = await _queue(storage, _queue_writer(storage, "queued", order, tmp_path), 1)
    holder.release.set()
    await asyncio.wait_for(asyncio.gather(holder.task, queued), 5)
    assert order == ["queued", "newcomer-waiting"]
    _assert_writer_idle(storage)


@pytest.mark.parametrize("wait", [0.0, 0.5])
async def test_cancelled_sole_waiter_does_not_block_a_newcomer(storage: SqliteBackend, wait: float):
    """The sole waiter is cancelled, the owner exits before its cleanup runs.

    The cancel is issued inside the owner's transaction, as its last step, and
    the newcomer enters right after the exit with no await in between. So the
    cancelled waiter's cleanup has not run, and its future is still queued, when
    the newcomer arrives. With the writer idle, the newcomer must enter at once.
    The hand-rolled queue let that stale future refuse a ``wait=0`` newcomer
    and made a ``wait > 0`` one wait out its whole budget.
    """
    entered: list[float] = []
    stale_queued_at_entry: list[bool] = []
    tasks: dict[str, asyncio.Task] = {}

    def cancel_sole() -> None:
        tasks["sole"].cancel()

    async def after() -> None:
        waiters = list(storage._writer_lock._waiters or ())
        assert waiters and all(w.cancelled() for w in waiters), "the race needs a stale waiter"
        async with storage.transaction(wait=wait):
            # Still queued means the newcomer got in without yielding the loop.
            stale_queued_at_entry.append(
                any(w.cancelled() for w in storage._writer_lock._waiters or ())
            )
            entered.append(wait)

    holder = _Holder(storage, after, before_exit=cancel_sole)
    await holder.start()
    tasks["sole"] = await _queue(storage, _enter(storage, 5.0), 1)
    holder.release.set()
    await asyncio.wait_for(holder.task, 5)
    assert entered == [wait]
    assert stale_queued_at_entry == [True]
    with pytest.raises(asyncio.CancelledError):
        await tasks["sole"]
    _assert_writer_idle(storage)


async def test_unowned_open_transaction_refuses_before_queueing(
    storage: SqliteBackend, tmp_path: Path
):
    """With the owner gone and a grant pending, an unowned transaction is refused by both budgets."""
    order: list[str] = []
    refusals: list[tuple[float, BaseException]] = []

    async def after() -> None:
        assert storage._db is not None
        storage._db.execute("BEGIN IMMEDIATE")
        try:
            for wait in (0.0, 0.5):
                try:
                    async with storage.transaction(wait=wait):
                        order.append(f"entered-{wait}")
                except StorageError as exc:
                    refusals.append((wait, exc))
                assert len(_live_waiters(storage)) == 1
        finally:
            storage._db.rollback()

    holder = await _Holder(storage, after).start()
    queued = await _queue(storage, _queue_writer(storage, "queued", order, tmp_path), 1)
    holder.release.set()
    await asyncio.wait_for(asyncio.gather(holder.task, queued), 5)
    assert [w for w, _ in refusals] == [0.0, 0.5]
    for _, exc in refusals:
        assert type(exc) is StorageError
        assert "already has an open transaction" in str(exc)
    assert order == ["queued"]
    _assert_writer_idle(storage)


async def test_a_waiter_refused_after_acquiring_hands_the_lock_on(
    storage: SqliteBackend, tmp_path: Path
):
    """The head acquires but is refused before ``BEGIN``; the next waiter still enters."""
    order: list[str] = []
    tasks: dict[str, asyncio.Task] = {}

    async def after() -> None:
        assert storage._db is not None
        storage._db.execute("BEGIN IMMEDIATE")  # opened between grant and resume
        await _until(lambda: tasks["head"].done())
        storage._db.rollback()

    holder = await _Holder(storage, after).start()
    tasks["head"] = await _queue(storage, _queue_writer(storage, "head", order, tmp_path), 1)
    tasks["next"] = await _queue(storage, _queue_writer(storage, "next", order, tmp_path), 2)
    holder.release.set()
    await asyncio.wait_for(holder.task, 5)
    with pytest.raises(StorageError, match="already has an open transaction"):
        await tasks["head"]
    await asyncio.wait_for(tasks["next"], 5)
    assert order == ["next"]
    _assert_writer_idle(storage)


async def test_delete_by_namespace_is_refused_while_a_grant_is_pending(
    storage: SqliteBackend, tmp_path: Path
):
    """The documented exception: a default caller that main would have let in.

    ``delete_by_namespace``'s own ``_get_db()`` sees no owner, so it reaches
    ``transaction()``, which refuses it rather than letting it ahead of the
    waiter the owner's exit has just granted.
    """
    order: list[str] = []
    refusal: list[BaseException] = []

    async def after() -> None:
        try:
            await storage.delete_by_namespace("default")
        except TransactionOwnedError as exc:
            refusal.append(exc)

    holder = await _Holder(storage, after).start()
    queued = await _queue(storage, _queue_writer(storage, "queued", order, tmp_path), 1)
    holder.release.set()
    await asyncio.wait_for(asyncio.gather(holder.task, queued), 5)
    assert len(refusal) == 1
    assert order == ["queued"]
    assert (await storage.get_stats())["total_chunks"] == 1


async def test_a_wait_that_runs_out_on_a_closing_store_reports_closing(storage: SqliteBackend):
    """Unit test of the timeout translation: closing wins over the ownership error.

    ``_closing`` is set directly while an owner holds the lock, so the waiter's
    timeout can take no other path. This is not a claim that ``close()`` can run
    while a task owns the writer.
    """
    holder = await _Holder(storage).start()
    waiter = await _queue(storage, _enter(storage, 0.05), 1)
    storage._closing = True
    try:
        with pytest.raises(StorageError, match="Database is closing") as info:
            await asyncio.wait_for(waiter, 5)
        assert not isinstance(info.value, TransactionOwnedError)
    finally:
        storage._closing = False
    holder.release.set()
    await holder.task
    _assert_writer_idle(storage)


async def test_close_refuses_each_writer_queued_behind_a_released_owner(
    storage: SqliteBackend, tmp_path: Path
):
    """Each queued writer acquires in turn and is refused by the closing store."""
    order: list[str] = []

    async def after() -> None:
        await storage.close()  # before any waiter resumes; nothing to drain

    holder = await _Holder(storage, after).start()
    queued = [
        await _queue(storage, _queue_writer(storage, name, order, tmp_path), i)
        for i, name in enumerate(["first", "second", "third"], start=1)
    ]
    holder.release.set()
    await asyncio.wait_for(holder.task, 5)
    for task in queued:
        with pytest.raises(StorageError, match="Database is closing"):
            await asyncio.wait_for(task, 5)
    assert order == []
    _assert_writer_idle(storage)


#: Production ``transaction()`` call sites and whether each opts into waiting
#: (#2860). A new site must be added here, deciding which it is.
_TRANSACTION_SITES = {
    "indexing/engine.py": [True],
    "search/dedup.py": [True],
    "server/tools/consolidation.py": [True],
    "storage/sqlite_backend.py": [False],
    "tools/consolidation_engine.py": [True],
    "tools/entity_backfill.py": [False],
    "tools/export_import.py": [True],
}


def _transaction_sites(source: str, where: str) -> list[bool]:
    """Whether each ``.transaction(...)`` call in ``source`` waits, in line order.

    A form this cannot classify fails instead of being skipped: ``.transaction``
    used other than as a direct call (an alias, a reference passed on),
    ``getattr(..., "transaction")``, and unpacked or positional arguments.
    """
    tree = ast.parse(source)
    called = {
        id(n.func)
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    sites: list[tuple[int, bool]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "transaction":
            if id(node) not in called:
                raise AssertionError(f"{where}:{node.lineno}: .transaction not called directly")
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) > 1
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "transaction"
        ):
            raise AssertionError(f"{where}:{node.lineno}: transaction reached through getattr")
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "transaction"
        ):
            continue
        if node.args or any(kw.arg is None for kw in node.keywords):
            raise AssertionError(f"{where}:{node.lineno}: transaction() arguments not readable")
        waits = [kw for kw in node.keywords if kw.arg == "wait"]
        if waits and not (
            isinstance(waits[0].value, ast.Name) and waits[0].value.id == "WRITER_WAIT_S"
        ):
            raise AssertionError(
                f"{where}:{node.lineno}: waits on something other than WRITER_WAIT_S"
            )
        sites.append((node.lineno, bool(waits)))
    return [waits for _, waits in sorted(sites)]


def test_every_transaction_call_site_is_classified():
    src = Path(memtomem.__file__).parent
    found: dict[str, list[bool]] = {}
    for path in sorted(src.rglob("*.py")):
        rel = path.relative_to(src).as_posix()
        sites = _transaction_sites(path.read_text(encoding="utf-8"), rel)
        if sites:
            found[rel] = sites
    assert found == _TRANSACTION_SITES
    assert WRITER_WAIT_S == 5.0


@pytest.mark.parametrize(
    "source",
    [
        'storage.transaction(**{"wait": WRITER_WAIT_S})',
        "storage.transaction(WRITER_WAIT_S)",
        'getattr(storage, "transaction")()',
        "tx = storage.transaction\ntx()",
        "run(storage.transaction)",
        "storage.transaction(wait=1.0)",
    ],
)
def test_call_site_scanner_refuses_what_it_cannot_classify(source: str):
    with pytest.raises(AssertionError):
        _transaction_sites(source, "<case>")


def test_call_site_scanner_classifies_direct_calls():
    source = "a.transaction(wait=WRITER_WAIT_S)\nb.transaction()\n"
    assert _transaction_sites(source, "<case>") == [True, False]


async def test_nested_same_task_transaction_refused_immediately(storage: SqliteBackend):
    """Same-task nested transaction() raises StorageError immediately without queuing."""
    async with storage.transaction():
        with pytest.raises(StorageError, match="Nested transactions are not supported"):
            async with storage.transaction():
                pass


async def test_unowned_open_transaction_refused_immediately(storage: SqliteBackend):
    """An open SQLite transaction not owned by transaction() is refused immediately."""
    assert storage._db is not None
    storage._db.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(StorageError, match="already has an open transaction"):
            async with storage.transaction():
                pass
    finally:
        storage._db.rollback()


async def test_concurrent_bm25_and_dense_search_never_share_reader_conn(
    storage: SqliteBackend, tmp_path: Path
):
    """Concurrent searches lease exclusive readers and queue safely when concurrency > pool size."""
    chunks = [
        _make_chunk(
            f"concurrency search target {i}",
            source=tmp_path / f"s{i}.md",
            line=1,
            embedding=[0.1 * (i + 1), 0.2, 0.3, 0.4],
        )
        for i in range(5)
    ]
    await storage.upsert_chunks(chunks)

    active_conn_ids: set[int] = set()
    overlap_detected = False
    orig_run_read = storage._run_read_in_thread

    async def tracked_run_read(conn, fn):
        nonlocal overlap_detected
        cid = id(conn)
        if cid in active_conn_ids:
            overlap_detected = True
        active_conn_ids.add(cid)
        try:
            await asyncio.sleep(0.01)
            return await orig_run_read(conn, fn)
        finally:
            active_conn_ids.discard(cid)

    storage._run_read_in_thread = tracked_run_read  # type: ignore[method-assign]

    # Run 8 concurrent searches (4 BM25 + 4 Dense) against a 2-connection read pool
    tasks = []
    for _ in range(4):
        tasks.append(
            asyncio.create_task(
                storage.bm25_search("concurrency search target", project_context_root=None)
            )
        )
        tasks.append(
            asyncio.create_task(
                storage.dense_search([0.1, 0.2, 0.3, 0.4], top_k=5, project_context_root=None)
            )
        )

    results = await asyncio.gather(*tasks)
    assert not overlap_detected, "Two concurrent searches shared the same reader connection!"
    for res in results:
        assert len(res) >= 1
    assert storage._active_readers == 0
    assert not storage._in_use_readers


async def test_sync_lease_never_shares_a_leased_reader(storage: SqliteBackend):
    """With every pooled reader leased, a sync reader gets a connection of its own.

    The async leases may be held by worker threads mid-query; handing the sync
    caller one of them would run two threads on one ``sqlite3.Connection``.
    """
    pool = list(storage._read_pool)
    assert pool, "fixture must build a read pool"
    async with contextlib.AsyncExitStack() as stack:
        leased = [await stack.enter_async_context(storage._lease_read_db()) for _ in pool]
        assert {id(c) for c in leased} == {id(c) for c in pool}
        with storage._lease_read_db_sync() as db:
            assert all(db is not c for c in pool)
            assert db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
            private = db
        with pytest.raises(sqlite3.ProgrammingError):
            private.execute("SELECT 1")
        assert id(private) not in storage._schema_checked_at
    assert storage._active_readers == 0
    assert not storage._in_use_readers


async def test_cancelled_reader_keeps_its_lease_until_the_worker_returns(storage: SqliteBackend):
    """A thread cannot be cancelled, so its caller's lease must outlive the cancel."""
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def slow(conn: sqlite3.Connection) -> int:
        started.set()
        release.wait(10)
        try:
            return conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        finally:
            finished.set()

    async def reader() -> int:
        async with storage._lease_read_db() as db:
            return await storage._run_read_in_thread(db, slow)

    task = asyncio.create_task(reader())
    await asyncio.to_thread(started.wait, 10)
    task.cancel()
    await asyncio.sleep(0.05)
    # Cancelled twice over: still waiting on the worker, lease still held.
    task.cancel()
    await asyncio.sleep(0.05)
    assert not task.done()
    assert storage._active_readers == 1
    assert len(storage._in_use_readers) == 1

    close_task = asyncio.create_task(storage.close())
    await asyncio.sleep(0.05)
    assert not close_task.done(), "close() must wait for the cancelled reader's worker"

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()
    await close_task
    assert storage._active_readers == 0


async def test_reader_lease_outlives_a_cancel_of_every_task(storage: SqliteBackend):
    """``asyncio.run`` shutdown cancels every Task; the lease must still wait for the thread."""
    started = threading.Event()
    finished = threading.Event()
    worker_error: list[BaseException] = []

    def slow(conn: sqlite3.Connection) -> int:
        started.set()
        time.sleep(0.3)
        try:
            return conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        except BaseException as exc:
            worker_error.append(exc)
            raise
        finally:
            finished.set()

    async def reader() -> int:
        async with storage._lease_read_db() as db:
            return await storage._run_read_in_thread(db, slow)

    task = asyncio.create_task(reader())
    await asyncio.to_thread(started.wait, 10)
    me = asyncio.current_task()
    for other in asyncio.all_tasks():
        if other is not me:
            other.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert finished.is_set(), "the reader task ended while its worker was still running"
    await storage.close()
    assert not worker_error


async def test_cancelled_close_still_closes_every_connection(tmp_path: Path):
    """Cancelling close() mid-drain must not leave the store open and refusing leases."""
    backend = SqliteBackend(
        StorageConfig(sqlite_path=str(tmp_path / "close_cancel.db")), dimension=4
    )
    await backend.initialize()
    pool = list(backend._read_pool)
    started = threading.Event()

    def slow(conn: sqlite3.Connection) -> int:
        started.set()
        time.sleep(0.3)
        return conn.execute("SELECT 1").fetchone()[0]

    async def reader() -> int:
        async with backend._lease_read_db() as db:
            return await backend._run_read_in_thread(db, slow)

    read_task = asyncio.create_task(reader())
    await asyncio.to_thread(started.wait, 10)
    close_task = asyncio.create_task(backend.close())
    await asyncio.sleep(0.05)
    read_task.cancel()
    close_task.cancel()
    results = await asyncio.gather(read_task, close_task, return_exceptions=True)

    assert all(isinstance(r, asyncio.CancelledError) for r in results)
    assert backend._db is None
    for conn in pool:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


async def test_close_waits_for_active_readers_and_rejects_new_requests(tmp_path: Path):
    """close() waits for in-flight leased readers to finish and rejects new reads/writes."""
    cfg = StorageConfig(sqlite_path=str(tmp_path / "phase2_close_drain.db"))
    backend = SqliteBackend(cfg, dimension=4)
    await backend.initialize()

    reader_leased = asyncio.Event()
    release_reader = asyncio.Event()
    close_finished = False

    async def slow_reader() -> int:
        async with backend._lease_read_db() as db:
            reader_leased.set()
            await release_reader.wait()
            return db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]

    reader_task = asyncio.create_task(slow_reader())
    await reader_leased.wait()

    async def run_close() -> None:
        nonlocal close_finished
        await backend.close()
        close_finished = True

    close_task = asyncio.create_task(run_close())
    await asyncio.sleep(0.02)
    # close() must still be waiting for slow_reader to release its lease
    assert not close_finished

    # While closing, new read or write requests must fail closed
    with pytest.raises(StorageError, match="Database is closing"):
        async with backend._lease_read_db():
            pass
    with pytest.raises(StorageError, match="Database is closing"):
        backend._get_db()

    release_reader.set()
    count = await reader_task
    assert count == 0
    await close_task
    assert close_finished
