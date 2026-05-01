"""Embedding provider protocol."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, Sequence


class EmbeddingProvider(Protocol):
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
