"""Reranker factory."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from memtomem.config import RerankConfig
    from memtomem.search.reranker.base import Reranker


def create_reranker(config: RerankConfig) -> Reranker | None:
    """Create a reranker based on config. Returns None if disabled."""
    if not config.enabled:
        return None

    provider = config.provider.lower()

    if provider == "cohere":
        from memtomem.search.reranker.cohere import CohereReranker

        return CohereReranker(config)

    if provider == "local":
        from memtomem.search.reranker.local import LocalReranker

        return LocalReranker(config)

    raise ValueError(f"Unknown reranker provider: {config.provider!r}. Supported: cohere, local")
