"""Local cross-encoder reranker using sentence-transformers."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

from memtomem._settlement import settle_shielded
from memtomem.embedding.hub_locks import prepare_hub_locks
from memtomem.embedding.hub_telemetry import hub_telemetry_off

if TYPE_CHECKING:
    from memtomem.config import RerankConfig
    from memtomem.models import SearchResult

logger = logging.getLogger(__name__)


def _sentence_transformers_cache_dirs() -> list[str]:
    """Every directory a ``CrossEncoder(model)`` download may go to, unresolved.

    Which one depends on the sentence-transformers release. 6.3.0 reads
    ``SENTENCE_TRANSFORMERS_HOME`` when no ``cache_folder`` is passed (memtomem
    passes none) and keeps an empty value, which the Hub resolves to the current
    directory. In 5.2.2, 4.1.0 and 3.4.1 ``CrossEncoder`` does not read the
    variable and downloads into the Hub's default cache. Both are returned rather than the
    release being detected: probing a directory the load does not use costs at
    most the shared store, and probing too few can leave a download that never
    ends.
    """
    dirs = []
    home = os.environ.get("SENTENCE_TRANSFORMERS_HOME")
    if home is not None:
        dirs.append(home)
    try:
        from huggingface_hub import constants
    except ImportError:
        return dirs
    dirs.append(constants.HF_HUB_CACHE)
    return dirs


def _prepare_sentence_transformers_cache(model: str) -> None:
    """Run the #3261 lock guard on each cache a cold ``CrossEncoder`` load may download into."""
    # A local model directory is loaded without the Hub, as sentence-transformers decides it.
    if os.path.isdir(model):
        return
    for raw in _sentence_transformers_cache_dirs():
        # Resolved as the Hub resolves ``cache_dir``: ``prepare_hub_locks`` probes
        # each spelling once, so a relative one would not be probed again after a
        # ``chdir``.
        try:
            cache_dir = Path(raw).expanduser().resolve()
        except (OSError, RuntimeError) as exc:
            # No HOME to expand, for one: the load decides what that means.
            logger.debug("Skipping the hub lock probe of %r for %s: %s", raw, model, exc)
            continue
        # The probe needs the directory, and a first install has none yet; the Hub
        # creates it on download anyway. If it cannot be made, the probe fails and
        # the shared store is turned off, the side that cannot hang.
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        prepare_hub_locks(cache_dir)


class LocalReranker:
    """Cross-encoder reranking using a local sentence-transformers model."""

    def __init__(self, config: RerankConfig):
        self._config = config
        self._model = None
        self._closed = False
        # Serializes the first load — same contract as ``OnnxEmbedder``:
        # the search path and the opt-in warmup task (#1621) can race into
        # ``_get_model`` from different threads.
        self._load_lock = threading.Lock()
        # Dedicated single-worker executor, the ``OnnxEmbedder`` shape
        # (#1783): model load and ``predict`` are seconds-long synchronous
        # torch calls that previously ran directly on the event loop,
        # freezing every other coroutine for the duration. A single worker
        # is also the hard cap on concurrent inference — concurrent searches
        # queue here instead of amplifying memory with parallel runs.
        self._infer_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="local-rerank")
        # A timed-out inference the pipeline abandoned but the worker thread
        # is still running (native code cannot be interrupted). While it is
        # pending, later reranks fail fast instead of queuing behind the
        # wedged worker and each paying the full timeout again.
        self._abandoned: Future | None = None

    def _get_model(self):
        # A closed instance must not resurrect: reloading the released model
        # here is silent expensive work on an instance nobody owns (#1778).
        # Cached reads go through a local snapshot so a concurrent close()
        # nulling ``_model`` between the check and the return cannot hand
        # the caller ``None``.
        if self._closed:
            raise RuntimeError("LocalReranker is closed")
        model = self._model
        if model is None:
            with self._load_lock:
                # Re-check under the lock: a warmup/readiness thread that
                # passed the guard above can lose the race to a concurrent
                # close() — loading here would resurrect the model onto the
                # closed instance (#1778).
                if self._closed:
                    raise RuntimeError("LocalReranker is closed")
                model = self._model
                if model is None:
                    from sentence_transformers import CrossEncoder

                    # A cache miss downloads from the Hub inside this constructor (#2556),
                    # which can wait forever where the cache cannot flock (#3266).
                    _prepare_sentence_transformers_cache(self._config.model)
                    with hub_telemetry_off():
                        model = CrossEncoder(self._config.model)
                    # Publish-then-verify: close() does not take this lock,
                    # so it can land while the construction above is in
                    # flight. Publishing first and re-checking makes every
                    # interleaving of close()'s (flag, _model=None) writes
                    # end with the closed instance holding no model (#1778).
                    self._model = model
                    if self._closed:
                        self._model = None
                        raise RuntimeError("LocalReranker is closed")
                    logger.info("Loaded local reranker: %s", self._config.model)
        return model

    def _predict_sync(self, pairs: list[tuple[str, str]]):
        """Load-and-score synchronously — runs on the dedicated worker."""
        return self._get_model().predict(pairs)

    async def rerank(
        self, query: str, results: list[SearchResult], top_k: int
    ) -> list[SearchResult]:
        from memtomem.models import SearchResult as SR

        if not results:
            return results
        # Deterministic post-close refusal (#1778) — the executor is already
        # shut down, and its "after shutdown" RuntimeError would otherwise be
        # swallowed into the degrade-to-original-order arm below.
        if self._closed:
            raise RuntimeError("LocalReranker is closed")
        if self._abandoned is not None:
            if not self._abandoned.done():
                logger.warning(
                    "Local rerank busy (a timed-out inference is still running), "
                    "returning original order"
                )
                return results[:top_k]
            self._abandoned = None

        pairs = [(query, r.chunk.content) for r in results]

        # ``submit`` + ``wrap_future`` rather than ``run_in_executor``: when
        # the awaiting task is cancelled, the asyncio wrapper is marked
        # cancelled even though a *running* worker cannot be interrupted —
        # only the concurrent future's ``done()`` stays truthful about the
        # thread, and that is what the busy check above reads.
        inference = self._infer_executor.submit(self._predict_sync, pairs)
        try:
            scores = await asyncio.wrap_future(inference)
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
            # Load errors previously propagated, but the pipeline's own
            # fallback for them is identical to this one (original order +
            # a warning), so converging here loses nothing callers observed.
            logger.warning("Local rerank failed, returning original order: %s", exc)
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
        await settle_shielded(future, what="local reranker teardown")

    def _close_sync(self) -> None:
        # No ``_load_lock`` here: close() landing mid-construction must
        # return promptly (#1780) — publish-then-verify in ``_get_model``
        # already guarantees a closed instance ends with no model published.
        self._closed = True
        self._model = None
        # cancel_futures drops queued-but-unstarted predictions; wait=True
        # joins the one that may still be running, so close() completing
        # means no worker still owns the model.
        self._infer_executor.shutdown(wait=True, cancel_futures=True)
