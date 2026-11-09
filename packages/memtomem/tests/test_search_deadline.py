"""``deadline_ms`` on mem_search: a server-processing budget, not a response deadline.

Pinned here: expiry raises ``SearchDeadlineExceeded`` (rendered as a retryable
error) and records nothing; the wait for a read connection and a still-queued
embedding slice are bounded, while a slice or read already running completes;
every leg that degrades on ``Exception`` lets the expiry through; the BM25 task
is settled, never orphaned; and a search without a budget makes exactly the
calls it made before.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from helpers import BUDGET_TOLERANCE_S, StubCtx, make_chunk
from memtomem.config import SearchConfig
from memtomem.errors import SearchDeadlineExceeded
from memtomem.models import SearchResult
from memtomem.search.deadline import SearchDeadline
from memtomem.search.pipeline import SearchPipeline


def _result(content: str = "hit") -> SearchResult:
    return SearchResult(chunk=make_chunk(content), score=1.0, rank=1, source="test")


def _pipeline(*, bm25=None, dense=None, embed=None, enable_dense: bool = False) -> SearchPipeline:
    storage = AsyncMock()
    storage.bm25_search = bm25 or AsyncMock(return_value=[_result()])
    storage.dense_search = dense or AsyncMock(return_value=[])
    storage.increment_access = AsyncMock()
    storage.save_query_history = AsyncMock()
    storage.get_access_counts = AsyncMock(return_value={})
    storage.get_embeddings_for_chunks = AsyncMock(return_value={})
    storage.get_importance_scores = AsyncMock(return_value={})
    storage.count_chunks_by_ns_prefix_detail = AsyncMock(return_value=(0, {}))
    embedder = AsyncMock()
    embedder.embed_query = embed or AsyncMock(return_value=[0.1] * 8)
    return SearchPipeline(
        storage=storage,
        embedder=embedder,
        config=SearchConfig(enable_bm25=True, enable_dense=enable_dense),
    )


def test_deadline_helper():
    assert SearchDeadline.from_ms(None) is None
    d = SearchDeadline.from_ms(60_000)
    assert d is not None and 59 < d.remaining() <= 60 + BUDGET_TOLERANCE_S
    d.check("start")
    expired = SearchDeadline(time.monotonic() - 1, 5)
    with pytest.raises(SearchDeadlineExceeded, match="5 ms exceeded at fusion"):
        expired.check("fusion")


async def test_without_a_budget_no_call_gains_a_deadline_argument():
    pipeline = _pipeline(enable_dense=True)
    results, _ = await pipeline.search("query", top_k=3)
    assert results
    assert "deadline" not in pipeline._storage.bm25_search.call_args.kwargs
    assert "deadline" not in pipeline._storage.dense_search.call_args.kwargs
    assert pipeline._embedder.embed_query.call_args.kwargs == {}


async def test_with_a_budget_reads_receive_the_deadline_instant():
    pipeline = _pipeline(enable_dense=True)
    before = time.monotonic()
    results, _ = await pipeline.search("query", top_k=3, deadline_ms=5_000)
    assert results
    at = pipeline._storage.bm25_search.call_args.kwargs["deadline"]
    assert before + 4.9 < at < time.monotonic() + 5.1
    assert pipeline._storage.dense_search.call_args.kwargs["deadline"] == at
    # A mock embedder does not declare ``supports_query_deadline``.
    assert pipeline._embedder.embed_query.call_args.kwargs == {}


async def test_an_expiry_in_the_bm25_leg_raises_and_records_nothing():
    async def expired(*args, **kwargs):
        raise SearchDeadlineExceeded("pool wait")

    pipeline = _pipeline(bm25=AsyncMock(side_effect=expired))
    with pytest.raises(SearchDeadlineExceeded, match="pool wait"):
        await pipeline.search("query", top_k=3, deadline_ms=600_000)
    pipeline._storage.increment_access.assert_not_called()
    pipeline._storage.save_search_observation.assert_not_called()


async def test_an_expiry_in_the_dense_leg_settles_the_bm25_task():
    started = asyncio.Event()
    finished: list[str] = []

    async def slow_bm25(*args, **kwargs):
        started.set()
        try:
            await asyncio.sleep(30)
        finally:
            finished.append("bm25")
        return []

    async def expired_embed(*args, **kwargs):
        await started.wait()
        raise SearchDeadlineExceeded("raised by the embedding slice")

    pipeline = _pipeline(
        bm25=AsyncMock(side_effect=slow_bm25),
        embed=AsyncMock(side_effect=expired_embed),
        enable_dense=True,
    )
    # A long budget, so no later checkpoint can raise in the dense leg's place:
    # the error must be the one the leg let through, and arrive at once.
    started_at = time.monotonic()
    with pytest.raises(SearchDeadlineExceeded, match="raised by the embedding slice"):
        await pipeline.search("query", top_k=3, deadline_ms=600_000)
    assert time.monotonic() - started_at < 5
    assert finished == ["bm25"], "the BM25 task outlived the search"


async def test_an_external_cancel_settles_the_bm25_task_too():
    started = asyncio.Event()
    finished: list[str] = []

    async def slow_bm25(*args, **kwargs):
        started.set()
        try:
            await asyncio.sleep(30)
        finally:
            finished.append("bm25")
        return []

    async def slow_embed(*args, **kwargs):
        await asyncio.sleep(30)
        return [0.1] * 8

    pipeline = _pipeline(
        bm25=AsyncMock(side_effect=slow_bm25),
        embed=AsyncMock(side_effect=slow_embed),
        enable_dense=True,
    )
    task = asyncio.create_task(pipeline.search("query", top_k=3))
    await started.wait()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished == ["bm25"]


async def test_a_dense_failure_still_degrades_without_a_budget():
    pipeline = _pipeline(dense=AsyncMock(side_effect=RuntimeError("vec down")), enable_dense=True)
    results, stats = await pipeline.search("query", top_k=3)
    assert results and stats.dense_error is not None


async def test_a_budget_spent_before_retrieval_raises_at_a_stage_boundary(monkeypatch):
    import memtomem.search.deadline as deadline_mod

    clock = iter([100.0, 100.0] + [200.0] * 100)
    monkeypatch.setattr(deadline_mod.time, "monotonic", lambda: next(clock))
    pipeline = _pipeline()
    with pytest.raises(SearchDeadlineExceeded, match="exceeded at"):
        await pipeline.search("query", top_k=3, deadline_ms=10)
    pipeline._storage.bm25_search.assert_not_called()


async def test_an_embedder_that_takes_a_deadline_gets_the_instant():
    pipeline = _pipeline(enable_dense=True)
    pipeline._embedder.supports_query_deadline = True
    await pipeline.search("query", top_k=3, deadline_ms=5_000)
    assert "deadline" in pipeline._embedder.embed_query.call_args.kwargs


class TestReadPoolWait:
    async def test_a_lease_waits_no_longer_than_the_deadline(self, bm25_only_components):
        comp, _ = bm25_only_components
        storage = comp.storage
        if not storage._read_pool:
            pytest.skip("no read pool configured")
        with storage._read_pool_lock:
            busy = {id(c) for c in storage._read_pool}
            storage._in_use_readers |= busy
        try:
            started = time.monotonic()
            with pytest.raises(SearchDeadlineExceeded, match="read connection"):
                async with storage._lease_read_db(deadline=time.monotonic() + 0.05):
                    pass
            assert time.monotonic() - started < 2
            assert not storage._reader_waiters
        finally:
            with storage._read_pool_lock:
                storage._in_use_readers -= busy
            storage._notify_reader_waiters(all_waiters=True)

    async def test_a_lease_without_a_deadline_still_waits_for_a_reader(self, bm25_only_components):
        comp, _ = bm25_only_components
        storage = comp.storage
        if not storage._read_pool:
            pytest.skip("no read pool configured")
        with storage._read_pool_lock:
            busy = {id(c) for c in storage._read_pool}
            storage._in_use_readers |= busy

        async def lease():
            async with storage._lease_read_db() as db:
                return db is not None

        task = asyncio.create_task(lease())
        await asyncio.sleep(0.05)
        assert not task.done()
        with storage._read_pool_lock:
            storage._in_use_readers -= busy
        storage._notify_reader_waiters(all_waiters=True)
        assert await asyncio.wait_for(task, 5) is True


class TestOnnxSliceStart:
    @staticmethod
    def _embedder():
        from memtomem.config import EmbeddingConfig
        from memtomem.embedding.onnx import OnnxEmbedder

        config = EmbeddingConfig(
            provider="onnx", model="all-MiniLM-L6-v2", dimension=1, base_url="", api_key=""
        )
        return OnnxEmbedder(config)

    async def test_an_expired_query_raises_the_deadline_error_not_an_embedding_error(self):
        embedder = self._embedder()
        ran: list[bool] = []
        embedder._get_model = lambda: ran.append(True)  # type: ignore[method-assign]
        with pytest.raises(SearchDeadlineExceeded):
            await embedder.embed_query("q", deadline=time.monotonic() - 1)
        assert ran == []

    async def test_a_slice_queued_past_the_deadline_never_runs(self):
        import threading

        from memtomem.embedding.onnx import OnnxEmbedder

        embedder = self._embedder()
        release = threading.Event()
        started = threading.Event()
        model_calls: list[str] = []
        real_sync = OnnxEmbedder._embed_sync

        def sync(texts, *args, **kwargs):
            if texts == ["first"]:
                started.set()
                release.wait(10)  # the single executor worker is busy with this slice
                return [[0.0]]
            return real_sync(embedder, texts, *args, **kwargs)

        embedder._embed_sync = sync  # type: ignore[method-assign]
        embedder._get_model = lambda: model_calls.append("model")  # type: ignore[method-assign]
        first = asyncio.create_task(embedder.embed_texts(["first"]))
        await asyncio.get_running_loop().run_in_executor(None, started.wait, 5)
        queued = asyncio.create_task(
            embedder.embed_query("second", deadline=time.monotonic() + 0.05)
        )
        await asyncio.sleep(0.15)  # the deadline passes while the slice waits in the queue
        release.set()
        assert await first == [[0.0]]
        with pytest.raises(SearchDeadlineExceeded):
            await queued
        assert model_calls == []

    def test_onnx_declares_it_takes_a_deadline(self):
        from memtomem.embedding import onnx

        assert onnx.OnnxEmbedder.supports_query_deadline is True


class TestAuxiliaryLegs:
    async def test_an_expired_lease_is_refused_even_with_a_reader_free(self, bm25_only_components):
        comp, _ = bm25_only_components
        with pytest.raises(SearchDeadlineExceeded, match="before a read connection"):
            async with comp.storage._lease_read_db(deadline=time.monotonic() - 1):
                pass

    async def test_dense_retrieval_does_not_start_after_an_embedding_that_spent_the_budget(self):
        async def slow_embed(*args, **kwargs):
            await asyncio.sleep(0.2)
            return [0.1] * 8

        pipeline = _pipeline(embed=AsyncMock(side_effect=slow_embed), enable_dense=True)
        with pytest.raises(SearchDeadlineExceeded, match="dense retrieval"):
            await pipeline.search("query", top_k=3, deadline_ms=50)
        pipeline._storage.dense_search.assert_not_called()

    async def test_heading_expansion_propagates_an_expiry_instead_of_falling_back(self):
        from memtomem.search.expansion import expand_query_headings

        storage = AsyncMock()
        storage.dense_search = AsyncMock(side_effect=SearchDeadlineExceeded("pool wait"))
        embedder = AsyncMock()
        embedder.embed_query = AsyncMock(return_value=[0.1] * 8)
        with pytest.raises(SearchDeadlineExceeded):
            await expand_query_headings("query", storage, embedder, deadline=time.monotonic() + 60)
        assert storage.dense_search.call_args.kwargs["deadline"] > time.monotonic()

    async def test_heading_expansion_without_a_budget_still_falls_back(self):
        from memtomem.search.expansion import expand_query_headings

        storage = AsyncMock()
        storage.dense_search = AsyncMock(side_effect=RuntimeError("vec down"))
        embedder = AsyncMock()
        embedder.embed_query = AsyncMock(return_value=[0.1] * 8)
        assert await expand_query_headings("query", storage, embedder) == "query"
        assert "deadline" not in storage.dense_search.call_args.kwargs

    async def test_the_rescue_leg_raises_an_expiry_after_both_legs_finish(self):
        finished: list[str] = []

        async def expired_bm25(*args, **kwargs):
            raise SearchDeadlineExceeded("rescue pool wait")

        async def slow_dense(*args, **kwargs):
            await asyncio.sleep(0.05)
            finished.append("dense")
            return []

        pipeline = _pipeline(
            bm25=AsyncMock(side_effect=expired_bm25),
            dense=AsyncMock(side_effect=slow_dense),
            enable_dense=True,
        )
        with pytest.raises(SearchDeadlineExceeded, match="rescue pool wait"):
            await pipeline._rescue_retrieval(
                "query",
                [0.1] * 8,
                top_k=3,
                boost_sources={"/tmp/x.md"},
                use_bm25=True,
                use_dense=True,
                deadline=time.monotonic() + 60,
            )
        assert finished == ["dense"]


class TestTool:
    async def test_mem_search_renders_an_expiry_as_a_retryable_error(
        self, bm25_only_components, monkeypatch
    ):
        from memtomem.server.context import AppContext
        from memtomem.server.tools.search import mem_search

        comp, _ = bm25_only_components
        ctx = StubCtx(AppContext.from_components(comp))

        async def expired(*args, **kwargs):
            raise SearchDeadlineExceeded("deadline of 5 ms exceeded at retrieval")

        monkeypatch.setattr(comp.storage, "bm25_search", expired)
        out = await mem_search(query="anything", deadline_ms=5, ctx=ctx)  # type: ignore[arg-type]
        assert out.startswith("Error (retryable): deadline of 5 ms exceeded")

    @pytest.mark.parametrize("bad", [0, -1, True])
    async def test_mem_search_refuses_a_bad_deadline(self, bad):
        from memtomem.server.tools.search import mem_search

        out = await mem_search(query="anything", deadline_ms=bad)  # type: ignore[arg-type]
        assert out.startswith("Error: deadline_ms must be a positive integer")


class TestEveryLeaseOfTheSearchTask:
    async def test_a_real_search_with_the_pool_exhausted_expires_promptly(
        self, bm25_only_components
    ):
        comp, _ = bm25_only_components
        storage = comp.storage
        if not storage._read_pool:
            pytest.skip("no read pool configured")
        with storage._read_pool_lock:
            busy = {id(c) for c in storage._read_pool}
            storage._in_use_readers |= busy
        try:
            started = time.monotonic()
            with pytest.raises(SearchDeadlineExceeded):
                await comp.search_pipeline.search("anything", top_k=3, deadline_ms=100)
            assert time.monotonic() - started < 3
        finally:
            with storage._read_pool_lock:
                storage._in_use_readers -= busy
            storage._notify_reader_waiters(all_waiters=True)

    async def test_the_ambient_budget_applies_to_the_search_task_only(self):
        from memtomem.search.deadline import ambient_read_deadline, bound_to_current_task

        deadline = SearchDeadline.from_ms(60_000)
        with bound_to_current_task(deadline):
            assert ambient_read_deadline() == deadline.at

            async def background():
                return ambient_read_deadline()

            assert await asyncio.create_task(background()) is None
        assert ambient_read_deadline() is None

    async def test_without_a_budget_nothing_is_bound(self):
        from memtomem.search.deadline import ambient_read_deadline, bound_to_current_task

        with bound_to_current_task(None):
            assert ambient_read_deadline() is None
