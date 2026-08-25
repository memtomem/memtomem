"""FastEmbed cross-encoder reranker — local ONNX, no external service."""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import TYPE_CHECKING

from memtomem._settlement import settle_shielded
from memtomem.embedding.fastembed_cache import resolve_fastembed_cache_dir

if TYPE_CHECKING:
    from memtomem.config import RerankConfig
    from memtomem.models import SearchResult

logger = logging.getLogger(__name__)


class FastEmbedReranker:
    """Cross-encoder reranking via ``fastembed.rerank.cross_encoder.TextCrossEncoder``.

    Runs on the CPU via ONNX Runtime — no external server and no PyTorch
    dependency. Reuses the ``memtomem[onnx]`` extra so enabling this provider
    adds no new packages. The model is downloaded on first use and cached in
    the path returned by ``resolve_fastembed_cache_dir()`` (default
    ``~/.memtomem/cache/fastembed``).
    """

    def __init__(self, config: RerankConfig) -> None:
        self._config = config
        self._model: object | None = None
        self._closed = False
        # Observability flags read by ``GET /api/system/model-readiness``.
        # Match ``OnnxEmbedder`` so the endpoint can introspect both via a
        # single contract without each provider having a bespoke surface.
        self._loading: bool = False
        self._load_error: str | None = None
        # Serializes the first load — same contract as ``OnnxEmbedder``:
        # the search path and the opt-in warmup task (#1621) can race into
        # ``_get_model`` from different threads.
        self._load_lock = threading.Lock()
        # Dedicated single-worker executor, the ``OnnxEmbedder`` shape
        # (#1783): the shared default pool would let N concurrent searches
        # run N simultaneous ORT cross-encoder inferences — the exact memory
        # amplification the embedder was hardened against. One worker is the
        # hard cap; excess reranks queue FIFO, and a queued rerank whose
        # awaiting task is cancelled never starts.
        self._infer_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="fastembed-rerank"
        )
        # A timed-out inference the pipeline abandoned but the worker thread
        # is still running (native code cannot be interrupted). While it is
        # pending, later reranks fail fast instead of queuing behind the
        # wedged worker and each paying the full timeout again.
        self._abandoned: Future | None = None

    def _get_model(self) -> object:
        """Lazily construct the ``TextCrossEncoder`` — downloads on first use.

        Double-checked lock so concurrent first-callers (search path vs
        warmup) share a single construction.
        """
        # A closed instance must not resurrect: re-downloading/re-initializing
        # the released ONNX model here is silent expensive work on an
        # instance nobody owns (#1778). Cached reads go through a local
        # snapshot so a concurrent close() nulling ``_model`` between the
        # check and the return cannot hand the caller ``None``.
        if self._closed:
            raise RuntimeError("FastEmbedReranker is closed")
        model = self._model
        if model is not None:
            return model
        with self._load_lock:
            # Re-check under the lock: a warmup/readiness thread that passed
            # the guard above can lose the race to a concurrent close() on
            # the event loop — loading here would resurrect the model onto
            # the closed instance (#1778).
            if self._closed:
                raise RuntimeError("FastEmbedReranker is closed")
            model = self._model
            if model is not None:
                return model
            try:
                from fastembed.rerank.cross_encoder import (  # type: ignore[import-untyped]
                    TextCrossEncoder,
                )
            except ImportError as exc:
                raise ImportError(
                    "fastembed is required for the fastembed reranker. "
                    "Install it with: pip install memtomem[onnx]"
                ) from exc

            cache_dir = resolve_fastembed_cache_dir()
            logger.info(
                "Loading fastembed reranker %s (cache_dir=%s) …",
                self._config.model,
                cache_dir,
            )
            self._loading = True
            self._load_error = None
            try:
                model = TextCrossEncoder(model_name=self._config.model, cache_dir=str(cache_dir))
            except ValueError as exc:
                supported = [m.get("model", "") for m in TextCrossEncoder.list_supported_models()]
                self._load_error = str(exc)
                raise ValueError(
                    f"fastembed reranker model {self._config.model!r} is not supported. "
                    f"Built-in options: {', '.join(sorted(s for s in supported if s))}. "
                    "For Korean/Chinese/Japanese try "
                    "'jinaai/jina-reranker-v2-base-multilingual' (1.1 GB); for lightweight "
                    "English 'Xenova/ms-marco-MiniLM-L-6-v2' (80 MB). Custom ONNX exports "
                    "must be registered via TextCrossEncoder.add_custom_model() before the "
                    "reranker is invoked."
                ) from exc
            except Exception as exc:
                self._load_error = str(exc)
                raise
            finally:
                self._loading = False
            # Publish-then-verify: close() does not take this lock, so it can
            # land while the (seconds-long) construction above is in flight.
            # Publishing first and re-checking makes every interleaving of
            # close()'s (flag, _model=None) writes end with the closed
            # instance holding no model (#1778).
            self._model = model
            if self._closed:
                self._model = None
                raise RuntimeError("FastEmbedReranker is closed")
            return model

    def _rerank_sync(self, query: str, documents: list[str]) -> list[float]:
        """Run inference synchronously — runs on the dedicated worker."""
        model = self._get_model()
        # ``rerank`` returns an iterable of floats; materialize inside the
        # thread so the caller doesn't block on lazy evaluation.
        return [float(s) for s in model.rerank(query, documents)]  # type: ignore[attr-defined]

    async def rerank(
        self, query: str, results: list[SearchResult], top_k: int
    ) -> list[SearchResult]:
        from memtomem.models import SearchResult as SR

        if not results:
            return results

        if self._abandoned is not None:
            if not self._abandoned.done():
                logger.warning(
                    "FastEmbed rerank busy (a timed-out inference is still running), "
                    "returning original order"
                )
                return results[:top_k]
            self._abandoned = None

        documents = [r.chunk.content for r in results]
        # ``submit`` + ``wrap_future`` rather than ``run_in_executor``: when
        # the awaiting task is cancelled, the asyncio wrapper is marked
        # cancelled even though a *running* worker cannot be interrupted —
        # only the concurrent future's ``done()`` stays truthful about the
        # thread, and that is what the busy check above reads.
        try:
            # ``submit`` inside the try: post-close it raises synchronously
            # ("cannot schedule new futures after shutdown"), and that must
            # flow into the same degrade arm the closed ``_get_model`` used
            # to reach — the documented post-close contract of this provider.
            inference = self._infer_executor.submit(self._rerank_sync, query, documents)
            scores = await asyncio.wrap_future(inference)
        except (ImportError, ValueError):
            # Setup/config errors carry actionable hints — surface, don't hide.
            raise
        except asyncio.CancelledError:
            # The pipeline's wait_for timeout cancels this await, but a
            # running native inference cannot be interrupted — remember its
            # future so later reranks fail fast until the worker frees up.
            # A still-queued inference is genuinely cancelled (never runs)
            # and needs no tracking.
            if not inference.done():
                self._abandoned = inference
            raise
        except Exception as exc:
            logger.warning("FastEmbed rerank failed, returning original order: %s", exc)
            return results[:top_k]

        scored = sorted(zip(scores, results), key=lambda x: x[0], reverse=True)
        return [
            SR(chunk=r.chunk, score=float(s), rank=i + 1, source="reranked")
            for i, (s, r) in enumerate(scored[:top_k])
        ]

    async def close(self) -> None:
        # Same teardown shape as ``OnnxEmbedder.close``: latch on the loop
        # thread, then run the blocking drain on a worker so the executor
        # join (which waits for a still-running inference) never stalls the
        # loop, with every await shielded so cancellation cannot skip it.
        loop = asyncio.get_running_loop()
        self._closed = True
        future = loop.run_in_executor(None, self._close_sync)
        await settle_shielded(future, what="fastembed reranker teardown")

    def _close_sync(self) -> None:
        # Force-collect at the end so the underlying ORT InferenceSession
        # releases its mmap and thread-local arenas before pytest cleans up
        # tmp_path on Windows. See #206.
        import gc

        # No ``_load_lock`` here: close() landing mid-construction must
        # return promptly (#1780) — publish-then-verify in ``_get_model``
        # already guarantees a closed instance ends with no model published.
        self._closed = True
        self._model = None
        # cancel_futures drops queued-but-unstarted inferences; wait=True
        # joins the one that may still be running, so close() completing
        # means no worker still owns the model/session.
        self._infer_executor.shutdown(wait=True, cancel_futures=True)
        gc.collect()
