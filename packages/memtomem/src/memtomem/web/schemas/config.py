"""Configuration-related schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class ConfigEmbeddingOut(BaseModel):
    provider: str
    model: str
    dimension: int
    base_url: str
    batch_size: int
    api_key: str = "***"
    # ONNX Runtime intra-op thread cap. Default 4 (caps ONNX so a bulk
    # reindex doesn't pin every core); 0 = ORT default (all physical
    # cores). Surfaced read-only here so users can discover the knob in
    # the Config tab before they edit ``~/.memtomem/config.json``.
    # Restart-required and excluded from ``MUTABLE_FIELDS`` per
    # ``EmbeddingConfig`` in config.py.
    threads: int = 4


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
    exclude_patterns: list[str] = []


class BuiltinExcludePatternsResponse(BaseModel):
    secret: list[str]
    noise: list[str]


class PrivacyPatternEntry(BaseModel):
    pattern: str
    flags: str


class PrivacyPatternsResponse(BaseModel):
    patterns: list[PrivacyPatternEntry]
    sha: str


class PrivacyStatsResponse(BaseModel):
    """GUI view of ``privacy.snapshot()`` — the process-lifetime redaction
    counters also surfaced over MCP by ``mem_add_redaction_stats`` (ADR-0006
    Axis E.1 audit surface).

    ``outcomes`` is the cumulative tally per outcome
    (``blocked`` / ``pass`` / ``bypassed`` / ``blocked_project_shared``);
    ``by_tool`` breaks the same tally down per write surface (``mem_add``,
    ``index``, ``web_api_upload``, …). Both are process-lifetime and reset on
    restart — not persisted rows.
    """

    outcomes: dict[str, int]
    by_tool: dict[str, dict[str, int]]


class ConfigDecayOut(BaseModel):
    enabled: bool
    half_life_days: float


class ConfigMMROut(BaseModel):
    enabled: bool
    lambda_param: float


class ConfigRerankOut(BaseModel):
    enabled: bool
    provider: str
    model: str
    oversample: float
    min_pool: int
    max_pool: int


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
    rerank: ConfigRerankOut
    namespace: ConfigNamespaceOut
    # Hot-reload surface: FE uses ``config_mtime_ns`` to detect external
    # edits on visibilitychange and ``config_reload_error`` to render a
    # banner when disk state is invalid (see web/hot_reload.py).
    config_mtime_ns: int = -1
    config_reload_error: str | None = None


class ConfigPatchRequest(BaseModel):
    """Section-level partial update. Include only fields to change."""

    model_config = ConfigDict(extra="allow")

    search: dict[str, Any] | None = None
    indexing: dict[str, Any] | None = None
    embedding: dict[str, Any] | None = None
    decay: dict[str, Any] | None = None
    mmr: dict[str, Any] | None = None
    rerank: dict[str, Any] | None = None
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


class EmbeddingCoverage(BaseModel):
    """Dense-vector coverage rollup for the ``chunks`` table.

    ``total`` is the chunk row count; ``with_dense`` is the subset that
    also has an embedding row in ``chunks_vec``. ``percent`` rounds to
    one decimal. When ``total == 0`` ``percent`` is ``0.0`` so callers
    can render the field directly without a divide-by-zero branch.
    """

    total: int
    with_dense: int
    percent: float


class EmbeddingStatusResponse(BaseModel):
    has_mismatch: bool
    dimension_mismatch: bool = False
    model_mismatch: bool = False
    stored: EmbeddingConfigInfo | None = None
    configured: EmbeddingConfigInfo | None = None
    coverage: EmbeddingCoverage | None = None


class EmbeddingResetResponse(BaseModel):
    ok: bool
    message: str


# ── Model readiness (issue #696) ─────────────────────────────────────
# Surfaces lazy-load state of the fastembed embedder + reranker so the
# Web UI can render a "Downloading…" / "Loading…" banner instead of
# leaving the user staring at a frozen Search button while a multi-GB
# model snapshot streams in. See ``GET /api/system/model-readiness``.

ModelComponentState = Literal[
    "ready",  # model is loaded in memory and ready to serve
    "loading",  # cache is on disk; constructor in flight
    "downloading",  # cache absent; constructor in flight
    "cold",  # nothing in flight; cache may or may not be present
    "error",  # last constructor attempt raised
    "skipped",  # provider not fastembed, or component disabled
]


class ModelComponent(BaseModel):
    """Per-component (embedder OR reranker) readiness snapshot."""

    state: ModelComponentState
    provider: str
    model: str | None = None
    cache_present: bool = False
    approx_size_mb: int | None = None
    error: str | None = None


class ModelReadinessResponse(BaseModel):
    embedder: ModelComponent
    reranker: ModelComponent
