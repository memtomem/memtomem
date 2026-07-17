"""Embedding provider protocol."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, Sequence


class EmbeddingProvider(Protocol):
    # Optional capability (deliberately NOT a required Protocol member, so
    # existing duck-typed fakes and out-of-tree providers stay structurally
    # conformant): providers MAY expose ``preferred_concurrency: int`` — the
    # maximum number of concurrent ``embed_texts`` calls the provider wants
    # across files during a bulk index run. The index engine clamps the value
    # to its file-level concurrency cap and falls back to that cap when the
    # attribute is absent or not a real ``int`` (see
    # ``indexing.engine._resolve_embed_limit``). Reading the attribute must be
    # side-effect-free — in particular it must not trigger a model load.
    # Local CPU providers should advertise a low value (concurrent inference
    # multiplies peak activation memory without adding throughput, #1783);
    # remote latency-bound providers can match the file cap.

    @property
    def dimension(self) -> int: ...

    @property
    def model_name(self) -> str: ...

    async def embed_texts(
        self,
        texts: Sequence[str],
        *,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[list[float]]:
        # ``on_progress(done, total)`` fires after each natural unit of work
        # (one batch for OpenAI/Ollama/ONNX). ``done`` is a monotonically
        # non-decreasing count of texts whose embeddings are now available;
        # it is NOT a positional index — concurrent batches (OpenAI/Ollama
        # via ``asyncio.gather``) complete in arbitrary order. Best-effort:
        # exceptions raised by the callback are caught and logged at debug.
        ...

    async def embed_query(self, query: str) -> list[float]: ...
    async def close(self) -> None: ...
