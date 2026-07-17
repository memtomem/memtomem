"""No-op embedding provider for BM25-only mode."""

from __future__ import annotations

from collections.abc import Callable
from typing import Sequence


class NoopEmbedder:
    """Embedding provider that returns empty vectors.

    Used when no embedding backend is configured (``provider="none"``).
    The search pipeline falls back to BM25-only keyword search, and the
    index engine skips vector storage entirely.
    """

    # Inert — the engine skips embedding when ``dimension == 0`` — but kept
    # equal to the engine's file-level cap so the hint contract (base.py)
    # holds for every built-in provider.
    preferred_concurrency = 8

    @property
    def dimension(self) -> int:
        return 0

    @property
    def model_name(self) -> str:
        return "none"

    async def embed_texts(
        self,
        texts: Sequence[str],
        *,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[list[float]]:
        # ``on_progress`` is accepted for ``EmbeddingProvider`` Protocol
        # conformance but not invoked — Noop is instantaneous and the
        # engine skips embedding entirely when ``dimension == 0``.
        return [[] for _ in texts]

    async def embed_query(self, query: str) -> list[float]:
        return []

    async def close(self) -> None:
        pass
