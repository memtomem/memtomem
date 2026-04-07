"""Proxy gateway configuration."""

from __future__ import annotations

import json
import logging
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class CompressionStrategy(StrEnum):
    NONE = "none"
    AUTO = "auto"
    TRUNCATE = "truncate"
    EXTRACT_FIELDS = "extract_fields"
    SCHEMA_PRUNING = "schema_pruning"
    SKELETON = "skeleton"
    LLM_SUMMARY = "llm_summary"
    SELECTIVE = "selective"
    HYBRID = "hybrid"


class TailMode(StrEnum):
    TOC = "toc"
    TRUNCATE = "truncate"


class TransportType(StrEnum):
    STDIO = "stdio"
    SSE = "sse"
    STREAMABLE_HTTP = "streamable_http"


class LLMProvider(StrEnum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    OLLAMA = "ollama"


class LLMCompressorConfig(BaseModel):
    provider: LLMProvider = LLMProvider.OPENAI
    model: str = "gpt-4o-mini"
    api_key: str = ""
    base_url: str = ""
    system_prompt: str = (
        "Summarize the following content concisely, preserving all key information. "
        "Keep the summary under {max_chars} characters."
    )
    max_tokens: int = 500


class CleaningConfig(BaseModel):
    enabled: bool = True
    strip_html: bool = True
    deduplicate: bool = True
    collapse_links: bool = True


class HybridConfig(BaseModel):
    head_chars: int = 5000
    tail_mode: TailMode = TailMode.TOC
    min_toc_budget: int = 200
    min_head_chars: int = 100
    head_ratio: float = 0.6


class SelectiveConfig(BaseModel):
    max_pending: int = 100
    pending_ttl_seconds: float = 300.0
    json_depth: int = 1
    min_section_chars: int = 50


class AutoIndexConfig(BaseModel):
    enabled: bool = False
    min_chars: int = 2000
    memory_dir: Path = Path("~/.memtomem/proxy_index")
    namespace: str = "proxy-{server}"


class ToolOverrideConfig(BaseModel):
    compression: CompressionStrategy | None = None
    max_result_chars: int | None = None
    llm: LLMCompressorConfig | None = None
    selective: SelectiveConfig | None = None
    hybrid: HybridConfig | None = None
    cleaning: CleaningConfig | None = None
    auto_index: bool | None = None
    hidden: bool = False
    description_override: str | None = None


class UpstreamServerConfig(BaseModel):
    command: str = ""
    args: list[str] = []
    env: dict[str, str] | None = None
    prefix: str
    transport: TransportType = TransportType.STDIO
    url: str = ""
    headers: dict[str, str] | None = None
    compression: CompressionStrategy = CompressionStrategy.AUTO
    max_result_chars: int = 2000
    llm: LLMCompressorConfig | None = None
    selective: SelectiveConfig | None = None
    hybrid: HybridConfig | None = None
    cleaning: CleaningConfig | None = None
    tool_overrides: dict[str, ToolOverrideConfig] = {}
    auto_index: bool | None = None
    max_retries: int = 3
    reconnect_delay_seconds: float = 1.0
    max_reconnect_delay_seconds: float = 30.0
    max_description_chars: int = 200
    strip_schema_descriptions: bool = False


class CacheConfig(BaseModel):
    enabled: bool = True
    db_path: Path = Path("~/.memtomem/proxy_cache.db")
    default_ttl_seconds: float | None = 3600.0
    max_entries: int = 10000


class MetricsConfig(BaseModel):
    enabled: bool = True
    db_path: Path = Path("~/.memtomem/proxy_metrics.db")
    max_history: int = 10000


class ProxyConfig(BaseModel):
    enabled: bool = False
    config_path: Path = Path("~/.memtomem/stm_proxy.json")
    upstream_servers: dict[str, UpstreamServerConfig] = {}
    default_compression: CompressionStrategy = CompressionStrategy.AUTO
    default_max_result_chars: int = 16000
    min_result_retention: float = 0.5
    """Minimum fraction of response to preserve after compression (0-1).

    If ``default_max_result_chars`` or per-tool ``max_result_chars`` would
    retain less than this fraction of the cleaned response, the effective
    budget is raised to ``len(response) * min_result_retention``.

    Default 0.5 ensures at least 50% of every response survives compression.
    Set to 0 to disable and use fixed budgets only.
    """
    max_description_chars: int = 200
    strip_schema_descriptions: bool = False
    cache: CacheConfig = Field(default_factory=CacheConfig)
    auto_index: AutoIndexConfig = Field(default_factory=AutoIndexConfig)
    metrics: MetricsConfig = Field(default_factory=MetricsConfig)

    @staticmethod
    def load_from_file(path: Path) -> ProxyConfig:
        resolved = path.expanduser().resolve()
        if not resolved.exists():
            logger.debug("Proxy config file not found: %s", resolved)
            return ProxyConfig()
        try:
            data: dict[str, Any] = json.loads(resolved.read_text(encoding="utf-8"))
            return ProxyConfig.model_validate(data)
        except (json.JSONDecodeError, Exception) as exc:
            logger.warning("Failed to parse proxy config %s: %s", resolved, exc)
            return ProxyConfig()


class ProxyConfigLoader:
    """mtime-based hot-reload for proxy config file."""

    def __init__(self, path: Path) -> None:
        self._path = path.expanduser().resolve()
        self._cached: ProxyConfig | None = None
        self._mtime: float = 0.0

    def seed(self, config: ProxyConfig) -> None:
        self._cached = config
        try:
            self._mtime = self._path.stat().st_mtime
        except OSError:
            self._mtime = -1.0

    def get(self) -> ProxyConfig:
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            if self._cached is not None:
                return self._cached
            return ProxyConfig.load_from_file(self._path)
        if mtime != self._mtime or self._cached is None:
            self._cached = ProxyConfig.load_from_file(self._path)
            self._mtime = mtime
        return self._cached
