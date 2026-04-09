"""Configuration system using Pydantic Settings."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class EmbeddingConfig(BaseSettings):
    provider: str = "ollama"
    model: str = "nomic-embed-text"
    dimension: int = 768
    base_url: str = "http://localhost:11434"
    api_key: str = ""
    batch_size: int = 64
    max_concurrent_batches: int = 4

    @field_validator("dimension", "batch_size", "max_concurrent_batches")
    @classmethod
    def must_be_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("must be positive")
        return v


class StorageConfig(BaseSettings):
    backend: str = "sqlite"
    sqlite_path: Path = Path("~/.memtomem/memtomem.db")
    collection_name: str = "memories"


class SearchConfig(BaseSettings):
    default_top_k: int = 10
    bm25_candidates: int = 50
    dense_candidates: int = 50
    rrf_k: int = 60
    enable_bm25: bool = True
    enable_dense: bool = True
    tokenizer: str = "unicode61"  # "unicode61" or "kiwipiepy"
    rrf_weights: list[float] = Field(default_factory=lambda: [1.0, 1.0])  # [BM25, Dense]
    cache_ttl: float = 30.0  # search result cache TTL in seconds

    @field_validator("default_top_k", "bm25_candidates", "dense_candidates", "rrf_k")
    @classmethod
    def must_be_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("must be positive")
        return v

    @field_validator("tokenizer")
    @classmethod
    def valid_tokenizer(cls, v: str) -> str:
        allowed = {"unicode61", "kiwipiepy"}
        if v not in allowed:
            raise ValueError(f"tokenizer must be one of {allowed}")
        return v


class IndexingConfig(BaseSettings):
    memory_dirs: list[Path] = Field(default_factory=lambda: [Path("~/.memtomem/memories")])
    supported_extensions: frozenset[str] = frozenset(
        {
            ".md",
            ".json",
            ".yaml",
            ".yml",
            ".toml",
            ".py",
            ".js",
            ".ts",
            ".tsx",
            ".jsx",
        }
    )
    max_chunk_tokens: int = 512
    min_chunk_tokens: int = 128
    chunk_overlap_tokens: int = 0
    structured_chunk_mode: str = "original"  # "original" or "recursive"
    paragraph_split_threshold: int = 800  # split long prose into paragraphs above this token count

    @field_validator(
        "max_chunk_tokens", "min_chunk_tokens", "chunk_overlap_tokens", "paragraph_split_threshold"
    )
    @classmethod
    def must_be_non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("must be non-negative")
        return v


class DecayConfig(BaseSettings):
    enabled: bool = False
    half_life_days: float = 30.0

    @field_validator("half_life_days")
    @classmethod
    def must_be_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("must be positive")
        return v


class MMRConfig(BaseSettings):
    enabled: bool = False
    lambda_param: float = 0.7  # 0.0=diversity max, 1.0=relevance max

    @field_validator("lambda_param")
    @classmethod
    def must_be_in_range(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("lambda_param must be between 0.0 and 1.0")
        return v


class AccessConfig(BaseSettings):
    enabled: bool = False
    max_boost: float = 1.5  # maximum score multiplier for highly accessed chunks

    @field_validator("max_boost")
    @classmethod
    def must_be_at_least_one(cls, v: float) -> float:
        if v < 1.0:
            raise ValueError("max_boost must be >= 1.0")
        return v


class NamespaceConfig(BaseSettings):
    default_namespace: str = "default"
    enable_auto_ns: bool = False


class RerankConfig(BaseSettings):
    enabled: bool = False
    provider: str = "cohere"  # "cohere" | "local"
    model: str = "rerank-english-v3.0"
    top_k: int = 20  # candidates to pass to reranker
    api_key: str = ""

    @field_validator("top_k")
    @classmethod
    def must_be_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("must be positive")
        return v


class QueryExpansionConfig(BaseSettings):
    enabled: bool = False
    max_terms: int = 3
    strategy: str = "tags"  # "tags" | "headings" | "both"

    @field_validator("strategy")
    @classmethod
    def valid_strategy(cls, v: str) -> str:
        if v not in ("tags", "headings", "both"):
            raise ValueError("strategy must be 'tags', 'headings', or 'both'")
        return v


class ImportanceConfig(BaseSettings):
    enabled: bool = False
    max_boost: float = 1.5
    weights: list[float] = Field(default_factory=lambda: [0.3, 0.2, 0.3, 0.2])

    @field_validator("max_boost")
    @classmethod
    def must_be_at_least_one(cls, v: float) -> float:
        if v < 1.0:
            raise ValueError("max_boost must be >= 1.0")
        return v


class ConflictConfig(BaseSettings):
    enabled: bool = False
    threshold: float = 0.75
    auto_check: bool = False  # auto-check on mem_add


class WebhookConfig(BaseSettings):
    enabled: bool = False
    url: str = ""
    events: list[str] = Field(default_factory=lambda: ["add", "delete", "search"])
    secret: str = ""
    timeout_seconds: float = 10.0


class ConsolidationScheduleConfig(BaseSettings):
    enabled: bool = False
    interval_hours: float = 24.0
    min_group_size: int = 3
    max_groups: int = 10


class PolicyConfig(BaseSettings):
    """Memory lifecycle policies."""

    enabled: bool = False
    scheduler_interval_minutes: float = 60.0
    max_actions_per_run: int = 100


class EntityExtractionConfig(BaseSettings):
    """Entity extraction from chunk content."""

    enabled: bool = False
    extract_on_index: bool = False
    entity_types: list[str] = Field(
        default_factory=lambda: [
            "person",
            "date",
            "decision",
            "action_item",
            "technology",
            "concept",
        ]
    )
    min_confidence: float = 0.5


class ContextWindowConfig(BaseSettings):
    """Context window expansion for search results (small-to-big retrieval)."""

    enabled: bool = False
    window_size: int = 2  # ±N adjacent chunks

    @field_validator("window_size")
    @classmethod
    def must_be_in_range(cls, v: int) -> int:
        if not 0 <= v <= 10:
            raise ValueError("window_size must be 0-10")
        return v


class HealthWatchdogConfig(BaseSettings):
    """Periodic health monitoring and auto-maintenance."""

    enabled: bool = False
    heartbeat_interval_seconds: float = 60.0
    diagnostic_interval_seconds: float = 300.0
    deep_interval_seconds: float = 3600.0
    max_snapshots: int = 1000
    orphan_cleanup_threshold: int = 10
    auto_maintenance: bool = True


class Mem2MemConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MEMTOMEM_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    indexing: IndexingConfig = Field(default_factory=IndexingConfig)
    decay: DecayConfig = Field(default_factory=DecayConfig)
    mmr: MMRConfig = Field(default_factory=MMRConfig)
    access: AccessConfig = Field(default_factory=AccessConfig)
    namespace: NamespaceConfig = Field(default_factory=NamespaceConfig)
    rerank: RerankConfig = Field(default_factory=RerankConfig)
    query_expansion: QueryExpansionConfig = Field(default_factory=QueryExpansionConfig)
    importance: ImportanceConfig = Field(default_factory=ImportanceConfig)
    conflict: ConflictConfig = Field(default_factory=ConflictConfig)
    webhook: WebhookConfig = Field(default_factory=WebhookConfig)
    consolidation_schedule: ConsolidationScheduleConfig = Field(
        default_factory=ConsolidationScheduleConfig
    )
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    entity_extraction: EntityExtractionConfig = Field(default_factory=EntityExtractionConfig)
    context_window: ContextWindowConfig = Field(default_factory=ContextWindowConfig)
    health_watchdog: HealthWatchdogConfig = Field(default_factory=HealthWatchdogConfig)


# ---------------------------------------------------------------------------
# Config persistence: ~/.memtomem/config.json override layer
# ---------------------------------------------------------------------------

_CONFIG_OVERRIDE_PATH = Path("~/.memtomem/config.json")


def _override_path() -> Path:
    return _CONFIG_OVERRIDE_PATH.expanduser()


def load_config_overrides(config: Mem2MemConfig) -> None:
    """Apply persisted overrides from ~/.memtomem/config.json (if exists)."""
    import json as _json
    import logging

    _log = logging.getLogger(__name__)

    path = _override_path()
    if not path.exists():
        return
    try:
        data = _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, _json.JSONDecodeError) as exc:
        _log.warning("Failed to read config overrides from %s: %s", path, exc)
        return
    for section_name, updates in data.items():
        section_obj = getattr(config, section_name, None)
        if section_obj is None or not isinstance(updates, dict):
            continue
        for key, value in updates.items():
            if hasattr(section_obj, key):
                try:
                    setattr(section_obj, key, value)
                except (TypeError, ValueError) as exc:
                    _log.warning(
                        "Skipping invalid config override %s.%s=%r: %s",
                        section_name,
                        key,
                        value,
                        exc,
                    )


def save_config_overrides(config: Mem2MemConfig, mutable_fields: dict[str, set[str]]) -> None:
    """Persist mutable fields to ~/.memtomem/config.json."""
    import json as _json

    data: dict[str, dict[str, object]] = {}
    for section_name, fields in mutable_fields.items():
        section_obj = getattr(config, section_name, None)
        if section_obj is None:
            continue
        section_data: dict[str, object] = {}
        for key in fields:
            val = getattr(section_obj, key, None)
            if val is not None:
                section_data[key] = val
        if section_data:
            data[section_name] = section_data

    path = _override_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json.dumps(data, indent=2, default=str), encoding="utf-8")
