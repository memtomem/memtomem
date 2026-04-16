"""Configuration system using Pydantic Settings."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from pydantic import Field, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class EmbeddingConfig(BaseSettings):
    provider: str = "none"
    model: str = ""
    dimension: int = 0
    base_url: str = ""
    api_key: str = ""
    batch_size: int = 64
    max_concurrent_batches: int = 4

    @field_validator("dimension")
    @classmethod
    def dimension_non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("must be non-negative (0 = no embeddings)")
        return v

    @field_validator("batch_size", "max_concurrent_batches")
    @classmethod
    def must_be_positive(cls, v: int, info: ValidationInfo) -> int:
        if v <= 0:
            raise ValueError(f"{info.field_name} must be positive, got {v}")
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
    # Namespaces starting with any of these prefixes are excluded from
    # *default* search (``namespace=None``) but remain retrievable with an
    # explicit namespace argument. Keeps system-generated buckets
    # (auto_archive targets, auto_consolidate ``archive:summary`` summaries)
    # out of day-to-day results while preserving their audit trail.
    # Set to an empty list to restore the pre-Phase-A.5 behavior where every
    # namespace is searchable by default.
    system_namespace_prefixes: list[str] = Field(default_factory=lambda: ["archive:"])

    @field_validator("default_top_k", "bm25_candidates", "dense_candidates", "rrf_k")
    @classmethod
    def must_be_positive(cls, v: int, info: ValidationInfo) -> int:
        if v <= 0:
            raise ValueError(f"{info.field_name} must be positive, got {v}")
        return v

    @field_validator("tokenizer")
    @classmethod
    def valid_tokenizer(cls, v: str) -> str:
        allowed = {"unicode61", "kiwipiepy"}
        if v not in allowed:
            raise ValueError(f"tokenizer must be one of {allowed}")
        return v

    @field_validator("system_namespace_prefixes")
    @classmethod
    def prefix_count_capped(cls, v: list[str]) -> list[str]:
        # Cap catches dynamic-generation mistakes at startup rather than
        # emitting a runaway N × M LIKE clause every search call. 10 is
        # generous — real configs are expected to have 1-3 entries.
        if len(v) > 10:
            raise ValueError(
                f"system_namespace_prefixes has {len(v)} entries; cap is 10. "
                "Did you accidentally generate prefixes dynamically?"
            )
        return v


def _default_memory_dirs() -> list[Path]:
    """Build default memory_dirs, auto-discovering well-known AI tool directories."""
    dirs: list[Path] = [Path("~/.memtomem/memories")]
    for d in _auto_discovered_memory_dirs():
        dirs.append(d)
    return dirs


class IndexingConfig(BaseSettings):
    memory_dirs: list[Path] = Field(default_factory=lambda: _default_memory_dirs())
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
    def must_be_non_negative(cls, v: int, info: ValidationInfo) -> int:
        if v < 0:
            raise ValueError(f"{info.field_name} must be non-negative, got {v}")
        return v

    @model_validator(mode="after")
    def check_chunk_token_range(self) -> "IndexingConfig":
        if self.min_chunk_tokens > self.max_chunk_tokens:
            raise ValueError(
                f"min_chunk_tokens ({self.min_chunk_tokens}) must be "
                f"<= max_chunk_tokens ({self.max_chunk_tokens})"
            )
        return self


class DecayConfig(BaseSettings):
    enabled: bool = False
    half_life_days: float = 30.0

    @field_validator("half_life_days")
    @classmethod
    def must_be_positive(cls, v: float, info: ValidationInfo) -> float:
        if v <= 0:
            raise ValueError(f"{info.field_name} must be positive, got {v}")
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
    def must_be_positive(cls, v: int, info: ValidationInfo) -> int:
        if v <= 0:
            raise ValueError(f"{info.field_name} must be positive, got {v}")
        return v


class QueryExpansionConfig(BaseSettings):
    enabled: bool = False
    max_terms: int = 3
    strategy: str = "tags"  # "tags" | "headings" | "both" | "llm"

    @field_validator("strategy")
    @classmethod
    def valid_strategy(cls, v: str) -> str:
        if v not in ("tags", "headings", "both", "llm"):
            raise ValueError("strategy must be 'tags', 'headings', 'both', or 'llm'")
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


MAX_CONTEXT_WINDOW_CHUNKS = 10  # max ±N adjacent chunks around each hit


class ContextWindowConfig(BaseSettings):
    """Context window expansion for search results (small-to-big retrieval)."""

    enabled: bool = False
    window_size: int = 2  # ±N adjacent chunks

    @field_validator("window_size")
    @classmethod
    def must_be_in_range(cls, v: int) -> int:
        if not 0 <= v <= MAX_CONTEXT_WINDOW_CHUNKS:
            raise ValueError(f"window_size must be 0-{MAX_CONTEXT_WINDOW_CHUNKS}")
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


class LLMConfig(BaseSettings):
    enabled: bool = False
    provider: str = "ollama"
    model: str = ""  # empty = provider-specific default resolved in factory
    base_url: str = "http://localhost:11434"
    api_key: str = ""
    max_tokens: int = 1024
    timeout: float = 60.0

    @field_validator("max_tokens")
    @classmethod
    def max_tokens_positive(cls, v: int, info: ValidationInfo) -> int:
        if v <= 0:
            raise ValueError(f"{info.field_name} must be positive, got {v}")
        return v

    @field_validator("timeout")
    @classmethod
    def timeout_positive(cls, v: float, info: ValidationInfo) -> float:
        if v <= 0:
            raise ValueError(f"{info.field_name} must be positive, got {v}")
        return v


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
    webhook: WebhookConfig = Field(default_factory=WebhookConfig)
    consolidation_schedule: ConsolidationScheduleConfig = Field(
        default_factory=ConsolidationScheduleConfig
    )
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    context_window: ContextWindowConfig = Field(default_factory=ContextWindowConfig)
    health_watchdog: HealthWatchdogConfig = Field(default_factory=HealthWatchdogConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)


# ---------------------------------------------------------------------------
# Canonical mutable-field definitions and validation
# ---------------------------------------------------------------------------
# Single source of truth for which config fields can be modified at runtime
# via CLI (``mm config set``), Web UI (``PATCH /api/config``), and MCP
# (``mem_config``).  All three paths import from here.

MUTABLE_FIELDS: dict[str, set[str]] = {
    "search": {
        "default_top_k",
        "bm25_candidates",
        "dense_candidates",
        "rrf_k",
        "enable_bm25",
        "enable_dense",
        "tokenizer",
        "rrf_weights",
    },
    "indexing": {
        "max_chunk_tokens",
        "min_chunk_tokens",
        "chunk_overlap_tokens",
        "structured_chunk_mode",
    },
    "embedding": {"batch_size"},
    "decay": {"enabled", "half_life_days"},
    "mmr": {"enabled", "lambda_param"},
    "namespace": {"default_namespace", "enable_auto_ns"},
}

FIELD_CONSTRAINTS: dict[str, dict] = {
    "search.default_top_k": {"type": int, "min": 1, "max": 500},
    "search.bm25_candidates": {"type": int, "min": 1, "max": 1000},
    "search.dense_candidates": {"type": int, "min": 1, "max": 1000},
    "search.rrf_k": {"type": int, "min": 1, "max": 1000},
    "search.enable_bm25": {"type": bool},
    "search.enable_dense": {"type": bool},
    "search.tokenizer": {"type": str, "allowed": {"unicode61", "kiwipiepy"}},
    "indexing.max_chunk_tokens": {"type": int, "min": 64, "max": 8192},
    "indexing.min_chunk_tokens": {"type": int, "min": 0, "max": 256},
    "indexing.chunk_overlap_tokens": {"type": int, "min": 0, "max": 512},
    "indexing.structured_chunk_mode": {"type": str, "allowed": {"original", "recursive"}},
    "embedding.batch_size": {"type": int, "min": 1, "max": 1024},
    "decay.enabled": {"type": bool},
    "decay.half_life_days": {"type": float, "min": 0.1},
    "mmr.enabled": {"type": bool},
    "mmr.lambda_param": {"type": float, "min": 0.0, "max": 1.0},
    "search.rrf_weights": {"type": list, "item_type": float, "length": 2},
    "namespace.default_namespace": {"type": str},
    "namespace.enable_auto_ns": {"type": bool},
}


def coerce_and_validate(value: object, constraint: dict | None) -> object:
    """Coerce *value* to the expected type and validate min/max/allowed constraints."""
    if constraint is None:
        return value

    expected_type = constraint["type"]

    if expected_type is bool:
        if isinstance(value, bool):
            coerced: bool | int | float | str | list[object] = value
        elif isinstance(value, str):
            low = value.lower()
            if low in ("true", "1", "yes"):
                coerced = True
            elif low in ("false", "0", "no"):
                coerced = False
            else:
                raise ValueError(f"cannot convert '{value}' to bool")
        elif isinstance(value, (int, float)):
            coerced = bool(value)
        else:
            raise ValueError(f"cannot convert to bool: {value}")
    elif expected_type is int:
        if not isinstance(value, (str, int, float)):
            raise ValueError(f"cannot convert '{value}' to int")
        try:
            coerced = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"cannot convert '{value}' to int")
    elif expected_type is float:
        if not isinstance(value, (str, int, float)):
            raise ValueError(f"cannot convert '{value}' to float")
        try:
            coerced = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"cannot convert '{value}' to float")
    elif expected_type is str:
        coerced = str(value)
    elif expected_type is list:
        item_type = constraint.get("item_type", float)
        expected_len = constraint.get("length")
        if isinstance(value, str):
            parts = [s.strip() for s in value.split(",")]
        elif isinstance(value, (list, tuple)):
            parts = list(value)
        else:
            raise ValueError(f"cannot convert {type(value).__name__} to list")
        try:
            coerced = [item_type(p) for p in parts]
        except (TypeError, ValueError):
            raise ValueError(f"cannot convert list items to {item_type.__name__}")
        if expected_len is not None and len(coerced) != expected_len:
            raise ValueError(f"expected length {expected_len}, got {len(coerced)}")
    else:
        coerced = cast("bool | int | float | str | list[object]", value)

    min_val = constraint.get("min")
    if (
        isinstance(min_val, (int, float))
        and isinstance(coerced, (int, float))
        and coerced < min_val
    ):
        raise ValueError(f"must be >= {min_val}")
    max_val = constraint.get("max")
    if (
        isinstance(max_val, (int, float))
        and isinstance(coerced, (int, float))
        and coerced > max_val
    ):
        raise ValueError(f"must be <= {max_val}")
    if "allowed" in constraint and coerced not in constraint["allowed"]:
        raise ValueError(f"must be one of {constraint['allowed']}")

    return coerced


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
            if section_obj is None and isinstance(updates, dict):
                _log.warning("Unknown config section '%s' in %s (ignored)", section_name, path)
            continue
        for key, value in updates.items():
            if hasattr(section_obj, key):
                full_key = f"{section_name}.{key}"
                constraint = FIELD_CONSTRAINTS.get(full_key)
                if constraint:
                    try:
                        value = coerce_and_validate(value, constraint)
                    except ValueError as exc:
                        _log.warning(
                            "Invalid config value %s=%r in %s: %s (using default)",
                            full_key,
                            value,
                            path,
                            exc,
                        )
                        continue
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


def ensure_auto_discovered_dirs(config: Mem2MemConfig) -> None:
    """Append auto-discovered well-known dirs that aren't already in memory_dirs.

    Call *after* ``load_config_overrides`` so that user overrides don't
    suppress auto-discovery of AI tool directories like ``~/.claude/projects``.
    """
    existing = {Path(d).expanduser().resolve() for d in config.indexing.memory_dirs}
    for d in _auto_discovered_memory_dirs():
        resolved = d.expanduser().resolve()
        if resolved not in existing:
            config.indexing.memory_dirs.append(d)


def _auto_discovered_memory_dirs() -> list[Path]:
    """Return well-known AI tool directories that exist on this machine.

    Checked directories:
    - ``~/.claude/projects``  — Claude Code per-project auto-memory
    - ``~/.gemini``           — Gemini CLI global GEMINI.md
    - ``~/.codex/memories``   — Codex CLI global memories
    """
    candidates: list[Path] = [
        Path("~/.claude/projects"),
        Path("~/.gemini"),
        Path("~/.codex/memories"),
    ]
    return [p.expanduser() for p in candidates if p.expanduser().is_dir()]


# Fields persisted by ``save_config_overrides`` but NOT settable via the
# generic ``mm config set`` / ``mem_config`` path.  Managed through dedicated
# endpoints (e.g. Web UI ``/memory-dirs/*``).
_EXTRA_PERSIST_FIELDS: dict[str, set[str]] = {
    "indexing": {"memory_dirs"},
}


def save_config_overrides(
    config: Mem2MemConfig,
    mutable_fields: dict[str, set[str]] | None = None,
) -> None:
    """Persist mutable fields to ~/.memtomem/config.json.

    Uses **read-merge-write** so that keys not in *mutable_fields* (e.g.
    init-only settings like ``embedding.provider`` or ``storage.sqlite_path``)
    are preserved across saves.
    """
    import json as _json
    import logging

    _log = logging.getLogger(__name__)
    base = mutable_fields or MUTABLE_FIELDS
    # Merge extra-persist fields so they are always written alongside mutables.
    effective: dict[str, set[str]] = {}
    for section in {*base, *_EXTRA_PERSIST_FIELDS}:
        effective[section] = base.get(section, set()) | _EXTRA_PERSIST_FIELDS.get(section, set())
    mutable_fields = effective

    path = _override_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    # ── Read existing config (merge base) ──
    existing: dict = {}
    if path.exists():
        try:
            existing = _json.loads(path.read_text(encoding="utf-8"))
        except (OSError, _json.JSONDecodeError) as exc:
            _log.warning("Cannot read existing config at %s: %s — overwriting", path, exc)

    # ── Merge mutable fields into existing data ──
    for section_name, fields in mutable_fields.items():
        section_obj = getattr(config, section_name, None)
        if section_obj is None:
            continue
        section_data: dict[str, object] = existing.get(section_name, {})
        if not isinstance(section_data, dict):
            section_data = {}
        for key in fields:
            val = getattr(section_obj, key, None)
            if val is not None:
                section_data[key] = val
        if section_data:
            existing[section_name] = section_data

    path.write_text(_json.dumps(existing, indent=2, default=str), encoding="utf-8")
