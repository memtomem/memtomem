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


def _configure_tokenizer_limit(
    model: object, configured_limit: int
) -> tuple[object | None, int | None]:
    """Apply memtomem's safety cap to FastEmbed's tokenizer.

    FastEmbed 0.8 does not expose a public sequence-length argument. Its
    ``TextEmbedding`` wrapper does expose the loaded tokenizer at
    ``model.model.tokenizer``; validate that pinned layout explicitly so a
    future FastEmbed change cannot silently restore bge-m3's 8192-token peak.
    ``configured_limit=0`` is the compatibility escape hatch and leaves the
    provider's tokenizer untouched.
    """

    inner_model = getattr(model, "model", None)
    tokenizer = getattr(inner_model, "tokenizer", None)
    truncation = getattr(tokenizer, "truncation", None)
    enable_truncation = getattr(tokenizer, "enable_truncation", None)

    if configured_limit == 0:
        if isinstance(truncation, dict):
            model_limit = truncation.get("max_length")
            if isinstance(model_limit, int) and not isinstance(model_limit, bool):
                return tokenizer, model_limit
        return tokenizer, None

    model_limit = truncation.get("max_length") if isinstance(truncation, dict) else None
    if (
        tokenizer is None
        or not callable(enable_truncation)
        or not isinstance(model_limit, int)
        or isinstance(model_limit, bool)
        or model_limit <= 0
    ):
        raise EmbeddingError(
            "FastEmbed tokenizer layout is incompatible with "
            "embedding.max_sequence_tokens; refusing unsafe ONNX fallback. "
            "Set embedding.max_sequence_tokens=0 to use the model limit explicitly."
        )

    applied_limit = min(model_limit, configured_limit)
    if applied_limit < model_limit:
        truncation_config = truncation if isinstance(truncation, dict) else {}
        try:
            enable_truncation(
                max_length=applied_limit,
                stride=truncation_config.get("stride", 0),
                strategy=truncation_config.get("strategy", "longest_first"),
                direction=truncation_config.get("direction", "right"),
            )
        except Exception as exc:
            raise EmbeddingError(f"Failed to configure FastEmbed tokenizer limit: {exc}") from exc
    return tokenizer, applied_limit


def _truncated_input_indexes(
    tokenizer: object | None,
    texts: list[str],
    max_sequence_tokens: int | None = None,
) -> list[int]:
    """Return zero-based indexes truncated by the configured tokenizer."""

    if tokenizer is None:
        return []
    encode = getattr(tokenizer, "encode", None)
    if callable(encode):
        truncated: list[int] = []
        try:
            # Encode one input at a time so warning detection does not retain
            # every token/overflow object for a whole file at once. FastEmbed
            # still performs its own batched tokenization for inference.
            for index, text in enumerate(texts):
                # Byte-level tokenizers cannot emit more tokens than ASCII
                # bytes plus special tokens. Skip exact preflight for the
                # common short-ASCII path, but keep it for non-ASCII and long
                # inputs where a chars/token heuristic could miss truncation.
                if (
                    max_sequence_tokens is not None
                    and text.isascii()
                    and len(text) + 8 <= max_sequence_tokens
                ):
                    continue
                if bool(getattr(encode(text), "overflowing", ())):
                    truncated.append(index)
        except Exception as exc:
            raise EmbeddingError(f"FastEmbed tokenizer preflight failed: {exc}") from exc
        return truncated

    # Compatibility fallback for tokenizer-like test doubles and older
    # tokenizers APIs. The pinned FastEmbed path above exposes ``encode``.
    encode_batch = getattr(tokenizer, "encode_batch", None)
    if not callable(encode_batch):
        return []
    try:
        encodings = encode_batch(texts)
    except Exception as exc:
        raise EmbeddingError(f"FastEmbed tokenizer preflight failed: {exc}") from exc
    return [
        index
        for index, encoding in enumerate(encodings)
        if bool(getattr(encoding, "overflowing", ()))
    ]


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
    # Optional duck-typed capability consumed by IndexEngine. Keeping it out
    # of EmbeddingProvider preserves structural compatibility for existing
    # fakes and out-of-tree providers while allowing path/index-only warning
    # context for truncation events.
    supports_input_context = True

    def __init__(self, config: EmbeddingConfig) -> None:
        self._config = config
        # Runtime-mutable and deliberately detached from ``_config``. Config
        # writers mutate/replace their candidate object before persistence;
        # inference must not observe a rejected candidate through a shared
        # reference. The setter is called only after a successful update.
        self._onnx_batch_size = config.onnx_batch_size
        self._model: object | None = None  # fastembed.TextEmbedding
        self._tokenizer: object | None = None
        self._active_max_sequence_tokens: int | None = None
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
                model = TextEmbedding(
                    model_name=model_id, threads=threads, cache_dir=str(cache_dir)
                )
                tokenizer, active_limit = _configure_tokenizer_limit(
                    model, self._config.max_sequence_tokens
                )
                self._tokenizer = tokenizer
                self._active_max_sequence_tokens = active_limit
                self._model = model
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

    @property
    def onnx_batch_size(self) -> int:
        return self._onnx_batch_size

    def set_onnx_batch_size(self, value: int) -> None:
        """Atomically publish a validated runtime inference batch size."""
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 256:
            raise ValueError("onnx_batch_size must be an integer between 1 and 256")
        self._onnx_batch_size = value

    def _embed_sync(
        self,
        texts: list[str],
        on_progress: Callable[[int, int], None] | None = None,
        source_path: str | None = None,
        chunk_indices: list[int] | None = None,
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
        # Snapshot once so a concurrent config update applies to the next
        # inference call, never halfway through this generator.
        batch_size = self._onnx_batch_size
        truncated = _truncated_input_indexes(
            self._tokenizer, texts, self._active_max_sequence_tokens
        )
        if truncated:
            display_indices = chunk_indices or [index + 1 for index in range(len(texts))]
            labels = [display_indices[index] for index in truncated]
            shown_labels = labels[:20]
            omitted = len(labels) - len(shown_labels)
            label_summary = f"{shown_labels}"
            if omitted:
                label_summary += f" (+{omitted} more)"
            logger.warning(
                "ONNX embedding truncated %d input(s) to %s tokens (source=%s, %s=%s)",
                len(truncated),
                self._active_max_sequence_tokens or "model limit",
                source_path or "<direct>",
                "chunks" if source_path is not None else "inputs",
                label_summary,
            )

        # One FastEmbed generator call retains the low-overhead streaming
        # path, while its public ``batch_size`` argument bounds each ORT
        # session.run. This avoids the former implicit FastEmbed default 256.
        if on_progress is None:
            return [vec.tolist() for vec in model.embed(texts, batch_size=batch_size)]
        total = len(texts)
        out: list[list[float]] = []
        for vec in model.embed(texts, batch_size=batch_size):
            out.append(vec.tolist())
            on_progress(len(out), total)
        return out

    async def embed_texts(
        self,
        texts: Sequence[str],
        *,
        on_progress: Callable[[int, int], None] | None = None,
        source_path: str | None = None,
        chunk_indices: Sequence[int] | None = None,
    ) -> list[list[float]]:
        if not texts:
            return []
        text_list = list(texts)
        total = len(text_list)
        index_list = list(chunk_indices) if chunk_indices is not None else None
        if index_list is not None and len(index_list) != total:
            raise EmbeddingError(
                f"chunk_indices has {len(index_list)} entries for {total} embedding inputs"
            )

        loop = asyncio.get_running_loop()

        if on_progress is None:
            # Fast path — no callback plumbing, no cross-thread hops.
            try:
                if source_path is None and index_list is None:
                    return await loop.run_in_executor(
                        self._infer_executor, self._embed_sync, text_list, None
                    )
                return await loop.run_in_executor(
                    self._infer_executor,
                    self._embed_sync,
                    text_list,
                    None,
                    source_path,
                    index_list,
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
            if source_path is None and index_list is None:
                return await loop.run_in_executor(
                    self._infer_executor, self._embed_sync, text_list, _thread_cb
                )
            return await loop.run_in_executor(
                self._infer_executor,
                self._embed_sync,
                text_list,
                _thread_cb,
                source_path,
                index_list,
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
            self._tokenizer = None
            self._active_max_sequence_tokens = None
        self._infer_executor.shutdown(wait=True, cancel_futures=True)
        gc.collect()
