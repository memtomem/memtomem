"""Tests for reranker pipeline components."""

import pytest
from pathlib import Path
from uuid import uuid4
from memtomem.models import Chunk, ChunkMetadata, SearchResult


def _make_result(content, score, rank=1):
    chunk = Chunk(
        content=content,
        metadata=ChunkMetadata(source_file=Path("/tmp/test.md")),
        id=uuid4(),
        embedding=[],
    )
    return SearchResult(chunk=chunk, score=score, rank=rank, source="fused")


class TestCohereReranker:
    def test_init(self):
        from memtomem.config import RerankConfig
        from memtomem.search.reranker.cohere import CohereReranker

        config = RerankConfig(enabled=True, provider="cohere", api_key="test-key")
        reranker = CohereReranker(config)
        assert reranker._config.api_key == "test-key"
        assert reranker._client is None

    @pytest.mark.asyncio
    async def test_empty_results(self):
        from memtomem.config import RerankConfig
        from memtomem.search.reranker.cohere import CohereReranker

        config = RerankConfig(enabled=True, provider="cohere", api_key="test")
        reranker = CohereReranker(config)
        result = await reranker.rerank("query", [], top_k=5)
        assert result == []

    @pytest.mark.asyncio
    async def test_close(self):
        from memtomem.config import RerankConfig
        from memtomem.search.reranker.cohere import CohereReranker

        config = RerankConfig(enabled=True, provider="cohere", api_key="test")
        reranker = CohereReranker(config)
        await reranker.close()
        assert reranker._client is None

    @pytest.mark.asyncio
    async def test_closed_instance_refuses_resurrect(self):
        """#1778: post-close use must raise, not re-create the httpx client —
        a client born after close() on a swapped-out instance leaks."""
        from memtomem.config import RerankConfig
        from memtomem.search.reranker.cohere import CohereReranker

        config = RerankConfig(enabled=True, provider="cohere", api_key="test")
        reranker = CohereReranker(config)
        # Positive control: a live instance builds its client on demand.
        assert reranker._get_client() is not None

        await reranker.close()
        assert reranker._client is None

        with pytest.raises(RuntimeError, match="closed"):
            await reranker.rerank("query", [_make_result("a", 1.0)], top_k=5)
        assert reranker._client is None  # no new client materialized

        await reranker.close()  # idempotent


class TestLocalReranker:
    def test_init(self):
        from memtomem.config import RerankConfig
        from memtomem.search.reranker.local import LocalReranker

        config = RerankConfig(
            enabled=True, provider="local", model="cross-encoder/ms-marco-MiniLM-L-6-v2"
        )
        reranker = LocalReranker(config)
        assert reranker._model is None  # lazy loaded

    @pytest.mark.asyncio
    async def test_empty_results(self):
        from memtomem.config import RerankConfig
        from memtomem.search.reranker.local import LocalReranker

        config = RerankConfig(enabled=True, provider="local")
        reranker = LocalReranker(config)
        result = await reranker.rerank("query", [], top_k=5)
        assert result == []

    @pytest.mark.asyncio
    async def test_close(self):
        from memtomem.config import RerankConfig
        from memtomem.search.reranker.local import LocalReranker

        config = RerankConfig(enabled=True, provider="local")
        reranker = LocalReranker(config)
        await reranker.close()
        assert reranker._model is None

    @pytest.mark.asyncio
    async def test_closed_instance_refuses_resurrect(self):
        """#1778: post-close use must raise, not silently reload the model."""
        from memtomem.config import RerankConfig
        from memtomem.search.reranker.local import LocalReranker

        config = RerankConfig(enabled=True, provider="local")
        reranker = LocalReranker(config)
        # Positive control: a live instance serves its cached model.
        sentinel = object()
        reranker._model = sentinel
        assert reranker._get_model() is sentinel

        await reranker.close()
        assert reranker._model is None

        with pytest.raises(RuntimeError, match="closed"):
            await reranker.rerank("query", [_make_result("a", 1.0)], top_k=5)
        assert reranker._model is None  # no reload

        await reranker.close()  # idempotent

    def test_close_landing_at_lock_acquisition_refuses_load(self):
        """A loader thread can pass the outer _closed guard, then lose the
        race to a concurrent close() before acquiring _load_lock; the
        re-check under the lock must refuse the load (#1778 review).
        Deterministic pin: a lock wrapper flips _closed at the acquisition
        point instead of racing real threads."""
        import threading

        from memtomem.config import RerankConfig
        from memtomem.search.reranker.local import LocalReranker

        reranker = LocalReranker(RerankConfig(enabled=True, provider="local"))

        class _CloseOnAcquire:
            def __init__(self) -> None:
                self._inner = threading.Lock()

            def __enter__(self):
                reranker._closed = True  # the concurrent close() lands here
                return self._inner.__enter__()

            def __exit__(self, *exc):
                return self._inner.__exit__(*exc)

        reranker._load_lock = _CloseOnAcquire()

        with pytest.raises(RuntimeError, match="closed"):
            reranker._get_model()
        assert reranker._model is None

    @pytest.mark.asyncio
    async def test_close_during_construction_does_not_publish_model(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """close() landing while the model constructor is in flight must not
        leave the finished model published on the closed instance (#1780
        codex review). The real async close() runs mid-construction, gated
        by events. sentence_transformers is not a test dependency, so a
        pausing stand-in module is injected."""
        import asyncio
        import sys
        import threading
        import types

        from memtomem.config import RerankConfig
        from memtomem.search.reranker.local import LocalReranker

        entered = threading.Event()
        release = threading.Event()

        class _PausingModel:
            def __init__(self, model_name: str) -> None:
                entered.set()
                assert release.wait(5)

        monkeypatch.setitem(
            sys.modules,
            "sentence_transformers",
            types.SimpleNamespace(CrossEncoder=_PausingModel),
        )

        reranker = LocalReranker(RerankConfig(enabled=True, provider="local"))
        errors: list[BaseException] = []

        def load() -> None:
            try:
                reranker._get_model()
            except RuntimeError as exc:
                errors.append(exc)

        loader = threading.Thread(target=load)
        loader.start()
        assert await asyncio.to_thread(entered.wait, 5)  # constructor in flight

        await reranker.close()  # lands mid-construction

        release.set()
        await asyncio.to_thread(loader.join, 5)
        assert not loader.is_alive()

        assert reranker._model is None  # the finished model was not published
        assert errors and "closed" in str(errors[0])


class TestRerankerFactory:
    def test_disabled(self):
        from memtomem.config import RerankConfig
        from memtomem.search.reranker.factory import create_reranker

        assert create_reranker(RerankConfig(enabled=False)) is None

    def test_cohere(self):
        from memtomem.config import RerankConfig
        from memtomem.search.reranker.factory import create_reranker
        from memtomem.search.reranker.cohere import CohereReranker

        r = create_reranker(RerankConfig(enabled=True, provider="cohere"))
        assert isinstance(r, CohereReranker)

    def test_local(self):
        from memtomem.config import RerankConfig
        from memtomem.search.reranker.factory import create_reranker
        from memtomem.search.reranker.local import LocalReranker

        r = create_reranker(RerankConfig(enabled=True, provider="local"))
        assert isinstance(r, LocalReranker)

    def test_unknown_raises(self):
        from memtomem.config import RerankConfig
        from memtomem.search.reranker.factory import create_reranker

        with pytest.raises(ValueError):
            create_reranker(RerankConfig(enabled=True, provider="unknown"))


class TestRerankerExecutorLifecycle:
    """#1783 parity: local rerankers run inference on a dedicated 1-worker
    executor (never the shared default pool, never the event loop), and
    close() shuts it down so a swapped-out instance cannot keep a worker."""

    @pytest.mark.asyncio
    async def test_local_close_shuts_down_its_executor(self):
        from memtomem.config import RerankConfig
        from memtomem.search.reranker.local import LocalReranker

        reranker = LocalReranker(RerankConfig(enabled=True, provider="local"))
        assert reranker._infer_executor._max_workers == 1
        await reranker.close()
        assert reranker._infer_executor._shutdown is True

    @pytest.mark.asyncio
    async def test_fastembed_close_shuts_down_its_executor(self):
        from memtomem.config import RerankConfig
        from memtomem.search.reranker.fastembed import FastEmbedReranker

        reranker = FastEmbedReranker(RerankConfig(enabled=True, provider="fastembed"))
        assert reranker._infer_executor._max_workers == 1
        await reranker.close()
        assert reranker._infer_executor._shutdown is True

    @pytest.mark.asyncio
    async def test_local_inference_runs_off_the_event_loop(self):
        """model.predict used to run directly on the loop thread, freezing
        every other coroutine for the duration of a torch forward pass."""
        import threading

        from memtomem.config import RerankConfig
        from memtomem.search.reranker.local import LocalReranker

        reranker = LocalReranker(RerankConfig(enabled=True, provider="local"))
        loop_thread = threading.current_thread()
        seen: dict[str, object] = {}

        class _FakeModel:
            def predict(self, pairs):
                seen["thread"] = threading.current_thread()
                return [0.5] * len(pairs)

        reranker._model = _FakeModel()
        out = await reranker.rerank("q", [_make_result("a", 1.0)], top_k=5)
        assert out[0].source == "reranked"
        assert seen["thread"] is not loop_thread
        assert seen["thread"].name.startswith("local-rerank")
        await reranker.close()


class TestRerankerAbandonedWorker:
    """A wait_for timeout cancels the awaiting coroutine, but a running
    native inference keeps the lone dedicated worker. Later reranks must
    fail fast (original order) instead of queuing behind the wedged worker
    and each paying the full timeout; the worker finishing clears the state,
    and close() must not return while the worker still owns the model."""

    @pytest.mark.asyncio
    async def test_local_busy_fast_fail_after_timeout_then_recovers(self):
        import asyncio
        import threading

        from memtomem.config import RerankConfig
        from memtomem.search.reranker.local import LocalReranker

        reranker = LocalReranker(RerankConfig(enabled=True, provider="local"))
        release = threading.Event()

        class _BlockingModel:
            def predict(self, pairs):
                release.wait(10)
                return [0.5] * len(pairs)

        reranker._model = _BlockingModel()
        candidates = [_make_result("a", 1.0)]

        with pytest.raises(TimeoutError):
            await asyncio.wait_for(reranker.rerank("q", candidates, top_k=5), timeout=0.05)
        assert reranker._abandoned is not None

        # The wedged worker is still running: this must return immediately
        # with the original order, not queue behind it.
        out = await reranker.rerank("q", candidates, top_k=5)
        assert out[0].source == "fused"

        release.set()
        for _ in range(200):
            if reranker._abandoned is None or reranker._abandoned.done():
                break
            await asyncio.sleep(0.01)
        out = await reranker.rerank("q", candidates, top_k=5)
        assert out[0].source == "reranked"
        await reranker.close()

    @pytest.mark.asyncio
    async def test_local_close_waits_for_the_running_inference(self):
        import asyncio
        import threading
        import time

        from memtomem.config import RerankConfig
        from memtomem.search.reranker.local import LocalReranker

        reranker = LocalReranker(RerankConfig(enabled=True, provider="local"))
        finished = threading.Event()

        class _SlowModel:
            def predict(self, pairs):
                time.sleep(0.2)
                finished.set()
                return [0.5] * len(pairs)

        reranker._model = _SlowModel()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                reranker.rerank("q", [_make_result("a", 1.0)], top_k=5), timeout=0.05
            )

        # close() must drain: when it returns, the worker has finished and
        # no thread still owns the model.
        await reranker.close()
        assert finished.is_set()
        assert reranker._infer_executor._shutdown is True

    @pytest.mark.asyncio
    async def test_fastembed_busy_fast_fail_after_timeout_then_recovers(self):
        import asyncio
        import threading

        from memtomem.config import RerankConfig
        from memtomem.search.reranker.fastembed import FastEmbedReranker

        reranker = FastEmbedReranker(RerankConfig(enabled=True, provider="fastembed"))
        release = threading.Event()

        class _BlockingModel:
            def rerank(self, query, documents):
                release.wait(10)
                return [0.5] * len(documents)

        reranker._model = _BlockingModel()
        candidates = [_make_result("a", 1.0)]

        with pytest.raises(TimeoutError):
            await asyncio.wait_for(reranker.rerank("q", candidates, top_k=5), timeout=0.05)
        assert reranker._abandoned is not None

        out = await reranker.rerank("q", candidates, top_k=5)
        assert out[0].source == "fused"

        release.set()
        for _ in range(200):
            if reranker._abandoned is None or reranker._abandoned.done():
                break
            await asyncio.sleep(0.01)
        out = await reranker.rerank("q", candidates, top_k=5)
        assert out[0].source == "reranked"
        await reranker.close()

    @pytest.mark.asyncio
    async def test_fastembed_close_waits_for_the_running_inference(self):
        import asyncio
        import threading
        import time

        from memtomem.config import RerankConfig
        from memtomem.search.reranker.fastembed import FastEmbedReranker

        reranker = FastEmbedReranker(RerankConfig(enabled=True, provider="fastembed"))
        finished = threading.Event()

        class _SlowModel:
            def rerank(self, query, documents):
                time.sleep(0.2)
                finished.set()
                return [0.5] * len(documents)

        reranker._model = _SlowModel()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                reranker.rerank("q", [_make_result("a", 1.0)], top_k=5), timeout=0.05
            )

        await reranker.close()
        assert finished.is_set()
        assert reranker._infer_executor._shutdown is True


def test_rerank_snapshot_tracks_timeout_changes():
    """A disk edit changing only rerank.timeout_s must produce a different
    snapshot, or _sync_reranker skips the reinstall and the pipeline keeps
    running with the old timeout."""
    from types import SimpleNamespace

    from memtomem.config import RerankConfig
    from memtomem.web.hot_reload import _rerank_snapshot

    a = SimpleNamespace(rerank=RerankConfig(enabled=True, timeout_s=30.0))
    b = SimpleNamespace(rerank=RerankConfig(enabled=True, timeout_s=5.0))
    assert _rerank_snapshot(a) != _rerank_snapshot(b)


def test_rerank_timeout_rejects_non_finite():
    from memtomem.config import RerankConfig

    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="timeout_s must be positive"):
            RerankConfig(timeout_s=bad)
