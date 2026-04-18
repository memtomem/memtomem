"""Configuration-related schemas."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class ConfigEmbeddingOut(BaseModel):
    provider: str
    model: str
    dimension: int
    base_url: str
    batch_size: int
    api_key: str = "***"


class ConfigStorageOut(BaseModel):
    backend: str
    sqlite_path: str
    collection_name: str


class ConfigSearchOut(BaseModel):
    default_top_k: int
    bm25_candidates: int
    dense_candidates: int
    rrf_k: int
    enable_bm25: bool
    enable_dense: bool
    tokenizer: str
    rrf_weights: list[float]


class ConfigIndexingOut(BaseModel):
    memory_dirs: list[str]
    supported_extensions: list[str]
    max_chunk_tokens: int
    min_chunk_tokens: int = 0
    target_chunk_tokens: int = 0
    chunk_overlap_tokens: int = 0
    structured_chunk_mode: str = "original"


class ConfigDecayOut(BaseModel):
    enabled: bool
    half_life_days: float


class ConfigMMROut(BaseModel):
    enabled: bool
    lambda_param: float


class ConfigNamespaceOut(BaseModel):
    default_namespace: str
    enable_auto_ns: bool


class ConfigResponse(BaseModel):
    embedding: ConfigEmbeddingOut
    storage: ConfigStorageOut
    search: ConfigSearchOut
    indexing: ConfigIndexingOut
    decay: ConfigDecayOut
    mmr: ConfigMMROut
    namespace: ConfigNamespaceOut


class ConfigPatchRequest(BaseModel):
    """Section-level partial update. Include only fields to change."""

    model_config = ConfigDict(extra="allow")

    search: dict[str, Any] | None = None
    indexing: dict[str, Any] | None = None
    embedding: dict[str, Any] | None = None
    decay: dict[str, Any] | None = None
    mmr: dict[str, Any] | None = None
    namespace: dict[str, Any] | None = None


class ConfigPatchChange(BaseModel):
    field: str
    old_value: str
    new_value: str


class ConfigPatchResponse(BaseModel):
    applied: list[ConfigPatchChange]
    rejected: list[str]


class EmbeddingConfigInfo(BaseModel):
    dimension: int
    provider: str
    model: str


class EmbeddingStatusResponse(BaseModel):
    has_mismatch: bool
    dimension_mismatch: bool = False
    model_mismatch: bool = False
    stored: EmbeddingConfigInfo | None = None
    configured: EmbeddingConfigInfo | None = None


class EmbeddingResetResponse(BaseModel):
    ok: bool
    message: str
