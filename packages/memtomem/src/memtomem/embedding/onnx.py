"""Local ONNX embedding provider using fastembed."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Sequence

from memtomem.config import EmbeddingConfig
from memtomem.embedding.aliases import resolve_embedder_id
from memtomem.embedding.fastembed_cache import resolve_fastembed_cache_dir
from memtomem.errors import EmbeddingError

logger = logging.getLogger(__name__)


def _register_custom_models_if_needed() -> None:
    """Register models that fastembed >=0.4 dropped from its built-in catalog.

    fastembed 0.8.0's ``TextEmbedding`` no longer ships ``BAAI/bge-m3`` (the
    model type split across dedicated classes none of which currently host
    it). Re-register it from the official HF ONNX export so existing installs
    keep working without changing the user-facing model name.
    """
    from fastembed import TextEmbedding  # type: ignore[import-untyped]
    from fastembed.common.model_description import (  # type: ignore[import-untyped]
        ModelSource,
        PoolingType,
    )

    registered = {m.get("model") for m in TextEmbedding.list_supported_models()}
    if "BAAI/bge-m3" not in registered:
        TextEmbedding.add_custom_model(
            model="BAAI/bge-m3",
            pooling=PoolingType.CLS,
            normalization=True,
            sources=ModelSource(hf="BAAI/bge-m3"),
            dim=1024,
            model_file="onnx/model.onnx",
            additional_files=["onnx/model.onnx_data"],
            size_in_gb=2.3,
        )


class OnnxEmbedder:
    """Embedding provider backed by fastembed (ONNX Runtime).

    Runs entirely on the CPU — no external server or GPU required.
    Install the optional dependency: ``pip install memtomem[onnx]``
    """

    # ORT ``session.run`` is CPU-bound: concurrent runs time-slice the same
    # cores (the intra-op pool, ``EmbeddingConfig.threads``, is the real
    # throughput limit) while each in-flight run allocates its own activation
    # memory — 8 concurrent runs drove RSS to ~31 GB on a 1.2 MB corpus
    # (#1783). Advertise 1 so the index engine serializes per-file embedding;
    # the dedicated single-worker executor below is the matching hard
    # guarantee. Same don't-saturate-the-machine rationale as the
    # ``threads=4`` default (#640).
    preferred_concurrency = 1

    def __init__(self, config: EmbeddingConfig) -> None:
        self._config = config
        self._model: object | None = None  # fastembed.TextEmbedding
        # Observability flags read by ``GET /api/system/model-readiness``.
        # Plain attribute reads/writes — bool/Optional[str] assignment is
        # atomic under CPython, and the readiness endpoint is allowed to
        # observe transient states without taking a lock.
        self._loading: bool = False
        self._load_error: str | None = None
        # Serializes the first load: the MCP request path and the opt-in
        # warmup task (#1621) can race into ``_get_model`` from different
        # threads — without the lock both would construct (and download)
        # the model. ``_closed`` is set under this same lock by ``close()``
        # so that *every* load path (inference on ``_infer_executor``, warmup
        # on the default executor) observes teardown and refuses to resurrect
        # the model after close — see ``_get_model`` / ``close``.
        self._load_lock = threading.Lock()
        self._closed = False
        # Dedicated single-worker executor: the hard cap on concurrent ONNX
        # inference, matching ``preferred_concurrency`` above. The engine's
        # asyncio semaphore is only the normal-path scheduler — cancelling a
        # coroutine awaiting the inference frees the async slot immediately
        # but cannot stop an already-running ``session.run``, so a follow-up
        # run could otherwise start a second inference alongside the
        # abandoned one and recreate the memory amplification (#1783).
        #
        # Why a dedicated executor rather than ``asyncio.to_thread`` +
        # a threading semaphore: with ``max_workers=1`` the executor *is* the
        # serialization, and it is cancellation-aware in the way the shared
        # default executor is not. A queued inference whose awaiting task is
        # cancelled has its future cancelled *before it starts* and never
        # runs; only the one already-executing run continues (it holds the
        # lone worker), so the cap holds without cancelled work piling up as
        # blocked threads in the process-wide default pool.
        #
        # Accepted trade-off: ``embed_query`` (search-time) also flows
        # through this executor, so a query issued mid-reindex waits for the
        # in-flight file's inference instead of running as an extra
        # concurrent ``session.run`` — that extra run was part of the
        # problem, and the wait is bounded by one file's embed.
        self._infer_executor = ThreadPoolExecutor(
            max_workers=self.preferred_concurrency, thread_name_prefix="onnx-embed"
        )

    def _get_model(self) -> object:
        """Lazily initialise the fastembed model (downloads on first use).

        Double-checked lock so concurrent first-callers (request path vs
        warmup) share a single construction.
        """
        if self._model is not None:
            return self._model
        with self._load_lock:
            if self._model is not None:
                return self._model
            if self._closed:
                # close() ran (or is running) — refuse to construct a model
                # that would outlive teardown. Checked under ``_load_lock`` so
                # it serializes with close()'s own clear: whichever wins, the
                # loser sees a consistent state and no orphaned ORT session
                # survives close (#1792 review, #206).
                raise EmbeddingError("ONNX embedder is closed")
            try:
                from fastembed import TextEmbedding  # type: ignore[import-untyped]
            except ImportError as exc:
                raise EmbeddingError(
                    "fastembed is required for the ONNX embedding provider. "
                    "Install it with: pip install memtomem[onnx]"
                ) from exc

            _register_custom_models_if_needed()
            model_id = resolve_embedder_id(self._config.model)
            # threads=0 → leave ORT default (all physical cores); threads>0 caps
            # the intra-op pool so seeding doesn't saturate the machine.
            threads = self._config.threads or None
            cache_dir = resolve_fastembed_cache_dir()
            logger.info(
                "Loading ONNX embedding model %s (threads=%s, cache_dir=%s) …",
                model_id,
                threads if threads is not None else "ORT default",
                cache_dir,
            )
            self._loading = True
            self._load_error = None
            try:
                self._model = TextEmbedding(
                    model_name=model_id, threads=threads, cache_dir=str(cache_dir)
                )
            except Exception as exc:
                self._load_error = str(exc)
                raise
            finally:
                self._loading = False
            return self._model

    @property
    def dimension(self) -> int:
        return self._config.dimension

    @property
    def model_name(self) -> str:
        return self._config.model

    def _embed_sync(
        self,
        texts: list[str],
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[list[float]]:
        """Run inference synchronously — submitted to ``_infer_executor``.

        The single-worker executor serializes these calls, so there is no
        in-body lock: concurrency is bounded by the pool size, not by this
        method. When ``on_progress`` is provided, iterate fastembed's
        generator and fire after each yielded vector. The callback runs on
        the worker thread; callers on an asyncio loop wrap it with
        ``loop.call_soon_threadsafe`` (see ``embed_texts``).
        """
        model = self._get_model()
        # Single ``model.embed()`` call — fastembed batches internally
        # (default batch_size=256) so a 250-text input becomes ONE
        # ORT session.run. Earlier we did Python-side chunking
        # (``for batch in batches: model.embed(batch)``); benchmark
        # showed +20% wall-clock regression vs single call because
        # each ``model.embed()`` invocation pays per-call ORT setup
        # cost. Stream-iterating the default-batched call recovers
        # that to +2.3% while still surfacing per-yield progress.
        if on_progress is None:
            return [vec.tolist() for vec in model.embed(texts)]
        total = len(texts)
        out: list[list[float]] = []
        for vec in model.embed(texts):
            out.append(vec.tolist())
            on_progress(len(out), total)
        return out

    async def embed_texts(
        self,
        texts: Sequence[str],
        *,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[list[float]]:
        if not texts:
            return []
        text_list = list(texts)
        total = len(text_list)

        loop = asyncio.get_running_loop()

        if on_progress is None:
            # Fast path — no callback plumbing, no cross-thread hops.
            try:
                return await loop.run_in_executor(
                    self._infer_executor, self._embed_sync, text_list, None
                )
            except EmbeddingError:
                raise
            except Exception as exc:
                raise EmbeddingError(f"ONNX embedding failed: {exc}") from exc

        # ``on_progress`` was provided. ``_embed_sync`` runs in a worker
        # thread but ``on_progress`` (e.g. ``queue.put_nowait`` into the
        # SSE stream) is event-loop-bound and not thread-safe. Wrap with
        # ``call_soon_threadsafe`` and throttle to at most ~20 ticks per
        # file so a 1000-text input doesn't fire 1000 cross-thread hops.
        last_reported = [0]
        step = max(1, total // 20)
        progress_warned = [False]

        def _safe_on_progress(done: int, t: int) -> None:
            try:
                on_progress(done, t)
            except Exception:
                if not progress_warned[0]:
                    progress_warned[0] = True
                    logger.debug(
                        "on_progress raised; further failures silenced",
                        exc_info=True,
                    )

        def _thread_cb(done: int, t: int) -> None:
            # Throttle thread→loop hops; always emit the final tick so
            # the SSE consumer's "(N/N)" final-render contract holds.
            if done - last_reported[0] < step and done != t:
                return
            last_reported[0] = done
            try:
                loop.call_soon_threadsafe(_safe_on_progress, done, t)
            except RuntimeError:
                # Event loop is closed (shutdown / cancel). Drop the
                # tick — embedding work itself continues unaffected.
                pass

        try:
            return await loop.run_in_executor(
                self._infer_executor, self._embed_sync, text_list, _thread_cb
            )
        except EmbeddingError:
            raise
        except Exception as exc:
            raise EmbeddingError(f"ONNX embedding failed: {exc}") from exc

    async def embed_query(self, query: str) -> list[float]:
        if not query or not query.strip():
            raise EmbeddingError("Query text cannot be empty")
        embeddings = await self.embed_texts([query])
        if not embeddings:
            raise EmbeddingError("No embeddings returned for query")
        return embeddings[0]

    async def close(self) -> None:
        # Teardown runs entirely in ``_close_sync`` on a worker thread, off
        # the event loop, so the blocking ``_load_lock`` acquire / executor
        # drain never stall the loop. Every await on the teardown future is
        # ``shield``ed so cancellation of the awaiting task — first or
        # repeated — can never propagate into the future and cancel a
        # still-queued ``_close_sync`` before it starts (which would leave
        # the model and executor alive; an already-running worker can't be
        # interrupted anyway). On cancellation we keep settling until the
        # future is done, then propagate. ``server/warmup.py`` uses the same
        # repeated-shield settlement for model loads; keep both paths aligned
        # so neither loses queued executor work (#1803).
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(None, self._close_sync)
        cancelled = False
        while True:
            try:
                await asyncio.shield(future)
                break
            except asyncio.CancelledError:
                cancelled = True
                if future.done():
                    break
        if cancelled:
            raise asyncio.CancelledError

    def _close_sync(self) -> None:
        # Latch closed and drop the model under ``_load_lock`` — the same lock
        # every ``_get_model`` load path takes. This is what makes teardown
        # complete for *all* loaders, not just inference on ``_infer_executor``:
        #   * A loader mid-construction (inference or warmup on the default
        #     executor) holds the lock, so this blocks until it has assigned
        #     ``_model``; we then null it — its assignment strictly
        #     happens-before our clear, no resurrection.
        #   * A loader that arrives after sees ``_closed`` and refuses.
        # Then drain the inference executor: cancel_futures drops queued work,
        # wait=True joins the one running inference. Finally force-collect —
        # the ORT InferenceSession holds an mmap + thread-local arenas that on
        # Windows can outlive ``_model = None`` long enough to fail pytest's
        # tmp_path rmtree with WinError 183 (#206).
        import gc

        with self._load_lock:
            self._closed = True
            self._model = None
        self._infer_executor.shutdown(wait=True, cancel_futures=True)
        gc.collect()
