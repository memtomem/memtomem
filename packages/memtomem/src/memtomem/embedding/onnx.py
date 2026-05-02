"""Local ONNX embedding provider using fastembed."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
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

    def __init__(self, config: EmbeddingConfig) -> None:
        self._config = config
        self._model: object | None = None  # fastembed.TextEmbedding
        # Observability flags read by ``GET /api/system/model-readiness``.
        # Plain attribute reads/writes — bool/Optional[str] assignment is
        # atomic under CPython, and the readiness endpoint is allowed to
        # observe transient states without taking a lock.
        self._loading: bool = False
        self._load_error: str | None = None

    def _get_model(self) -> object:
        """Lazily initialise the fastembed model (downloads on first use)."""
        if self._model is not None:
            return self._model
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
        """Run inference synchronously — called inside ``to_thread``.

        When ``on_progress`` is provided, iterate fastembed's generator
        and fire after each yielded vector. The callback is called from
        the worker thread; callers running on an asyncio loop must wrap
        with ``loop.call_soon_threadsafe`` (see ``embed_texts``).
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

        if on_progress is None:
            # Fast path — no callback plumbing, no cross-thread hops.
            try:
                return await asyncio.to_thread(self._embed_sync, text_list, None)
            except EmbeddingError:
                raise
            except Exception as exc:
                raise EmbeddingError(f"ONNX embedding failed: {exc}") from exc

        # ``on_progress`` was provided. ``_embed_sync`` runs in a worker
        # thread but ``on_progress`` (e.g. ``queue.put_nowait`` into the
        # SSE stream) is event-loop-bound and not thread-safe. Wrap with
        # ``call_soon_threadsafe`` and throttle to at most ~20 ticks per
        # file so a 1000-text input doesn't fire 1000 cross-thread hops.
        loop = asyncio.get_running_loop()
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
            return await asyncio.to_thread(self._embed_sync, text_list, _thread_cb)
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
        self._model = None
