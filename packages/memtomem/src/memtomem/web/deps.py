"""FastAPI dependency injectors."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from fastapi import HTTPException, Request

from memtomem.storage.sqlite_helpers import norm_path

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


def get_project_root(request: Request) -> Path:
    return request.app.state.project_root


def require_indexed_source(user_path: str, indexed_sources: Iterable[Path]) -> Path:
    """Return the NFC-normalized Path for ``user_path`` if it matches any indexed source.

    Raises 403 when the path is not indexed. Normalizes both sides with
    ``norm_path`` (resolve + NFC) so an NFC user-typed path can still match
    an NFD on-disk path on macOS/APFS (issue #235).
    """
    request_norm = norm_path(Path(user_path))
    indexed_norms = {norm_path(p) for p in indexed_sources}
    if request_norm not in indexed_norms:
        raise HTTPException(status_code=403, detail="Path is not an indexed source file.")
    return Path(request_norm)
