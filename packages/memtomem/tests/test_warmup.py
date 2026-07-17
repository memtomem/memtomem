"""Tests for opt-in model warmup (#1621).

Three layers:

- ``warm_models`` unit contract — the ``_get_model`` sniff (local
  providers warm, remote/noop providers skip, already-loaded reported),
  the live-pipeline reranker read, and failure propagation.
- Lifespan integration against the real ``app_lifespan`` — flag off
  spawns nothing and preserves the #399 handshake invariant; flag on
  runs the full init + model preload in the background; failures are
  logged and never crash the server; shutdown cancels an in-flight
  warmup.
- ``mm warmup`` CLI — outcome lines on success, ``raise_cli_error``
  hint on failure.
"""

from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from memtomem.server import lifespan as lifespan_mod
from memtomem.server.warmup import warm_models

from .helpers import set_home


class _FakeLocalModel:
    """Stand-in for OnnxEmbedder / FastEmbedReranker: sync ``_get_model``
    that records whether it ran off the event-loop thread.
    """

    def __init__(self) -> None:
        self._model: object | None = None
        self.load_calls = 0
        self.ran_on_main_thread: bool | None = None

    def _get_model(self) -> object:
        self.load_calls += 1
        self.ran_on_main_thread = threading.current_thread() is threading.main_thread()
        self._model = object()
        return self._model


def _fake_components(embedder: object | None, reranker: object | None) -> SimpleNamespace:
    return SimpleNamespace(
        config=SimpleNamespace(
            embedding=SimpleNamespace(provider="onnx", model="embed-model"),
            rerank=SimpleNamespace(enabled=True, provider="fastembed", model="rerank-model"),
        ),
        embedder=embedder,
        search_pipeline=SimpleNamespace(_reranker=reranker),
    )


class TestWarmModels:
    @pytest.mark.asyncio
    async def test_loads_local_embedder_and_reranker_off_the_loop_thread(self):
        embedder = _FakeLocalModel()
        reranker = _FakeLocalModel()

        outcomes = await warm_models(_fake_components(embedder, reranker))

        assert [(o.component, o.status) for o in outcomes] == [
            ("embedder", "loaded"),
            ("reranker", "loaded"),
        ]
        assert embedder.load_calls == 1
        assert reranker.load_calls == 1
        # ``asyncio.to_thread`` contract: the sync load must not block the loop.
        assert embedder.ran_on_main_thread is False
        assert reranker.ran_on_main_thread is False
        # Provider/model strings come from config, for display.
        assert outcomes[0].provider == "onnx"
        assert outcomes[0].model == "embed-model"
        assert outcomes[1].provider == "fastembed"
        assert outcomes[1].model == "rerank-model"

    @pytest.mark.asyncio
    async def test_holders_without_get_model_are_skipped(self):
        """Remote providers (OllamaEmbedder, OpenAIEmbedder, CohereReranker)
        and NoopEmbedder expose no ``_get_model`` — warmup must skip them
        rather than issue real (possibly billed) API calls.
        """
        outcomes = await warm_models(_fake_components(object(), object()))

        assert [o.status for o in outcomes] == ["skipped", "skipped"]

    @pytest.mark.asyncio
    async def test_missing_reranker_is_skipped(self):
        """``rerank.enabled=False`` leaves the pipeline with ``_reranker=None``."""
        outcomes = await warm_models(_fake_components(_FakeLocalModel(), None))

        assert outcomes[1].component == "reranker"
        assert outcomes[1].status == "skipped"

    @pytest.mark.asyncio
    async def test_already_loaded_model_is_not_reloaded(self):
        embedder = _FakeLocalModel()
        embedder._model = object()

        outcomes = await warm_models(_fake_components(embedder, None))

        assert outcomes[0].status == "already-loaded"
        assert embedder.load_calls == 0

    @pytest.mark.asyncio
    async def test_reranker_read_from_live_pipeline_attribute(self):
        """The reranker must be read through ``search_pipeline._reranker``
        (hot-reload swaps it in place) — same contract as the readiness
        endpoint.
        """
        reranker = _FakeLocalModel()
        components = _fake_components(None, reranker)

        outcomes = await warm_models(components)

        assert outcomes[0].status == "skipped"  # embedder=None
        assert outcomes[1].status == "loaded"
        assert reranker.load_calls == 1

    @pytest.mark.asyncio
    async def test_load_failure_propagates_to_caller(self):
        """``warm_models`` raises — policy (log vs surface) is the caller's."""

        class _Broken(_FakeLocalModel):
            def _get_model(self) -> object:
                raise RuntimeError("download failed")

        with pytest.raises(RuntimeError, match="download failed"):
            await warm_models(_fake_components(_Broken(), None))


@pytest.fixture
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Same isolation shape as ``test_lazy_init_acceptance.isolated_state``:
    tmp ``HOME`` (config.json read path), pinned DB path, no memory dirs,
    fast NoopEmbedder, every optional subsystem off. Individual tests
    flip ``MEMTOMEM_WARMUP__ENABLED`` / the embedding provider on top.
    """
    home = tmp_path / "home"
    home.mkdir()
    db_path = tmp_path / "memtomem.db"
    set_home(monkeypatch, home)
    monkeypatch.setenv("MEMTOMEM_STORAGE__SQLITE_PATH", str(db_path))
    monkeypatch.setenv("MEMTOMEM_INDEXING__MEMORY_DIRS", "[]")
    monkeypatch.setenv("MEMTOMEM_EMBEDDING__PROVIDER", "none")
    monkeypatch.setenv("MEMTOMEM_CONSOLIDATION_SCHEDULE__ENABLED", "false")
    monkeypatch.setenv("MEMTOMEM_POLICY__ENABLED", "false")
    monkeypatch.setenv("MEMTOMEM_HEALTH_WATCHDOG__ENABLED", "false")
    monkeypatch.setenv("MEMTOMEM_WEBHOOK__ENABLED", "false")
    return {"home": home, "db_path": db_path}


class TestLifespanWarmup:
    @pytest.mark.asyncio
    async def test_flag_off_spawns_no_task_and_stays_lazy(self, isolated_state):
        """Default (off) path: no warmup task, no components, no DB —
        the #399 handshake invariant is untouched.
        """
        db_path = isolated_state["db_path"]

        async with lifespan_mod.app_lifespan(MagicMock()) as ctx:
            assert ctx._warmup_task is None
            assert ctx._components is None

        assert not db_path.exists()

    @pytest.mark.asyncio
    async def test_flag_on_inits_and_loads_models_in_background(self, isolated_state, monkeypatch):
        """Flag on: the lifespan spawns a task that runs the full init and
        hits the model-load choke point (``OnnxEmbedder._get_model``,
        monkeypatched here so no real download happens) before any tool
        call.
        """
        from memtomem.embedding.onnx import OnnxEmbedder

        monkeypatch.setenv("MEMTOMEM_WARMUP__ENABLED", "true")
        monkeypatch.setenv("MEMTOMEM_EMBEDDING__PROVIDER", "onnx")

        loads: list[str] = []

        def fake_get_model(self: OnnxEmbedder) -> object:
            loads.append(self._config.model)
            self._model = object()
            return self._model

        monkeypatch.setattr(OnnxEmbedder, "_get_model", fake_get_model)

        db_path = isolated_state["db_path"]

        async with lifespan_mod.app_lifespan(MagicMock()) as ctx:
            assert ctx._warmup_task is not None
            await ctx._warmup_task

            assert ctx._components is not None
            assert db_path.exists(), "warmup must run the full init (DB open)"
            assert len(loads) == 1, "embedder model load must happen exactly once"

    @pytest.mark.asyncio
    async def test_warmup_failure_logged_never_crashes_lifespan(
        self, isolated_state, monkeypatch, caplog
    ):
        from memtomem.embedding.onnx import OnnxEmbedder

        monkeypatch.setenv("MEMTOMEM_WARMUP__ENABLED", "true")
        monkeypatch.setenv("MEMTOMEM_EMBEDDING__PROVIDER", "onnx")

        def broken_get_model(self: OnnxEmbedder) -> object:
            raise RuntimeError("model download failed")

        monkeypatch.setattr(OnnxEmbedder, "_get_model", broken_get_model)

        with caplog.at_level("WARNING", logger="memtomem.server.warmup"):
            async with lifespan_mod.app_lifespan(MagicMock()) as ctx:
                assert ctx._warmup_task is not None
                await ctx._warmup_task  # must not raise — task swallows and logs

        assert any(
            "Model warmup failed" in rec.message and rec.levelname == "WARNING"
            for rec in caplog.records
        )

    @pytest.mark.asyncio
    async def test_shutdown_cancels_inflight_warmup(self, isolated_state, monkeypatch):
        """Exiting the lifespan while warmup is still running must cancel
        the task (``AppContext.close`` does it before component teardown)
        rather than leak it or hang shutdown.
        """
        import memtomem.server.warmup as warmup_mod

        monkeypatch.setenv("MEMTOMEM_WARMUP__ENABLED", "true")

        started = asyncio.Event()

        async def hanging_warm(components: object) -> list:
            started.set()
            await asyncio.Event().wait()  # block until cancelled
            return []

        monkeypatch.setattr(warmup_mod, "warm_models", hanging_warm)

        async with lifespan_mod.app_lifespan(MagicMock()) as ctx:
            task = ctx._warmup_task
            assert task is not None
            await asyncio.wait_for(started.wait(), timeout=5)

        assert task.cancelled(), "in-flight warmup must be cancelled on shutdown"
        assert ctx._warmup_task is None


class TestShutdownThreadSettlement:
    @pytest.mark.asyncio
    async def test_double_cancel_still_settles_queued_loader(self):
        """#1803: repeated cancellation must not cancel a queued load while
        ``_warm_one`` is already settling the first cancellation.
        """
        from concurrent.futures import ThreadPoolExecutor

        from memtomem.server.warmup import _warm_one

        blocker_started = threading.Event()
        blocker_release = threading.Event()
        load_calls: list[bool] = []

        def occupy_worker() -> None:
            blocker_started.set()
            blocker_release.wait(timeout=10)

        class _QueuedLocalModel:
            _model: object | None = None

            def _get_model(self) -> object:
                load_calls.append(True)
                self._model = object()
                return self._model

        loop = asyncio.get_running_loop()
        small = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-default")
        blocker = small.submit(occupy_worker)
        assert blocker_started.wait(timeout=5), "default-executor blocker did not start"
        loop.set_default_executor(small)

        holder = _QueuedLocalModel()
        task = asyncio.create_task(_warm_one("embedder", "onnx", "model", holder))
        try:
            await asyncio.sleep(0)  # submit the model load behind the blocker
            task.cancel()
            await asyncio.sleep(0)  # enter the first shielded settle iteration
            assert not task.done()

            task.cancel()
            await asyncio.sleep(0)  # deliver the second cancellation mid-settle
            assert not task.done(), "second cancel must not lose the queued load"
            assert load_calls == []

            blocker_release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            blocker.result(timeout=5)

            assert load_calls == [True]
            assert holder._model is not None
        finally:
            blocker_release.set()
            small.shutdown(wait=False)

    @pytest.mark.asyncio
    async def test_close_waits_for_inflight_loader_thread(self, isolated_state, monkeypatch):
        """Cancelling the warmup task can't interrupt a loader *thread* —
        ``close()`` must wait for it to settle before tearing components
        down (review finding on #1621: cancel-and-abandon would let the
        load complete against closed components).
        """
        from memtomem.embedding.onnx import OnnxEmbedder

        monkeypatch.setenv("MEMTOMEM_WARMUP__ENABLED", "true")
        monkeypatch.setenv("MEMTOMEM_EMBEDDING__PROVIDER", "onnx")

        entered = threading.Event()
        gate = threading.Event()
        settled: list[bool] = []

        def blocking_get_model(self: OnnxEmbedder) -> object:
            entered.set()
            gate.wait(timeout=10)
            settled.append(True)
            self._model = object()
            return self._model

        monkeypatch.setattr(OnnxEmbedder, "_get_model", blocking_get_model)

        async with lifespan_mod.app_lifespan(MagicMock()) as ctx:
            task = ctx._warmup_task
            assert task is not None
            # Make sure the load thread is actually in flight before exit.
            await asyncio.to_thread(entered.wait, 5)
            # Release the gate shortly after shutdown starts cancelling.
            threading.Timer(0.2, gate.set).start()

        # Lifespan exit ran close(): by the time it returns, the loader
        # thread must have settled — never abandoned mid-load.
        assert settled == [True], "close() returned before the loader thread settled"
        assert task.done()


class TestLoaderConcurrency:
    """The three local loaders serialize their first load (double-checked
    ``_load_lock``) — warmup and the request path share one concurrency
    contract instead of constructing (and downloading) the model twice
    (review finding on #1621).
    """

    N_THREADS = 8

    @staticmethod
    def _hammer(get_model) -> None:
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            for f in [pool.submit(get_model) for _ in range(TestLoaderConcurrency.N_THREADS)]:
                f.result(timeout=10)

    @staticmethod
    def _slow_ctor(calls: list):
        class _Tokenizer:
            def __init__(self) -> None:
                self.truncation = {
                    "max_length": 8192,
                    "stride": 0,
                    "strategy": "longest_first",
                    "direction": "right",
                }

            def enable_truncation(self, *, max_length: int, **kwargs) -> None:
                self.truncation = {**self.truncation, **kwargs, "max_length": max_length}

        class _Ctor:
            def __init__(self, *args, **kwargs) -> None:
                calls.append((args, kwargs))
                self.model = SimpleNamespace(tokenizer=_Tokenizer())
                # Widen the race window so a missing lock surfaces reliably.
                import time

                time.sleep(0.05)

        return _Ctor

    def test_onnx_embedder_constructs_model_once(self, monkeypatch, tmp_path):
        import sys

        import memtomem.embedding.onnx as onnx_mod
        from memtomem.config import EmbeddingConfig

        calls: list = []
        monkeypatch.setitem(
            sys.modules, "fastembed", SimpleNamespace(TextEmbedding=self._slow_ctor(calls))
        )
        monkeypatch.setattr(onnx_mod, "_register_custom_models_if_needed", lambda: None)
        monkeypatch.setattr(onnx_mod, "resolve_embedder_id", lambda m: m)
        monkeypatch.setattr(onnx_mod, "resolve_fastembed_cache_dir", lambda: tmp_path)

        embedder = onnx_mod.OnnxEmbedder(EmbeddingConfig(provider="onnx"))
        self._hammer(embedder._get_model)

        assert len(calls) == 1, f"model constructed {len(calls)}× under concurrency"
        assert embedder._model is not None

    def test_fastembed_reranker_constructs_model_once(self, monkeypatch, tmp_path):
        import sys

        import memtomem.search.reranker.fastembed as fe_mod
        from memtomem.config import RerankConfig

        calls: list = []
        fake_mod = SimpleNamespace(TextCrossEncoder=self._slow_ctor(calls))
        monkeypatch.setitem(sys.modules, "fastembed", SimpleNamespace(rerank=fake_mod))
        monkeypatch.setitem(sys.modules, "fastembed.rerank", fake_mod)
        monkeypatch.setitem(sys.modules, "fastembed.rerank.cross_encoder", fake_mod)
        monkeypatch.setattr(fe_mod, "resolve_fastembed_cache_dir", lambda: tmp_path)

        reranker = fe_mod.FastEmbedReranker(RerankConfig(enabled=True))
        self._hammer(reranker._get_model)

        assert len(calls) == 1, f"model constructed {len(calls)}× under concurrency"

    def test_local_reranker_constructs_model_once(self, monkeypatch):
        import sys

        import memtomem.search.reranker.local as local_mod
        from memtomem.config import RerankConfig

        calls: list = []
        monkeypatch.setitem(
            sys.modules,
            "sentence_transformers",
            SimpleNamespace(CrossEncoder=self._slow_ctor(calls)),
        )

        reranker = local_mod.LocalReranker(RerankConfig(enabled=True, provider="local"))
        self._hammer(reranker._get_model)

        assert len(calls) == 1, f"model constructed {len(calls)}× under concurrency"


class TestWarmupCli:
    @staticmethod
    def _patch_cli_components(monkeypatch, components):
        import memtomem.cli._bootstrap as bootstrap

        @asynccontextmanager
        async def fake_components():
            yield components

        monkeypatch.setattr(bootstrap, "cli_components", fake_components)

    def test_mm_warmup_reports_outcomes(self, monkeypatch):
        from memtomem.cli import cli

        embedder = _FakeLocalModel()
        self._patch_cli_components(monkeypatch, _fake_components(embedder, None))

        result = CliRunner().invoke(cli, ["warmup"])

        assert result.exit_code == 0, result.output
        assert "embedder: loaded (provider=onnx, model=embed-model)" in result.output
        assert "reranker: skipped (provider=fastembed, model=rerank-model)" in result.output
        assert embedder.load_calls == 1

    def test_mm_warmup_surfaces_load_failure_with_hint(self, monkeypatch):
        from memtomem.cli import cli
        from memtomem.errors import EmbeddingError

        class _Broken(_FakeLocalModel):
            def _get_model(self) -> object:
                raise EmbeddingError("fastembed is required")

        self._patch_cli_components(monkeypatch, _fake_components(_Broken(), None))

        result = CliRunner().invoke(cli, ["warmup"])

        assert result.exit_code == 1
        assert "fastembed is required" in result.output
        assert "Hint:" in result.output, "EmbeddingError must carry the next-step hint"
