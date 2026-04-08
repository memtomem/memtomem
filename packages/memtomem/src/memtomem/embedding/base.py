"""Embedding provider protocol."""

from __future__ import annotations

from typing import Protocol, Sequence


class EmbeddingProvider(Protocol):
    @property
    def dimension(self) -> int: ...

    @property
    def model_name(self) -> str: ...

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]: ...
    async def embed_query(self, query: str) -> list[float]: ...
    async def close(self) -> None: ...
