"""Reranker factory."""

from __future__ import annotations

import asyncio
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

    if provider == "fastembed":
        from memtomem.search.reranker.fastembed import FastEmbedReranker

        return FastEmbedReranker(config)

    raise ValueError(
        f"Unknown reranker provider: {config.provider!r}. Supported: cohere, local, fastembed"
    )


async def load_reranker(reranker: object | None) -> None:
    """Load a lazy local reranker's model now, off the event loop.

    Enabling reranking at runtime calls this before installing the reranker,
    so a missing dependency (``fastembed`` not installed) or an unloadable
    model is reported to the caller instead of at the first search. Remote
    rerankers have no model to load.
    """
    if reranker is None:
        return
    load_model = getattr(reranker, "_get_model", None)
    if callable(load_model):
        await asyncio.to_thread(load_model)
