"""FastAPI dependency injectors."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Request

if TYPE_CHECKING:
    from memtomem.config import Mem2MemConfig
    from memtomem.embedding.base import EmbeddingProvider
    from memtomem.indexing.engine import IndexEngine
    from memtomem.search.pipeline import SearchPipeline
    from memtomem.storage.sqlite_backend import SqliteBackend


def get_storage(request: Request) -> SqliteBackend:
    return request.app.state.storage


def get_search_pipeline(request: Request) -> SearchPipeline:
    return request.app.state.search_pipeline


def get_index_engine(request: Request) -> IndexEngine:
    return request.app.state.index_engine


def get_embedder(request: Request) -> EmbeddingProvider:
    return request.app.state.embedder


def get_config(request: Request) -> Mem2MemConfig:
    return request.app.state.config


def get_dedup_scanner(request: Request):
    return request.app.state.dedup_scanner
