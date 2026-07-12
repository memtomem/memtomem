"""Configuration system using Pydantic Settings."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, cast, get_args

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from memtomem.constants import default_system_prefixes


@dataclass(frozen=True)
class MergeStrategy:
    """Declares how multiple sources contribute to a ``list[*]`` field.

    Read at runtime by the ``config.d/`` fragment loader. Attach to list
    fields via ``Annotated[list[X], APPEND]`` / ``Annotated[list[X], REPLACE]``
    so the strategy is co-located with the field definition and enforced by
    ``test_config_overrides.py`` (every ``list[*]`` field must declare one).

    - ``APPEND`` — each source's values are concatenated, duplicates
      removed. Use for lists where each element is independent (memory
      directories, exclude patterns, webhook events).
    - ``REPLACE`` — the highest-priority source wins; lower-priority
      lists are discarded. Use for positional tuning knobs where element
      order or length carries semantic meaning (RRF weights, importance
      weights).
    """

    mode: Literal["append", "replace"]


APPEND = MergeStrategy("append")
REPLACE = MergeStrategy("replace")


class ConfigModel(BaseModel):
    """Base for config sections and nested config entries (#1522).

    Sub-configs deliberately do not inherit ``BaseSettings`` — env binding
    flows exclusively through ``Mem2MemConfig``'s ``MEMTOMEM_`` prefix. This
    base preserves the strictness ``BaseSettings`` used to provide: unknown
    keys are rejected (a typo like ``MEMTOMEM_EMBEDDING__TYPO`` or a stray
    key in ``config.json``/``config.d`` fails loudly instead of being
    silently dropped) and defaults run through field validators.
    """

    model_config = ConfigDict(extra="forbid", validate_default=True)


class EmbeddingConfig(ConfigModel):
    provider: str = "none"
    model: str = ""
    dimension: int = 0
    base_url: str = ""
    api_key: str = ""
    batch_size: int = 64
    max_concurrent_batches: int = 4
    # ONNX Runtime intra-op thread cap for the local fastembed provider.
    # Default 4 — caps ONNX so a bulk reindex doesn't pin every physical
    # core and starve the web server / other apps. Live diagnosis of #640
    # showed the prior default (0 = ORT default = all cores) made a normal
    # indexing run feel like a hang because nothing else on the machine
    # could respond. Set to 0 to opt back into ORT's default (all physical
    # cores) for maximum throughput on dedicated machines; set to any
    # other small integer for finer control. Forwarded to
    # ``fastembed.TextEmbedding(threads=...)``; ignored by the
    # network-bound providers (Ollama, OpenAI). Bounds ORT's intra-op pool
    # only — does not affect numpy/scipy thread pools (use OMP_NUM_THREADS
    # for those).
    #
    # Restart-required, intentionally excluded from ``MUTABLE_FIELDS``: the
    # ``TextEmbedding`` instance is cached on first use, so a runtime change
    # would not take effect until restart anyway. Same precedent as
    # ``rerank.provider``/``model``/``api_key`` below.
    threads: int = 4
    # Per-chunk progress emission gate for the SSE indexing stream.
    # The index engine only forwards ``chunk_progress`` events to the
    # stream when a single file produces more than this many chunks —
    # avoids spamming the UI with one event per tiny file. Semantics:
    #   * ``> 0`` (default): emit only when ``chunks_total > threshold``.
    #   * ``0``: always emit (debug affordance — useful when validating
    #     the SSE plumbing on small fixtures, or when a user reports
    #     "I never see chunk_progress" and you want to confirm the
    #     event flows at all).
    # Runtime-mutable: the gate lives in the engine, not in cached
    # embedder state, so a config change takes effect on the next file.
    #
    # Why 32 (vs ``batch_size`` default 64)? Files producing ≤ batch_size
    # chunks finish in one batch — there's no mid-progress to show, so
    # any threshold ≥ batch_size would suppress them naturally. Picking
    # 32 = batch_size/2 means "1-batch files stay quiet, 2+-batch files
    # start ticking", which is the natural break-point between "instant"
    # and "user wonders if anything is happening". Adjustable per
    # operator preference.
    progress_threshold: int = 32

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

    @field_validator("threads")
    @classmethod
    def threads_non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("threads must be non-negative (0 = ORT default)")
        return v

    @field_validator("progress_threshold")
    @classmethod
    def progress_threshold_non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("progress_threshold must be non-negative (0 = always emit)")
        return v


class StorageConfig(ConfigModel):
    backend: str = "sqlite"
    sqlite_path: Path = Path("~/.memtomem/memtomem.db")
    collection_name: str = "memories"


class SearchConfig(ConfigModel):
    default_top_k: int = 10
    bm25_candidates: int = 50
    dense_candidates: int = 50
    rrf_k: int = 60
    enable_bm25: bool = True
    enable_dense: bool = True
    tokenizer: str = "unicode61"  # "unicode61" or "kiwipiepy"
    rrf_weights: Annotated[list[float], REPLACE] = Field(
        default_factory=lambda: [1.0, 1.0]
    )  # [BM25, Dense]
    cache_ttl: float = 30.0  # search result cache TTL in seconds
    # Namespaces starting with any of these prefixes are excluded from
    # *default* search (``namespace=None``) but remain retrievable with an
    # explicit namespace argument. Keeps system-generated buckets
    # (auto_archive targets, auto_consolidate ``archive:summary`` summaries)
    # *and* per-agent private buckets (``agent-runtime:<id>``) out of
    # day-to-day results while preserving their audit trail. Set to an
    # empty list to restore the pre-Phase-A.5 behavior where every
    # namespace is searchable by default. The default list is sourced from
    # ``memtomem.constants.default_system_prefixes`` so multi_agent and
    # CLI code can derive the same prefix without re-declaring the literal.
    system_namespace_prefixes: Annotated[list[str], APPEND] = Field(
        default_factory=default_system_prefixes
    )

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
    """Build default memory_dirs.

    Only the single canonical user dir ``~/.memtomem/memories`` is returned.
    Provider memory dirs (Claude Code per-project memory, Claude plans, Codex
    memories) are added explicitly via the ``mm init`` wizard's
    "Provider memory folders" step; existing installs that previously relied
    on the legacy ``indexing.auto_discover`` flag get a one-shot migration
    from :func:`_migrate_auto_discover_once`.
    """
    return [Path("~/.memtomem/memories")]


class IndexingConfig(ConfigModel):
    memory_dirs: Annotated[list[Path], APPEND] = Field(
        default_factory=lambda: _default_memory_dirs()
    )
    # Project-tier index roots (ADR-0011). Each entry MUST resolve under a
    # ``<X>/.memtomem/memories`` or ``<X>/.memtomem/memories.local`` directory
    # so the path classifier can derive the scope without a side table. Empty
    # by default; users opt in via ``mm mem init`` (#1700 — writes through
    # ``register_project_memory_dir``) or by editing the config directly.
    # Sibling of ``memory_dirs`` so the wizard / Sources tab / auto-discover
    # migration stay scope-unaware.
    project_memory_dirs: Annotated[list[Path], APPEND] = Field(default_factory=list)
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
    # Soft goal for semantic packing: merge adjacent short siblings while
    # cur < target and combined <= max. Set to 0 to disable Pass 2 packing.
    target_chunk_tokens: int = 384
    chunk_overlap_tokens: int = 0
    structured_chunk_mode: str = "original"  # "original" or "recursive"
    paragraph_split_threshold: int = 800  # split long prose into paragraphs above this token count
    exclude_patterns: Annotated[list[str], APPEND] = Field(default_factory=list)
    # DEPRECATED: superseded by explicit ``mm init`` opt-in (provider memory dirs
    # are added directly to ``memory_dirs``). Kept as a one-shot migration trigger
    # for legacy installs — :func:`_migrate_auto_discover_once` discovers canonical
    # provider paths, appends them to ``memory_dirs``, then flips this flag to
    # False. Default stays True so existing users without an explicit value
    # still trigger migration on next startup. Will be removed in a future release.
    auto_discover: bool = True
    # When True, ``FileWatcher.start()`` walks each ``memory_dir`` once at
    # startup and indexes files the watchdog observer didn't see (the observer
    # only fires on change events from the moment it's scheduled, so files
    # that landed before start() — server was down, or the dir was newly
    # added to ``memory_dirs`` — would otherwise stay invisible).
    #
    # Default False (PR #295 lesson): an unconditional startup walk is
    # silently CPU-bound on first install — a multi-minute embed job blocks
    # the server while the user thinks it hung, the same failure mode that
    # killed the earlier startup-scan attempt. The ``mm init`` wizard's
    # opt-in ``_maybe_seed_initial_index`` is the user-driven path that
    # resolves the same gap with a visible progress bar and confirm prompt;
    # users who want the same backfill on every restart can flip this to
    # True explicitly. ``mm index <dir>`` and the web UI's per-dir Reindex
    # button cover ad-hoc indexing without flipping this — content-hash
    # dedup makes both paths idempotent.
    startup_backfill: bool = False

    # AI per-source summary (Source tab "✨ AI" preview). Disabled by default —
    # requires ``llm.enabled=true`` and a configured provider, and produces one
    # LLM call per indexed file on first run. Re-indexing skips unchanged files
    # via a content_hash signature, so steady-state cost is zero. Language drift
    # (e.g., ``summary_language="ko"`` after summaries were generated as ``"en"``)
    # is surfaced as an explicit opt-in banner in the Web UI rather than auto-
    # regenerating, since the bulk regen is the only place that would re-spend
    # the LLM budget without a content change.
    auto_summarize: bool = False
    # Output language for AI summaries — fixed string (e.g., ``"en"``, ``"ko"``)
    # rather than auto-detect to keep prompts cacheable and behaviour predictable
    # across mixed-language source corpora. Surfaced verbatim in the prompt
    # ("write the summary in <language>"); arbitrary ISO codes work.
    summary_language: str = "en"
    # Cap on bytes of source body sent to the LLM — protects against runaway
    # cost on huge files. The first ~5 chunks are joined and truncated to this
    # limit; larger documents still get a representative head-of-doc summary.
    summary_max_input_chars: int = 3000
    # Output token cap — 256 is comfortably above 2-3 sentence prose for both
    # English and Korean. Smaller values risk mid-sentence truncation.
    summary_max_tokens: int = 256

    @field_validator(
        "max_chunk_tokens",
        "min_chunk_tokens",
        "target_chunk_tokens",
        "chunk_overlap_tokens",
        "paragraph_split_threshold",
        "summary_max_input_chars",
        "summary_max_tokens",
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
        if self.target_chunk_tokens > self.max_chunk_tokens:
            raise ValueError(
                f"target_chunk_tokens ({self.target_chunk_tokens}) must be "
                f"<= max_chunk_tokens ({self.max_chunk_tokens})"
            )
        return self

    def all_index_roots(self) -> list[Path]:
        """Return every directory the indexer should treat as a root (ADR-0011).

        Single source of truth for "all index roots" — watcher, engine
        within-roots guard, exclusion match, and the Web sources status
        endpoint all consume this. Direct ``.memory_dirs`` access is
        reserved for the user-tier registry (wizard, Sources tab user-
        view, auto-discover migration); functional consumers that need
        to act on every indexable file go through this helper instead so
        a future scope addition does not fork the consumers.

        Entries are coerced to ``Path`` to honour the declared return
        type even when raw ``str`` values slipped past Pydantic via the
        non-validating ``setattr`` path in ``load_config_overrides`` —
        otherwise ``reindex_all`` blew up calling ``.expanduser()`` on
        the unwrapped JSON strings.
        """
        return [Path(d) for d in (*self.memory_dirs, *self.project_memory_dirs)]


class DecayConfig(ConfigModel):
    enabled: bool = False
    half_life_days: float = 30.0

    @field_validator("half_life_days")
    @classmethod
    def must_be_positive(cls, v: float, info: ValidationInfo) -> float:
        if v <= 0:
            raise ValueError(f"{info.field_name} must be positive, got {v}")
        return v


class MMRConfig(ConfigModel):
    enabled: bool = False
    lambda_param: float = 0.7  # 0.0=diversity max, 1.0=relevance max

    @field_validator("lambda_param")
    @classmethod
    def must_be_in_range(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("lambda_param must be between 0.0 and 1.0")
        return v


class AccessConfig(ConfigModel):
    enabled: bool = False
    max_boost: float = 1.5  # maximum score multiplier for highly accessed chunks

    @field_validator("max_boost")
    @classmethod
    def must_be_at_least_one(cls, v: float) -> float:
        if v < 1.0:
            raise ValueError("max_boost must be >= 1.0")
        return v


_NAMESPACE_MAX_LEN = 128
_ALLOWED_NS_PLACEHOLDERS: frozenset[str] = frozenset({"parent", "ancestor"})


class NamespacePolicyRule(ConfigModel):
    """Maps files matching a glob pattern to a namespace label.

    ``path_glob`` uses gitignore-style patterns (via ``pathspec.GitIgnoreSpec``).
    Leading ``~/`` is expanded at load time. Matching is case-insensitive and
    runs against the absolute resolved file path with any leading ``/``
    stripped — same semantics as ``IndexingConfig.exclude_patterns``.

    ``namespace`` supports two placeholders, both resolved against the matched
    file's path:

    - ``{parent}`` — the immediate parent folder name (equivalent to
      ``{ancestor:0}``).
    - ``{ancestor:N}`` — the folder name ``N`` levels above the immediate
      parent. ``N=0`` is the immediate parent; ``N=1`` is the grandparent,
      and so on. This lets rules for well-known memory_dir layouts (e.g.,
      ``~/.claude/projects/*/memory/**``) pick out the project id rather
      than the generic ``memory`` basename — see issue #296.

    Unknown placeholders, non-integer or negative ``ancestor`` specs are
    rejected at load time. If a placeholder would expand to an empty string
    (e.g., root of filesystem) or ``N`` exceeds the available ancestors, the
    rule is skipped at runtime and the next rule is tried.
    """

    path_glob: str
    namespace: str

    @field_validator("path_glob")
    @classmethod
    def _expand_and_validate_glob(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("path_glob must be non-empty")
        if v == "~" or v.startswith("~/"):
            # ``as_posix()`` not ``str()``: ``pathspec.GitIgnoreSpec`` is the
            # downstream consumer (engine.py:_build_exclude_spec) and gitignore
            # patterns require ``/`` separators. On Windows ``str(Path(...))``
            # would emit backslashes, which pathspec treats as escape characters
            # and silently mismatches every file.
            v = Path(v).expanduser().as_posix()
        elif Path(v).is_absolute():
            # Same normalization for already-absolute paths supplied directly
            # (e.g. a config fragment that pre-expanded ``~/`` to ``C:\...``).
            # Without this, two fragments declaring the equivalent rule via
            # different forms (``~/foo`` and ``C:\Users\me\foo``) would dedupe
            # on POSIX but not on Windows, since ``_dedup_key`` hashes the raw
            # post-validator string.
            v = Path(v).as_posix()
        return v

    @field_validator("namespace")
    @classmethod
    def _validate_namespace(cls, v: str) -> str:
        import string as _string

        v = v.strip()
        if not v:
            raise ValueError("namespace must be non-empty")
        if len(v) > _NAMESPACE_MAX_LEN:
            raise ValueError(f"namespace must be <= {_NAMESPACE_MAX_LEN} chars, got {len(v)}")
        for _lit, field_name, spec, _conv in _string.Formatter().parse(v):
            if field_name is None:
                continue
            if field_name not in _ALLOWED_NS_PLACEHOLDERS:
                raise ValueError(
                    f"unknown placeholder '{{{field_name}}}' in namespace; "
                    f"supported: {sorted(_ALLOWED_NS_PLACEHOLDERS)}"
                )
            if field_name == "parent" and spec:
                raise ValueError("{parent} does not accept a format spec; use {ancestor:N}")
            if field_name == "ancestor":
                if not spec:
                    raise ValueError("{ancestor} requires an integer index, e.g. {ancestor:1}")
                try:
                    n = int(spec)
                except ValueError as exc:
                    raise ValueError(
                        f"{{ancestor:{spec}}} index must be a non-negative integer"
                    ) from exc
                if n < 0:
                    raise ValueError(f"{{ancestor:{spec}}} index must be non-negative")
        if any(ord(c) < 32 for c in v):
            raise ValueError("namespace must not contain control characters")
        return v


class NamespaceConfig(ConfigModel):
    default_namespace: str = "default"
    enable_auto_ns: bool = False
    rules: Annotated[list[NamespacePolicyRule], APPEND] = Field(default_factory=list)


class RerankConfig(ConfigModel):
    """Cross-encoder reranker settings (Stage 3b in the search pipeline).

    Default is a lightweight English fastembed cross-encoder (~80 MB ONNX,
    local, no external service). For Korean/Chinese/Japanese/other
    non-English content set
    ``model="jinaai/jina-reranker-v2-base-multilingual"`` (1.1 GB) — the
    English default noticeably degrades non-English reranking quality.

    Provider-specific model IDs:

    - ``fastembed``: fastembed catalog ID. Supported built-ins include
      ``Xenova/ms-marco-MiniLM-L-6-v2`` (EN, 80 MB),
      ``jinaai/jina-reranker-v2-base-multilingual`` (multilingual, 1.1 GB),
      ``jinaai/jina-reranker-v1-tiny-en`` (EN, 8K context). Custom ONNX
      exports can be registered via
      ``TextCrossEncoder.add_custom_model()`` before the server starts.
    - ``cohere``: Cohere Rerank API model (e.g. ``rerank-english-v3.0``,
      ``rerank-multilingual-v3.0``). Requires ``api_key``.
    - ``local``: sentence-transformers ``CrossEncoder`` model name (e.g.
      ``cross-encoder/ms-marco-MiniLM-L-6-v2``). Requires
      ``sentence-transformers`` to be installed separately; the
      ``fastembed`` provider is usually preferable.
    """

    enabled: bool = False
    provider: str = "fastembed"  # "cohere" | "local" | "fastembed"
    model: str = "Xenova/ms-marco-MiniLM-L-6-v2"
    api_key: str = ""

    # Candidate pool (Stage 3b oversample) — the reranker sees
    # ``max(min_pool, min(max_pool, int(oversample * response_top_k)))``
    # items, then returns the caller's response top_k. Defaults give the
    # classic 2× oversample at top_k=10 (pool=20) while scaling with
    # larger requests.
    oversample: float = 2.0
    min_pool: int = 20
    max_pool: int = 200

    # Deprecated: superseded by oversample/min_pool/max_pool. Kept as a
    # field so legacy config.json and MEMTOMEM_RERANK__TOP_K env vars
    # still load without errors; ``_migrate_legacy_top_k`` rewrites it
    # to ``min_pool`` during validation. Slated for removal in a future release.
    top_k: int = 20

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_top_k(cls, data: object) -> object:
        if not isinstance(data, dict) or "top_k" not in data:
            return data
        import warnings

        if "min_pool" in data:
            warnings.warn(
                "rerank.top_k is deprecated and is ignored when rerank.min_pool "
                "is set. Remove rerank.top_k from your config. "
                "(Slated for removal in a future release.)",
                DeprecationWarning,
                stacklevel=2,
            )
            data.pop("top_k")
        else:
            warnings.warn(
                "rerank.top_k is deprecated; migrating to rerank.min_pool. "
                "Use rerank.oversample + rerank.min_pool + rerank.max_pool to "
                "scale the reranker candidate pool with the request top_k. "
                "(Slated for removal in a future release.)",
                DeprecationWarning,
                stacklevel=2,
            )
            data["min_pool"] = data.pop("top_k")
        return data

    @field_validator("top_k", "min_pool", "max_pool")
    @classmethod
    def must_be_positive(cls, v: int, info: ValidationInfo) -> int:
        if v <= 0:
            raise ValueError(f"{info.field_name} must be positive, got {v}")
        return v

    @field_validator("oversample")
    @classmethod
    def oversample_must_be_positive(cls, v: float, info: ValidationInfo) -> float:
        if v <= 0:
            raise ValueError(f"{info.field_name} must be positive, got {v}")
        return v

    @model_validator(mode="after")
    def _check_pool_bounds(self) -> "RerankConfig":
        if self.max_pool < self.min_pool:
            raise ValueError(
                f"rerank.max_pool ({self.max_pool}) must be >= rerank.min_pool ({self.min_pool})"
            )
        return self


class QueryExpansionConfig(ConfigModel):
    enabled: bool = False
    max_terms: int = 3
    strategy: str = "tags"  # "tags" | "headings" | "both" | "llm"

    @field_validator("strategy")
    @classmethod
    def valid_strategy(cls, v: str) -> str:
        if v not in ("tags", "headings", "both", "llm"):
            raise ValueError("strategy must be 'tags', 'headings', 'both', or 'llm'")
        return v


class ImportanceConfig(ConfigModel):
    enabled: bool = False
    max_boost: float = 1.5
    weights: Annotated[list[float], REPLACE] = Field(default_factory=lambda: [0.3, 0.2, 0.3, 0.2])

    @field_validator("max_boost")
    @classmethod
    def must_be_at_least_one(cls, v: float) -> float:
        if v < 1.0:
            raise ValueError("max_boost must be >= 1.0")
        return v


class WebhookConfig(ConfigModel):
    enabled: bool = False
    url: str = ""
    events: Annotated[list[str], APPEND] = Field(
        default_factory=lambda: ["add", "delete", "search"]
    )
    secret: str = ""
    timeout_seconds: float = 10.0


class ConsolidationScheduleConfig(ConfigModel):
    enabled: bool = False
    interval_hours: float = 24.0
    min_group_size: int = 3
    max_groups: int = 10


class WarmupConfig(ConfigModel):
    """Opt-in eager model loading at MCP server start (#1621).

    Default off: handshake-only sessions stay fully lazy (#399 — no
    storage open, no model import). When enabled, ``app_lifespan``
    spawns a background task that runs the full component init and
    pre-loads the local embedder/reranker models, so the first query
    after server start doesn't pay the model download/load cost.
    Remote providers (ollama, openai, cohere) are skipped — they have
    no local model to preload. Startup-only; not runtime-mutable.
    """

    enabled: bool = False


class PolicyConfig(ConfigModel):
    """Memory lifecycle policies."""

    enabled: bool = False
    scheduler_interval_minutes: float = 60.0
    max_actions_per_run: int = 100


MAX_CONTEXT_WINDOW_CHUNKS = 10  # max ±N adjacent chunks around each hit


class ContextWindowConfig(ConfigModel):
    """Context window expansion for search results (small-to-big retrieval)."""

    enabled: bool = False
    window_size: int = 2  # ±N adjacent chunks

    @field_validator("window_size")
    @classmethod
    def must_be_in_range(cls, v: int) -> int:
        if not 0 <= v <= MAX_CONTEXT_WINDOW_CHUNKS:
            raise ValueError(f"window_size must be 0-{MAX_CONTEXT_WINDOW_CHUNKS}")
        return v


class HealthWatchdogConfig(ConfigModel):
    """Periodic health monitoring and auto-maintenance."""

    enabled: bool = False
    heartbeat_interval_seconds: float = 60.0
    diagnostic_interval_seconds: float = 300.0
    deep_interval_seconds: float = 3600.0
    max_snapshots: int = 1000
    orphan_cleanup_threshold: int = 10
    auto_maintenance: bool = True


class SchedulerConfig(ConfigModel):
    """Cron scheduler for memory lifecycle jobs (P2 Phase A).

    Dispatch cadence is the health watchdog loop — this config has no
    own tick interval. Both ``scheduler.enabled`` AND
    ``health_watchdog.enabled`` must be true for schedules to fire; the
    watchdog gate wins because the dispatcher rides its loop.

    Phase A is UTC-only: ``default_timezone`` is accepted but only the
    value ``"utc"`` is honored. Other values log a warning at startup
    and fall back to UTC. Per-schedule timezone overrides are deferred
    to Phase C (RFC Open-Q-1).
    """

    enabled: bool = False
    max_concurrent_jobs: int = 1
    default_timezone: str = "utc"
    runner_timeout_seconds: float = 300.0

    @field_validator("max_concurrent_jobs")
    @classmethod
    def _max_concurrent_positive(cls, v: int, info: ValidationInfo) -> int:
        if v < 1:
            raise ValueError(f"{info.field_name} must be >= 1, got {v}")
        return v

    @field_validator("runner_timeout_seconds")
    @classmethod
    def _runner_timeout_positive(cls, v: float, info: ValidationInfo) -> float:
        if v <= 0:
            raise ValueError(f"{info.field_name} must be positive, got {v}")
        return v


class LLMConfig(ConfigModel):
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


class SessionSummaryConfig(ConfigModel):
    """Auto LLM summary on ``mem_session_end`` (RFC P1 Phase B).

    When ``auto`` is True and the closing session has at least
    ``min_chunks`` chunks added during its lifetime, the server
    generates an LLM summary and Phase A's persistence helper promotes
    it to ``archive:session:<id>``. Sessions whose serialized chunk
    body would exceed ``max_input_chars`` are skipped (the caller can
    pass an explicit ``summary=`` instead).
    """

    auto: bool = True
    min_chunks: int = 5
    max_summary_tokens: int = 500
    max_input_chars: int = 60_000
    # Cap on ``chunk_links`` rows written from the summary chunk back
    # to the source chunks it summarized (RFC Open-Question-1). Long
    # sessions otherwise emit one row per chunk; we keep the newest
    # ``max_summary_links`` (chunks arrive newest first, tail dropped).
    max_summary_links: int = 50
    # Phase C — Stage-1 query-expansion enrichment. After standard
    # expansion, the pipeline runs a small lookup against
    # ``archive:session:*`` and, for any summary chunk scoring above
    # ``expansion_score_threshold``, follows ``chunk_links`` of type
    # ``summarizes`` back to the source files of the summarized session.
    # Those files become a "rescue leg" — a parallel BM25+dense retrieval
    # restricted to those source paths and merged into RRF as a third
    # input list weighted by ``expansion_rescue_weight``. This brings
    # past-session chunks into ranking contention without changing the
    # storage primitive signatures.
    expansion_lookup_top_k: int = 3
    expansion_score_threshold: float = 0.3
    expansion_rescue_weight: float = 0.5

    @field_validator("min_chunks")
    @classmethod
    def min_chunks_positive(cls, v: int, info: ValidationInfo) -> int:
        if v <= 0:
            raise ValueError(f"{info.field_name} must be positive, got {v}")
        return v

    @field_validator(
        "max_summary_tokens",
        "max_input_chars",
        "max_summary_links",
        "expansion_lookup_top_k",
    )
    @classmethod
    def positive_int(cls, v: int, info: ValidationInfo) -> int:
        if v <= 0:
            raise ValueError(f"{info.field_name} must be positive, got {v}")
        return v

    @field_validator("expansion_score_threshold", "expansion_rescue_weight")
    @classmethod
    def non_negative_float(cls, v: float, info: ValidationInfo) -> float:
        if v < 0:
            raise ValueError(f"{info.field_name} must be non-negative, got {v}")
        return v


TargetScope = Literal["user", "project_shared", "project_local"]


class HooksConfig(ConfigModel):
    """Settings-hooks fan-out scope (ADR-0010 §3).

    ``target_scope`` selects where memtomem-managed Claude Code hooks land:
    user-tier (``~/.claude/settings.json``), project-shared
    (``<project>/.claude/settings.json``), or project-local
    (``<project>/.claude/settings.local.json``). v1 default is ``user`` for
    zero behavior change; the default-flip trigger lives in ADR-0010 §5.
    """

    target_scope: TargetScope = "user"


class ContextGatewayConfig(ConfigModel):
    """Settings for the multi-project context UI (skills / commands / agents).

    See ``memtomem-docs/memtomem/planning/multi-project-context-ui-rfc.md`` —
    PR2 lands the read-only fields (``known_projects_path``,
    ``experimental_claude_projects_scan``).
    """

    # Where ``Add Project`` registrations are persisted. Sibling to the
    # mem2mem config home so any installed user already has the parent.
    known_projects_path: Path = Path("~/.memtomem/known_projects.json")
    # Opt-in reverse-decode of ``~/.claude/projects/<encoded>`` directory
    # names. Off by default — the encoding is fragile around dash-containing
    # paths, so this is gated behind explicit consent (RFC §Decision 2). When
    # True the scan is *unfiltered* (the legacy escape hatch).
    experimental_claude_projects_scan: bool = False
    # Filtered auto-display of ~/.claude/projects/ scan candidates: surfaces a
    # discovered project only when its root carries a recognized runtime marker
    # (.claude/.gemini/.codex/.agents/.kimi/.memtomem). On by default — this is
    # the auto-display source. Independent of experimental_claude_projects_scan
    # (which, when True, widens the same scan to unfiltered candidates). Added
    # as a new field rather than flipping the experimental flag so no env var
    # name changes (no validation_alias infra) and the unfiltered default is
    # untouched.
    auto_display_configured_projects: bool = True


class SessionTraceConfig(ConfigModel):
    """Configuration for session command execution tracing."""

    enabled: bool = False
    jsonl_enabled: bool = True
    jsonl_path: Path = Path("~/.memtomem/traces/session-traces.jsonl")
    langfuse_enabled: bool = False
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = ""
    sampling_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    payload_mode: Literal["metadata", "redacted", "full"] = "metadata"
    max_payload_chars: int = Field(default=10000, gt=0)

    @model_validator(mode="after")
    def _require_keys_when_enabled(self) -> "SessionTraceConfig":
        if (
            self.enabled
            and self.langfuse_enabled
            and not (self.langfuse_public_key and self.langfuse_secret_key)
            # Deliberate, narrow exception to the MEMTOMEM_-only env surface
            # (#1522): the Langfuse SDK's own documented variables count as
            # credentials, so a standard Langfuse env setup keeps working.
            # ``get_langfuse_client`` omits kwargs for empty fields and lets
            # the SDK read these itself — the values are never copied into
            # the config object, so they cannot leak into ``config.json``
            # or any config-display surface. Credentials only: activation
            # still requires the explicit ``langfuse_enabled`` opt-in above;
            # ``LANGFUSE_ENABLED`` alone can never turn tracing on.
            and not (
                os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")
            )
        ):
            raise ValueError(
                "SessionTraceConfig.langfuse_enabled=true requires langfuse_public_key and "
                "langfuse_secret_key — set them in config "
                "(MEMTOMEM_SESSION_TRACE__LANGFUSE_PUBLIC_KEY / "
                "MEMTOMEM_SESSION_TRACE__LANGFUSE_SECRET_KEY) or export the Langfuse SDK's "
                "LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY."
            )
        return self

    @model_validator(mode="after")
    def _require_langfuse_package_when_enabled(self) -> "SessionTraceConfig":
        if self.enabled and self.langfuse_enabled:
            from importlib.util import find_spec

            if find_spec("langfuse") is None:
                raise ValueError(
                    "SessionTraceConfig.langfuse_enabled=true but the 'langfuse' package is not "
                    "installed. Install the langfuse extra "
                    "(e.g. `pip install 'memtomem[langfuse]'`)."
                )
        return self


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
    warmup: WarmupConfig = Field(default_factory=WarmupConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    context_window: ContextWindowConfig = Field(default_factory=ContextWindowConfig)
    health_watchdog: HealthWatchdogConfig = Field(default_factory=HealthWatchdogConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    session_summary: SessionSummaryConfig = Field(default_factory=SessionSummaryConfig)
    context_gateway: ContextGatewayConfig = Field(default_factory=ContextGatewayConfig)
    hooks: HooksConfig = Field(default_factory=HooksConfig)
    session_trace: SessionTraceConfig = Field(default_factory=SessionTraceConfig)


# ---------------------------------------------------------------------------
# Canonical mutable-field definitions and validation
# ---------------------------------------------------------------------------
# Single source of truth for which config fields can be modified at runtime
# via CLI (``mm config set``), Web UI (``PATCH /api/config``), and MCP
# (``mem_config``).  All three paths import from here.


def _validate_exclude_patterns(value: object) -> None:
    """Reject empty strings, duplicates, and malformed pathspec patterns.

    ``pathspec.GitIgnoreSpec.from_lines`` raises ``GitIgnorePatternError`` on
    patterns like ``!`` or ``\\`` that would otherwise only surface at indexing
    time. Run the same parse eagerly so CLI/MCP/web all fail fast with the
    parser error instead of silently accepting bad input.
    """
    import pathspec
    from pathspec.patterns.gitwildmatch import GitWildMatchPatternError

    if not isinstance(value, list):
        raise ValueError("exclude_patterns must be a list")

    seen: set[str] = set()
    for idx, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"exclude_patterns[{idx}]: empty pattern")
        if item in seen:
            raise ValueError(f"exclude_patterns[{idx}]: duplicate pattern {item!r}")
        seen.add(item)
        try:
            pathspec.GitIgnoreSpec.from_lines([item.lower()])
        except GitWildMatchPatternError as exc:
            raise ValueError(f"exclude_patterns[{idx}]: {exc}") from exc


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
        "target_chunk_tokens",
        "chunk_overlap_tokens",
        "structured_chunk_mode",
        "exclude_patterns",
        "auto_discover",
        "supported_extensions",
        "auto_summarize",
        "summary_language",
        "summary_max_input_chars",
        "summary_max_tokens",
    },
    "embedding": {"batch_size", "progress_threshold"},
    "decay": {"enabled", "half_life_days"},
    "mmr": {"enabled", "lambda_param"},
    "namespace": {"default_namespace", "enable_auto_ns", "rules"},
    # Runtime-mutable via ``PATCH /api/config``: only the pool-sizing knobs
    # plus the on/off toggle. ``provider``/``model``/``api_key`` are kept
    # off the PATCH surface because a credential or backend swap should
    # carry intent (and the live reranker instance is otherwise cached on
    # startup). Disk hot-reload (``apply_runtime_config_changes`` →
    # ``_sync_reranker`` in ``web/hot_reload.py``) *does* rebuild the live
    # reranker on any field change, since editing the YAML already implies
    # operator intent — so the asymmetry is by design.
    "rerank": {"enabled", "oversample", "min_pool", "max_pool"},
    "hooks": {"target_scope"},
    "session_trace": {
        "enabled",
        "jsonl_enabled",
        "jsonl_path",
        "langfuse_enabled",
        "langfuse_public_key",
        "langfuse_secret_key",
        "langfuse_host",
        "sampling_rate",
        "payload_mode",
        "max_payload_chars",
    },
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
    "indexing.target_chunk_tokens": {"type": int, "min": 0, "max": 8192},
    "indexing.chunk_overlap_tokens": {"type": int, "min": 0, "max": 512},
    "indexing.structured_chunk_mode": {"type": str, "allowed": {"original", "recursive"}},
    "indexing.exclude_patterns": {
        "type": list,
        "item_type": str,
        "validator": _validate_exclude_patterns,
    },
    "indexing.auto_discover": {"type": bool},
    "indexing.auto_summarize": {"type": bool},
    "indexing.summary_language": {"type": str},
    "indexing.summary_max_input_chars": {"type": int, "min": 200, "max": 50000},
    "indexing.summary_max_tokens": {"type": int, "min": 32, "max": 2048},
    "embedding.batch_size": {"type": int, "min": 1, "max": 1024},
    "embedding.progress_threshold": {"type": int, "min": 0, "max": 100000},
    "decay.enabled": {"type": bool},
    "decay.half_life_days": {"type": float, "min": 0.1},
    "mmr.enabled": {"type": bool},
    "mmr.lambda_param": {"type": float, "min": 0.0, "max": 1.0},
    "search.rrf_weights": {"type": list, "item_type": float, "length": 2},
    "namespace.default_namespace": {"type": str},
    "namespace.enable_auto_ns": {"type": bool},
    "namespace.rules": {"type": list, "item_type": NamespacePolicyRule},
    "rerank.enabled": {"type": bool},
    "rerank.oversample": {"type": float, "min": 0.1, "max": 10.0},
    "rerank.min_pool": {"type": int, "min": 1, "max": 1000},
    "rerank.max_pool": {"type": int, "min": 1, "max": 1000},
    "hooks.target_scope": {"type": str, "allowed": set(get_args(TargetScope))},
    "session_trace.enabled": {"type": bool},
    "session_trace.jsonl_enabled": {"type": bool},
    "session_trace.jsonl_path": {"type": str},
    "session_trace.langfuse_enabled": {"type": bool},
    "session_trace.langfuse_public_key": {"type": str},
    "session_trace.langfuse_secret_key": {"type": str},
    "session_trace.langfuse_host": {"type": str},
    "session_trace.sampling_rate": {"type": float, "min": 0.0, "max": 1.0},
    "session_trace.payload_mode": {"type": str, "allowed": {"metadata", "redacted", "full"}},
    "session_trace.max_payload_chars": {"type": int, "min": 1},
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
        # ``list[BaseModel]`` (e.g. ``namespace.rules``): accept a JSON
        # string or list of dicts/model instances and validate each entry
        # via ``model_validate``. Mirrors ``load_config_d``'s APPEND
        # coercion so mutation paths (PATCH /api/config, mm config set)
        # stay in sync with the load path.
        if isinstance(item_type, type) and issubclass(item_type, BaseModel):
            if isinstance(value, str):
                import json as _json

                try:
                    parsed = _json.loads(value)
                except _json.JSONDecodeError as exc:
                    raise ValueError(f"cannot parse JSON: {exc}") from exc
            else:
                parsed = value
            if not isinstance(parsed, list):
                raise ValueError(
                    f"cannot convert {type(parsed).__name__} to list[{item_type.__name__}]"
                )
            coerced_items: list[object] = []
            for idx, item in enumerate(parsed):
                if isinstance(item, item_type):
                    coerced_items.append(item)
                elif isinstance(item, dict):
                    try:
                        coerced_items.append(item_type.model_validate(item))
                    except Exception as exc:
                        raise ValueError(f"item[{idx}]: {exc}") from exc
                else:
                    raise ValueError(
                        f"item[{idx}]: expected dict or {item_type.__name__}, "
                        f"got {type(item).__name__}"
                    )
            coerced = coerced_items
            if expected_len is not None and len(coerced) != expected_len:
                raise ValueError(f"expected length {expected_len}, got {len(coerced)}")
        else:
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

    validator = constraint.get("validator")
    if callable(validator):
        validator(coerced)

    return coerced


# ---------------------------------------------------------------------------
# Config persistence: ~/.memtomem/config.json override layer
# ---------------------------------------------------------------------------

_CONFIG_OVERRIDE_PATH = Path("~/.memtomem/config.json")


def _override_path() -> Path:
    return _CONFIG_OVERRIDE_PATH.expanduser()


def load_config_overrides(config: Mem2MemConfig, *, migrate: bool = True) -> None:
    """Apply persisted overrides from ~/.memtomem/config.json (if exists).

    Precedence: ``MEMTOMEM_<SECTION>__<FIELD>`` env vars win over
    ``config.json``. If an env var is set for a field, the corresponding
    ``config.json`` entry is skipped so the env-bound value remains in effect.

    Pass ``migrate=False`` to skip the auto-discover legacy migration —
    required for read-only diagnostic surfaces (e.g. ``mm context detect``,
    scope resolution from config) that must not touch disk as a side
    effect (see ``feedback_doctor_no_migration_loader``).
    """
    import json as _json
    import logging
    import os
    import warnings

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
        # Snapshot the pre-override section so a cross-field validation
        # failure below can roll the whole section back to its known-good
        # baseline (defaults + any config.d values already applied).
        section_before = section_obj.model_copy(deep=True)
        applied_keys: set[str] = set()
        for key, value in updates.items():
            if hasattr(section_obj, key):
                env_var = f"MEMTOMEM_{section_name.upper()}__{key.upper()}"
                if env_var in os.environ:
                    _log.debug(
                        "Skipping %s.%s from %s: %s is set in environment (env wins)",
                        section_name,
                        key,
                        path,
                        env_var,
                    )
                    continue
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
                    applied_keys.add(key)
                except (TypeError, ValueError) as exc:
                    _log.warning(
                        "Skipping invalid config override %s.%s=%r: %s",
                        section_name,
                        key,
                        value,
                        exc,
                    )
        # ``ConfigModel`` sub-configs don't set ``validate_assignment=True``, so
        # the ``@model_validator(mode="after")`` cross-field checks never re-run
        # for the ``setattr`` overrides above. Re-validate the assembled section
        # so an invariant the validator was written to reject (e.g.
        # ``session_trace.langfuse_enabled`` with no keys, or
        # ``min_chunk_tokens > max_chunk_tokens``) fails loudly instead of
        # slipping through, and re-surface any deprecation the user's own config
        # triggers. On failure, restore the pre-override baseline — cross-field
        # invariants are about field *combinations*, so partial retention isn't
        # meaningful; fall back to known-good as a whole.
        #
        # This is deliberately a *check*, not a rebuild: we do not assign the
        # coerced model back. ``model_validate`` coerces every field to its
        # declared type (e.g. a ``config.json`` string ``sqlite_path`` ->
        # ``Path``), which would diverge from the raw values the ``setattr``
        # path stores and change ``str()`` output cross-platform. That
        # normalization is out of scope here; this pass only enforces validity
        # and surfaces warnings.
        if applied_keys:
            # ``exclude_defaults`` keeps *untouched* defaulted legacy fields
            # (e.g. ``rerank.top_k``) out of the payload so they don't spuriously
            # re-fire their ``mode="before"`` migration; the ``applied_keys``
            # overlay adds back the keys the user actually set even when their
            # value equals the default, so an explicitly-configured deprecated
            # field still surfaces its warning. ``catch_warnings(record=True)``
            # forces ``simplefilter("always")`` so an ambient ``-W error`` can't
            # turn this internal pass into a crash; captured deprecations are
            # re-emitted via the logger rather than swallowed.
            dumped = section_obj.model_dump()
            payload = section_obj.model_dump(exclude_defaults=True)
            payload.update({k: dumped[k] for k in applied_keys if k in dumped})
            try:
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter("always")
                    type(section_obj).model_validate(payload)
            except ValidationError as exc:
                _log.warning(
                    "Invalid config section [%s] in %s: %s (reverting section to defaults)",
                    section_name,
                    path,
                    exc,
                )
                setattr(config, section_name, section_before)
            else:
                for w in caught:
                    # The captured message may describe an auto-migration (e.g.
                    # "migrating rerank.top_k to min_pool"), but this validation
                    # pass does not persist it — the config.json value is used as
                    # set. Clarify so the operator updates the config themselves.
                    _log.warning(
                        "Config %s [%s] in %s: %s (config.json value is applied "
                        "as set and not auto-rewritten — update it to the "
                        "replacement field named above)",
                        w.category.__name__,
                        section_name,
                        path,
                        w.message,
                    )

    # One-shot migration of legacy auto_discover=True installs to explicit
    # provider memory_dirs entries. No-op for fresh installs (no config.json)
    # and for already-migrated installs (auto_discover=False). Skipped when
    # ``migrate=False`` so read-only callers don't trigger a disk write.
    if migrate:
        _migrate_auto_discover_once(config)


_CONFIG_D_PATH = Path("~/.memtomem/config.d")


def _config_d_path() -> Path:
    return _CONFIG_D_PATH.expanduser()


def _merge_strategy_for(section_cls: type, field_name: str) -> MergeStrategy | None:
    """Return the ``MergeStrategy`` annotated on a field, or None if scalar."""
    info = (
        section_cls.model_fields.get(field_name) if hasattr(section_cls, "model_fields") else None
    )
    if info is None:
        return None
    for m in info.metadata:
        if isinstance(m, MergeStrategy):
            return m
    return None


def _list_item_type(section_cls: type, field_name: str) -> type | None:
    """Return the element type of a ``list[X]`` field, or ``None`` for scalars.

    Used by the fragment loader to coerce raw JSON dicts into ``BaseModel``
    instances before APPEND dedup, since ``setattr`` on a non-validating
    model won't re-validate the assigned list.
    """
    import typing

    info = (
        section_cls.model_fields.get(field_name) if hasattr(section_cls, "model_fields") else None
    )
    if info is None:
        return None
    args = typing.get_args(info.annotation)
    if not args:
        return None
    item = args[0]
    return item if isinstance(item, type) else None


def _dedup_key(item: object) -> object:
    """Stable equality key for APPEND dedup.

    Normalises Path to its string form and dict/BaseModel to a recursively
    sorted tuple form so that ``list[dict]`` and ``list[BaseModel]`` fields
    (e.g. ``NamespaceConfig.rules``) can be deduped across a native default
    list and raw JSON fragment entries.
    """
    if isinstance(item, Path):
        return str(item)
    if isinstance(item, BaseModel):
        return _dedup_key(item.model_dump(mode="json"))
    if isinstance(item, dict):
        return tuple(sorted((k, _dedup_key(v)) for k, v in item.items()))
    if isinstance(item, list):
        return tuple(_dedup_key(x) for x in item)
    return item


def load_config_d(config: Mem2MemConfig, *, quiet: bool = False) -> None:
    """Apply fragments from ``~/.memtomem/config.d/*.json`` (if dir exists).

    Intended for administrator-managed or external integration fragments that
    need a reversible layer. ``mm init`` itself writes ``config.json`` and
    does not create client fragments. Each fragment is a partial
    ``Mem2MemConfig`` JSON. Fragments are applied in lexicographic filename
    order. For each field:

    - If ``MEMTOMEM_<SECTION>__<FIELD>`` env var is set → skip (env wins).
    - If scalar → last fragment wins.
    - If ``list[*]`` with ``APPEND`` strategy → values concatenated,
      duplicates removed (first-seen order preserved).
    - If ``list[*]`` with ``REPLACE`` strategy → last fragment wins; prior
      list (incl. defaults) is discarded.

    ``~/.memtomem/config.json`` is a separate layer applied *after* fragments
    (see ``load_config_overrides``); that file remains a full REPLACE-on-set
    for every field so the ``mm init`` wizard keeps unambiguous user-override
    semantics.

    ``quiet=True`` suppresses *warning* output only — used by
    ``build_comparand`` which calls this on every save and would
    otherwise repeat "malformed fragment" / "unknown section" messages
    for every PATCH. Exceptions that represent real errors still raise
    (pydantic validation etc. are already caught + logged here, not
    raised, so this toggle is purely about log noise).
    """
    import json as _json
    import logging
    import os

    _log = logging.getLogger(__name__)

    def _warn(msg: str, *args: object) -> None:
        if not quiet:
            _log.warning(msg, *args)

    dir_ = _config_d_path()
    if not dir_.is_dir():
        return

    fragments = sorted(p for p in dir_.iterdir() if p.is_file() and p.suffix == ".json")
    for path in fragments:
        try:
            data = _json.loads(path.read_text(encoding="utf-8"))
        except (OSError, _json.JSONDecodeError) as exc:
            _warn("Failed to read config fragment %s: %s", path, exc)
            continue
        if not isinstance(data, dict):
            _warn("Config fragment %s is not a JSON object (ignored)", path)
            continue
        for section_name, updates in data.items():
            section_obj = getattr(config, section_name, None)
            if section_obj is None or not isinstance(updates, dict):
                if section_obj is None and isinstance(updates, dict):
                    _warn("Unknown config section '%s' in %s (ignored)", section_name, path)
                continue
            section_cls = type(section_obj)
            for key, value in updates.items():
                if not hasattr(section_obj, key):
                    continue
                env_var = f"MEMTOMEM_{section_name.upper()}__{key.upper()}"
                if env_var in os.environ:
                    _log.debug(
                        "Skipping %s.%s from %s: %s is set (env wins)",
                        section_name,
                        key,
                        path,
                        env_var,
                    )
                    continue
                strategy = _merge_strategy_for(section_cls, key)
                if strategy is not None and strategy.mode == "append":
                    if not isinstance(value, list):
                        _warn(
                            "Expected list for %s.%s in %s (got %s); skipping",
                            section_name,
                            key,
                            path,
                            type(value).__name__,
                        )
                        continue
                    current = list(getattr(section_obj, key))
                    item_type = _list_item_type(section_cls, key)
                    coerce = (
                        item_type
                        if item_type is not None
                        and isinstance(item_type, type)
                        and issubclass(item_type, BaseModel)
                        else None
                    )
                    seen = {_dedup_key(x) for x in current}
                    for item in value:
                        if coerce is not None and isinstance(item, dict):
                            try:
                                item = coerce.model_validate(item)
                            except Exception as exc:
                                _warn(
                                    "Skipping invalid %s.%s entry in %s: %s",
                                    section_name,
                                    key,
                                    path,
                                    exc,
                                )
                                continue
                        k = _dedup_key(item)
                        if k not in seen:
                            current.append(item)
                            seen.add(k)
                    try:
                        setattr(section_obj, key, current)
                    except (TypeError, ValueError) as exc:
                        _warn(
                            "Skipping invalid fragment merge %s.%s from %s: %s",
                            section_name,
                            key,
                            path,
                            exc,
                        )
                else:
                    try:
                        setattr(section_obj, key, value)
                    except (TypeError, ValueError) as exc:
                        _warn(
                            "Skipping invalid fragment value %s.%s=%r from %s: %s",
                            section_name,
                            key,
                            value,
                            path,
                            exc,
                        )


# Typed vocabulary for provider-dir classification. ``ProviderCategory``
# enumerates every category a ``memory_dir`` can be classified as;
# ``ProviderName`` enumerates every vendor tag attached to a category.
# Both are the *single source of truth* — ``_VALID_PROVIDER_CATEGORIES``
# and ``_VALID_PROVIDERS`` are derived at module load via ``get_args``
# so the frozensets can never drift from the ``Literal`` types mypy
# sees at call sites. See RFC #304 Phase 1.
ProviderCategory = Literal["user", "claude-memory", "claude-plans", "codex"]
ProviderName = Literal["user", "claude", "openai"]

# Two-bucket classification on top of ``ProviderCategory`` so the Web UI's
# Sources page can split entries between agent/user memory and arbitrary
# RAG-style folders. ``categorize_memory_dir`` already separates known
# provider layouts from the catch-all ``"user"`` bucket; this further
# splits ``"user"`` by a path-segment heuristic so a folder named
# ``~/memories/`` or ``~/Documents/memory/`` lands with the agent memory
# instead of the general indexed folders. Heuristic — pattern-only, no
# persisted override.
MemoryDirKind = Literal["memory", "general"]

# Path segments that, when present anywhere in a ``user``-category dir,
# flip its kind to ``"memory"``. Frozen so the test fixture and runtime
# both walk the same set. Edits here are user-visible classification
# changes — bump the unit-test coverage at the same time.
_USER_MEMORY_SEGMENTS: frozenset[str] = frozenset({"memory", "memories"})

# Single source of truth for provider-dir classification. Each row ties a
# category name to the regex that recognises paths in that category. The
# Web UI's ``/api/memory-dirs/status`` response carries the resulting
# ``category`` field so the client does not maintain a parallel regex.
_PROVIDER_CATEGORY_PATTERNS: tuple[tuple[ProviderCategory, re.Pattern[str]], ...] = (
    ("claude-memory", re.compile(r"/\.claude/projects/[^/]+/memory/?$")),
    ("claude-plans", re.compile(r"/\.claude/plans/?$")),
    ("codex", re.compile(r"/\.codex/memories/?$")),
)

# Derived from ``ProviderCategory`` — do NOT edit independently. Adding a
# new category means adding it to the ``Literal`` above; the frozenset
# (and mypy's exhaustiveness checking at call sites) picks it up for
# free. Until RFC #304 decides a deeper hierarchy, any change here
# requires a coordinated update to the pattern table + pin tests.
# Mirrors the ``_VALID_PRESET_PLACEHOLDERS`` pattern in ``cli/init_cmd.py``.
_VALID_PROVIDER_CATEGORIES: frozenset[str] = frozenset(get_args(ProviderCategory))

_VOCABULARY_LOCK_MESSAGE = (
    "Provider category patterns changed without updating the "
    "ProviderCategory Literal. See RFC #304 before adding categories."
)

assert ({cat for cat, _ in _PROVIDER_CATEGORY_PATTERNS} | {"user"}) == _VALID_PROVIDER_CATEGORIES, (
    _VOCABULARY_LOCK_MESSAGE
)

# Vendor tag for each category. Exposed on ``memory_dir_stats()`` entries so
# the Web UI can render a two-level vendor → product tree without duplicating
# the category→vendor map in JS. RFC #304 Phase 1 — see plan #314 resolution.
_CATEGORY_TO_PROVIDER: dict[ProviderCategory, ProviderName] = {
    "user": "user",
    "claude-memory": "claude",
    "claude-plans": "claude",
    "codex": "openai",
}

# Derived from ``ProviderName`` — same discipline as ``_VALID_PROVIDER_CATEGORIES``.
_VALID_PROVIDERS: frozenset[str] = frozenset(get_args(ProviderName))

_PROVIDER_VOCABULARY_LOCK_MESSAGE = (
    "Provider vocabulary changed without updating _VALID_PROVIDERS. "
    "See RFC #304 before adding providers."
)

# Distinct message for the key-axis drift: when this assert fires the
# category vocabulary itself is fine — what's out of sync is the tag
# mapping, so point the contributor at the right file instead of
# re-reading ``_VALID_PROVIDER_CATEGORIES``.
_CATEGORY_TO_PROVIDER_KEY_DRIFT_MESSAGE = (
    "_CATEGORY_TO_PROVIDER keys out of sync with _VALID_PROVIDER_CATEGORIES. "
    "Add or remove the matching key in _CATEGORY_TO_PROVIDER. See RFC #304."
)

# Paired asserts: keys mirror the category vocabulary, values mirror the
# provider vocabulary. Without the value-side lock a future
# ``_CATEGORY_TO_PROVIDER["skills"] = "anthropic"`` would add a new provider
# silently; #313 locks the category axis only.
assert set(_CATEGORY_TO_PROVIDER.keys()) == _VALID_PROVIDER_CATEGORIES, (
    _CATEGORY_TO_PROVIDER_KEY_DRIFT_MESSAGE
)
assert set(_CATEGORY_TO_PROVIDER.values()) == _VALID_PROVIDERS, _PROVIDER_VOCABULARY_LOCK_MESSAGE

# Derived from ``_PROVIDER_CATEGORY_PATTERNS`` — do NOT edit independently.
# Add a new pattern row above and this tuple picks it up automatically.
# Excludes ``"user"`` by design (user dirs have no pattern; they fall
# through :func:`categorize_memory_dir`'s default).
PROVIDER_DIR_CATEGORIES: tuple[ProviderCategory, ...] = tuple(
    cat for cat, _ in _PROVIDER_CATEGORY_PATTERNS
)


def provider_for_category(category: str) -> ProviderName:
    """Return the vendor tag for a ``memory_dir`` category.

    Consumed by :func:`~memtomem.indexing.engine.memory_dir_stats` so the
    Web UI can group entries by vendor. Unknown categories fall back to
    ``"user"`` — mirrors :func:`categorize_memory_dir`'s user-default.
    Accepts ``str`` (not ``ProviderCategory``) so callers can pass
    server-supplied strings without narrowing first.
    """
    return _CATEGORY_TO_PROVIDER.get(cast(ProviderCategory, category), "user")


def categorize_memory_dir(path: str | Path) -> ProviderCategory:
    """Return the category for a ``memory_dir`` path.

    Returns one of ``ProviderCategory``'s literal values, defaulting to
    ``"user"`` for anything that doesn't match a known provider layout.
    Classification only — does not check existence or validity. Matching
    normalizes separators to forward slashes first, so POSIX, Windows,
    UNC, and mixed-separator strings hit the same provider patterns.
    """
    s = str(path).replace("\\", "/").rstrip("/")
    for cat, pat in _PROVIDER_CATEGORY_PATTERNS:
        if pat.search(s):
            return cat
    return "user"


# ── Provider index-file conventions ─────────────────────────────────
# Per-category rules for the agent-managed index/TOC file that lives
# inside a memory dir. ``index_file`` is the table-of-contents the agent
# maintains and loads into its context each session (e.g. Claude Code's
# ``MEMORY.md``). ``exclude_filenames`` are meta files that must never be
# indexed as searchable content: the TOC is just pointers, so indexing it
# surfaces a high-score duplicate on every query, and READMEs are
# how-to-read meta. Centralized here (previously hardcoded in
# ``cli/ingest_cmd.py``) so EVERY index path — ``mm ingest``, the general
# engine walk, the file watcher, and ``mm purge`` — honors one set.
@dataclass(frozen=True)
class ProviderIndexConvention:
    """Index-file convention for a single :data:`ProviderCategory`."""

    index_file: str | None
    exclude_filenames: frozenset[str]


_PROVIDER_INDEX_CONVENTIONS: dict[ProviderCategory, ProviderIndexConvention] = {
    "user": ProviderIndexConvention(index_file=None, exclude_filenames=frozenset()),
    "claude-memory": ProviderIndexConvention(
        index_file="MEMORY.md",
        exclude_filenames=frozenset({"MEMORY.md", "README.md"}),
    ),
    "claude-plans": ProviderIndexConvention(index_file=None, exclude_filenames=frozenset()),
    "codex": ProviderIndexConvention(index_file=None, exclude_filenames=frozenset({"README.md"})),
}

# Lock: every category must declare a convention. Mirrors the
# ``_PROVIDER_CATEGORY_PATTERNS`` vocabulary lock above — adding a category
# to the ``ProviderCategory`` Literal without a convention here trips at
# import, not silently at the first index of the new provider's dir.
_INDEX_CONVENTION_LOCK_MESSAGE = (
    "_PROVIDER_INDEX_CONVENTIONS keys out of sync with ProviderCategory. "
    "Add the matching convention when adding a category. See RFC #304."
)
assert set(_PROVIDER_INDEX_CONVENTIONS.keys()) == _VALID_PROVIDER_CATEGORIES, (
    _INDEX_CONVENTION_LOCK_MESSAGE
)


def index_excluded_filenames(category: str) -> frozenset[str]:
    """Filenames never indexed as content for a ``memory_dir`` *category*.

    Accepts ``str`` (not ``ProviderCategory``) so callers can pass the
    result of :func:`categorize_memory_dir` without narrowing. Unknown
    categories return the empty set (index everything).
    """
    conv = _PROVIDER_INDEX_CONVENTIONS.get(cast(ProviderCategory, category))
    return conv.exclude_filenames if conv else frozenset()


def provider_index_file(category: str) -> str | None:
    """The agent-managed index/TOC filename for *category*, or ``None``.

    Companion to :func:`index_excluded_filenames` — consumed by the
    memory-index doctor to locate the per-provider hot-cache index.
    """
    conv = _PROVIDER_INDEX_CONVENTIONS.get(cast(ProviderCategory, category))
    return conv.index_file if conv else None


# Pattern matchers for project-tier scope dirs. ``project_local`` MUST
# precede ``project_shared`` here because ``.local`` is a strict suffix
# of ``memories`` after the leading-dir match — match the more specific
# pattern first. Anchors require the directory to live directly under a
# ``.memtomem/`` ancestor; nested paths like
# ``<X>/foo/.memtomem/memories/bar.md`` still classify correctly because
# we walk up from ``<X>/foo`` to its ``.memtomem`` ancestor.
_PROJECT_SCOPE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("project_local", re.compile(r"/\.memtomem/memories\.local(?:/|$)")),
    ("project_shared", re.compile(r"/\.memtomem/memories(?:/|$)")),
)


def classify_scope(
    path: str | Path,
    project_memory_dirs: Iterable[Path | str] | None = None,
) -> tuple[str, Path | None]:
    """Classify ``path`` into a scope tier and project root (ADR-0011).

    Returns ``(scope, project_root)`` where:

    - ``scope`` is one of ``"user"`` (default) / ``"project_shared"`` /
      ``"project_local"`` (matches ``TargetScope`` from ADR-0010).
    - ``project_root`` is the absolute ``<X>`` such that the path lives
      under ``<X>/.memtomem/...``, or ``None`` for user scope.

    Matching is path-pattern-based; the path is **not** required to exist.
    Caller passes the configured ``project_memory_dirs`` so we can refuse
    to classify a project_* path that has not been registered (defense
    against unregistered project trees being silently picked up by the
    indexer). When ``project_memory_dirs`` is ``None``, registration is
    not enforced — used by lower-level callers that already gate on the
    config field separately.
    """
    s = str(path).replace("\\", "/").rstrip("/")
    for scope_name, pat in _PROJECT_SCOPE_PATTERNS:
        m = pat.search(s)
        if m is None:
            continue
        # Project root is everything before ``/.memtomem/...``.
        memtomem_idx = m.start()
        if memtomem_idx <= 0:
            continue
        project_root = Path(str(path)[:memtomem_idx])
        if project_memory_dirs is not None:
            registered_roots = {Path(str(d)).expanduser().resolve() for d in project_memory_dirs}
            try:
                resolved_path = Path(str(path)).expanduser().resolve()
            except OSError:
                resolved_path = Path(str(path))
            # The path qualifies if any registered project_memory_dir is
            # an ancestor (the registered dir is the ``memories[/.local]``
            # subdir, so ``path`` must live at or under it).
            if not any(
                resolved_path == r or _is_strictly_under(resolved_path, r) for r in registered_roots
            ):
                return ("user", None)
        return (scope_name, project_root)
    return ("user", None)


def _is_strictly_under(child: Path, parent: Path) -> bool:
    """Return True iff ``child`` is at or below ``parent`` (resolved both)."""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def memory_dir_kind(path: str | Path) -> MemoryDirKind:
    """Classify a ``memory_dir`` as ``"memory"`` vs ``"general"``.

    Splits the configured dir list into two semantic buckets so the Web
    UI Sources page can show agent/user memory and arbitrary indexed
    folders in separate views without persisting a side table. Layered
    on top of :func:`categorize_memory_dir`:

    - Any non-``"user"`` category (claude-memory, claude-plans, codex…)
      is unambiguously memory.
    - A ``"user"``-category dir flips to memory when any path segment is
      ``"memory"`` or ``"memories"`` — covers the common
      ``~/memories/`` layout. Segment match is case-insensitive so
      ``/Users/X/Memories`` (which exists on macOS' case-insensitive
      APFS) classifies the same as ``/Users/X/memories``. Heuristic, so
      it intentionally over-includes paths like
      ``~/Library/Caches/<app>/memory/`` or ``~/projects/foo/memory/``;
      the cost is showing them in the Memory view instead of General.
      Future PR can add an explicit ``memory_dirs[i].kind`` override on
      top.
    """
    if categorize_memory_dir(path) != "user":
        return "memory"
    parts = Path(str(path)).parts
    if any(p.lower() in _USER_MEMORY_SEGMENTS for p in parts):
        return "memory"
    return "general"


def _detect_provider_dirs() -> dict[str, list[Path]]:
    """Group canonical provider memory dirs by category for wizard prompting.

    Each category maps to zero or more existing directories. Empty
    categories are still present as ``[]`` so callers can render
    "(none found)" deterministically. Discovered paths are classified
    via :func:`categorize_memory_dir` so discovery and classification
    stay locked to the same pattern table.

    Categories (verified against official docs):

    - ``claude-memory``: ``~/.claude/projects/<project>/memory/`` per-project
      auto-memory (https://code.claude.com/docs/en/memory). Subdirs without
      any ``*.md`` files are skipped to avoid pulling in empty session
      scaffolding from projects Claude visited but never wrote memory for.
    - ``claude-plans``: ``~/.claude/plans/`` (local convention, not in
      official docs but commonly used for plan-mode artifacts).
    - ``codex``: ``~/.codex/memories/``
      (https://developers.openai.com/codex/memories).

    Gemini CLI is intentionally excluded: its memory surface is the single
    file ``~/.gemini/GEMINI.md`` (doesn't fit the directory abstraction)
    and the parent directory contains secrets like ``oauth_creds.json``.
    Use ``mm ingest gemini-memory`` for one-shot Gemini import instead.
    """
    grouped: dict[str, list[Path]] = {cat: [] for cat in PROVIDER_DIR_CATEGORIES}

    def _bucket(p: Path) -> None:
        cat = categorize_memory_dir(p)
        if cat in grouped:
            grouped[cat].append(p)

    claude_projects = Path("~/.claude/projects").expanduser()
    if claude_projects.is_dir():
        for project in sorted(claude_projects.iterdir()):
            if not project.is_dir():
                continue
            mem = project / "memory"
            if mem.is_dir() and any(mem.glob("*.md")):
                _bucket(mem)

    plans = Path("~/.claude/plans").expanduser()
    if plans.is_dir():
        _bucket(plans)

    codex = Path("~/.codex/memories").expanduser()
    if codex.is_dir():
        _bucket(codex)

    return grouped


def _canonical_provider_dirs() -> list[Path]:
    """Flat list of all canonical provider dirs that exist on this machine.

    Used by the legacy ``auto_discover`` migration. The wizard uses
    :func:`_detect_provider_dirs` directly so it can group prompts by
    category. See that function's docstring for scope rationale.
    """
    grouped = _detect_provider_dirs()
    return [d for cat in PROVIDER_DIR_CATEGORIES for d in grouped[cat]]


def _migrate_auto_discover_once(config: Mem2MemConfig) -> None:
    """One-shot migration from legacy ``indexing.auto_discover`` to explicit
    ``memory_dirs`` entries.

    Releases 0.1.11 and earlier ran ``ensure_auto_discovered_dirs`` on every
    startup to silently append three provider home dirs (``~/.claude/projects``,
    ``~/.gemini``, ``~/.codex/memories``) whenever the flag was True. That
    was both too wide (transcripts + secrets) and too quiet (no opt-in
    surface). The replacement is a wizard step in ``mm init`` that picks
    canonical provider memory dirs explicitly.

    For existing installs with the legacy flag still True, this helper:

    1. Enumerates :func:`_canonical_provider_dirs` (narrowed scope).
    2. Appends each one not already in ``memory_dirs``.
    3. Flips ``auto_discover`` to False in-memory.
    4. Persists the result to ``~/.memtomem/config.json`` (atomic write)
       so subsequent startups see the explicit entries and skip migration.

    Brand-new installs (no ``config.json`` yet) skip migration: the wizard
    is the only path that adds provider dirs there. The flag's deprecated
    True default still applies in-memory but never triggers without a
    config.json on disk to update — startup runtime no longer reads it.
    """
    if not config.indexing.auto_discover:
        return  # already migrated, or explicitly opted out

    config_path = _override_path()
    if not config_path.exists():
        return  # fresh install — wizard handles provider dirs explicitly

    existing = {Path(d).expanduser().resolve() for d in config.indexing.memory_dirs}
    new_dirs = [d for d in _canonical_provider_dirs() if d.expanduser().resolve() not in existing]

    config.indexing.memory_dirs.extend(new_dirs)
    config.indexing.auto_discover = False

    # Persist the full post-migration memory_dirs list (factory default + any
    # pre-existing entries + newly discovered dirs) so that the explicit
    # config.json layer reflects the same effective list the migration just
    # mutated in-memory. Without the full list we'd persist only ``new_dirs``,
    # the REPLACE-on-set semantics of the config.json layer would drop the
    # factory default on the next load, and users would lose
    # ``~/.memtomem/memories`` silently.
    _persist_auto_discover_migration(config_path, list(config.indexing.memory_dirs))

    import logging

    logging.getLogger(__name__).info(
        "Migrated auto_discover -> explicit memory_dirs: added %d provider path(s). "
        "See %s (auto_discover is deprecated and will be removed).",
        len(new_dirs),
        config_path,
    )


def _persist_auto_discover_migration(config_path: Path, full_memory_dirs: list[Path]) -> None:
    """Write the migration result to ``config.json`` atomically.

    Persists the *complete* post-migration ``memory_dirs`` list (factory
    default included) and sets ``indexing.auto_discover`` to False so the
    one-shot migration becomes idempotent. Read-merge-write so non-migrated
    sections survive untouched.
    """
    import json as _json
    import logging

    _log = logging.getLogger(__name__)

    # This runs on every legacy-config load at startup, so use a short lock
    # budget: a deferred migration (re-attempted next load) is far cheaper than
    # a 30s stall on every ``mm`` command when another writer holds the lock.
    try:
        with _config_write_lock(config_path, timeout=_MIGRATION_LOCK_BUDGET_S):
            existing: dict = {}
            if config_path.exists():
                try:
                    existing = _json.loads(config_path.read_text(encoding="utf-8"))
                except (OSError, _json.JSONDecodeError) as exc:
                    _log.warning(
                        "Cannot read %s during auto_discover migration (%s); skipping persist",
                        config_path,
                        exc,
                    )
                    return

            if not isinstance(existing, dict):
                return

            indexing = existing.get("indexing")
            if not isinstance(indexing, dict):
                indexing = {}
                existing["indexing"] = indexing

            indexing["memory_dirs"] = [str(d) for d in full_memory_dirs]
            indexing["auto_discover"] = False

            _relativize_config_paths_in_place(existing)
            try:
                _atomic_write_json(config_path, existing)
            except OSError as exc:
                _log.warning(
                    "Failed to persist auto_discover migration to %s: %s", config_path, exc
                )
    except TimeoutError:
        _log.warning(
            "auto_discover migration could not lock %s (another writer holds it); "
            "skipping this run, will retry next config load",
            config_path,
        )


# Fields that ``save_config_overrides`` persists but ``MUTABLE_FIELDS`` does
# not expose to generic mutation paths (``mm config set``,
# ``PATCH /api/config``). Managed by dedicated endpoints (e.g.
# ``/memory-dirs/add|remove`` for ``memory_dirs``) because their updates
# carry validation, indexing triggers, or filesystem side-effects that
# generic mutation would bypass.
#
# Pre-Z history: this set was named ``_EXTRA_PERSIST_FIELDS`` and had
# "always-persist" semantics to protect env-dependent factory defaults
# from being dropped on save (see
# ``feedback_env_dependent_factory_equality.md``). Z (delta-vs-comparand
# via ``build_comparand``) removed that need because the comparand itself
# incorporates factory output. The set was renamed to reflect its remaining
# role — marking the mutation/save asymmetry — rather than deleted.
_EXTRA_MUTATION_FIELDS: dict[str, set[str]] = {
    "indexing": {"memory_dirs"},
}


def _portable_path_str(p: Path | str | os.PathLike[str]) -> str:
    """Serialize a path as ``~/...`` if under ``$HOME``, else absolute.

    Keeps ``config.json`` portable across machines with different
    ``$HOME`` values: a config written on one machine and copied (or
    git-synced) to another resolves correctly because loaders already
    call ``Path.expanduser()`` per-field. Outside-``$HOME`` paths
    (``/var/log/...``, ``/opt/...``) stay absolute since their meaning
    is genuinely machine-specific.

    Idempotent: already-tilde input is returned verbatim. Relative
    inputs are also passed through unchanged. ``$HOME`` lookup
    failures (rare; bare-bones containers without ``HOME``) fall
    through to the absolute form.
    """
    p_str = os.fspath(p)
    if p_str.startswith("~"):
        return p_str
    p_path = Path(p_str)
    if not p_path.is_absolute():
        return p_str
    try:
        home = Path.home()
    except (RuntimeError, KeyError):
        return p_str
    try:
        rel = p_path.relative_to(home)
    except ValueError:
        return p_str
    rel_str = rel.as_posix()
    if rel_str in ("", "."):
        return "~"
    return f"~/{rel_str}"


# Schema-aware: which config fields hold path values that benefit from
# home-relative serialization. Update both tuples when adding new path-
# typed fields. Loaders apply ``Path.expanduser()`` per-field, so the
# round-trip ``write tilde -> read absolute`` is symmetric.
_CONFIG_PATH_SCALAR_FIELDS: tuple[tuple[str, str], ...] = (
    ("storage", "sqlite_path"),
    ("session_trace", "jsonl_path"),
)
_CONFIG_PATH_LIST_FIELDS: tuple[tuple[str, str], ...] = (
    ("indexing", "memory_dirs"),
    ("indexing", "project_memory_dirs"),
)


def _relativize_config_paths_in_place(data: dict) -> None:
    """Rewrite known path-typed config fields as ``~/...`` if under HOME.

    Mutates *data* in place. Idempotent — safe to call on already-tilde
    values or on dicts where the target sections are missing. Accepts
    both ``str`` and ``Path`` values for path fields, so it covers the
    init-wizard write path (string-typed ``state["db_path"]``) and the
    ``save_config_overrides`` flow (Pydantic ``Path`` instances pulled
    via ``getattr``) with one transform.
    """
    for section, field in _CONFIG_PATH_SCALAR_FIELDS:
        sec = data.get(section)
        if not isinstance(sec, dict):
            continue
        val = sec.get(field)
        if isinstance(val, (str, Path)):
            sec[field] = _portable_path_str(val)
    for section, field in _CONFIG_PATH_LIST_FIELDS:
        sec = data.get(section)
        if not isinstance(sec, dict):
            continue
        val = sec.get(field)
        if isinstance(val, list):
            sec[field] = [_portable_path_str(p) if isinstance(p, (str, Path)) else p for p in val]


def _json_default(obj: object) -> object:
    """``json.dumps`` fallback for values not natively JSON-serializable.

    ``BaseModel`` entries in fields like ``namespace.rules`` must be
    written as dicts (via ``model_dump(mode="json")``) so the load path
    can re-validate them on startup. ``Path`` goes through
    ``_portable_path_str`` so home-rooted paths land as ``~/...``
    even if a future field bypasses ``_relativize_config_paths_in_place``;
    unknown types fall back to ``str()`` to preserve the original
    default=str behaviour.
    """
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if isinstance(obj, Path):
        return _portable_path_str(obj)
    return str(obj)


# Cross-process lock budget for config.json read-modify-write (issue #1567).
# Matches the 30s convention of _MCP/_SETTINGS/_SKILLS_LOCK_BUDGET_S in
# memtomem.context. Migration uses a shorter budget because it runs on every
# legacy-config load at startup (see _MIGRATION_LOCK_BUDGET_S below).
_CONFIG_LOCK_BUDGET_S = 30.0
_MIGRATION_LOCK_BUDGET_S = 5.0


@contextmanager
def _config_write_lock(config_path: Path, *, timeout: float | None = None) -> Iterator[None]:
    """Serialize config.json read-modify-write across processes (issue #1567).

    ``_atomic_write_json`` prevents torn/corrupt JSON, but nothing serializes
    the read→merge→write window across processes: two concurrent writers each
    read the pre-change file, merge only their own delta, and whichever
    ``os.replace``\\s second silently discards the other's update. This holds a
    ``portalocker`` sidecar lock (``.config.json.lock``) across that whole
    span so writers serialize instead of clobbering each other.

    Locks a **sidecar**, not config.json itself — ``os.replace`` rebinds the
    data-file inode mid-write, so a lock on the data file disconnects (see
    ``_file_lock``). ``config_path`` is passed by the caller (not resolved via
    ``_override_path()`` here) so migration's explicit path and isolated-HOME
    tests lock the file they actually write. On timeout ``_file_lock`` raises
    ``TimeoutError`` having acquired nothing — callers surface a clean abort.
    """
    from memtomem.context._atomic import _file_lock, _lock_path_for

    with _file_lock(_lock_path_for(config_path), timeout=timeout or _CONFIG_LOCK_BUDGET_S):
        yield


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON atomically via tempfile in the same directory + os.replace.

    Prevents partial writes from corrupting config.json when the process
    dies mid-write or disk fills up. The tempfile lives in ``path.parent``
    so ``os.replace`` is a same-filesystem rename (atomic on POSIX + Windows).

    All four config.json writers (``save_config_overrides``, ``mm config
    unset``, ``mm init``'s ``_write_config_and_summary``, and
    ``_persist_auto_discover_migration``) route through this helper. Atomic
    replace covers crash-mid-write; concurrent writers are serialized by
    ``_config_write_lock``, which callers hold across their read-modify-write.
    """
    import json as _json
    import os
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".config.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            _json.dump(data, f, indent=2, default=_json_default)
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def build_comparand(*, quiet: bool = True) -> "Mem2MemConfig":
    """Build a fresh config reflecting everything *except* user overrides.

    Comparand = built-in defaults + ``MEMTOMEM_*`` env vars + ``config.d/``
    fragments + env-dependent factory output (``memory_dirs`` etc.). This is
    **not** "pristine code default" — it represents the value that would
    apply to a field if ``~/.memtomem/config.json`` did not pin it.

    Two consumers:

    - ``save_config_overrides`` persists only fields where the live config
      differs from this comparand — closing fragment/env/factory drag-in at
      the source (see ``project_fragment_dragin_gap.md``).
    - ``GET /api/config/defaults`` (Web UI reset-to-default button) returns
      these values so the UI can pre-fill a field with "what applies if this
      override didn't exist." After Save, ``save_config_overrides`` drops
      the matching entry so env/fragment values continue to flow through.

    ``Mem2MemConfig()`` construction reads env automatically via pydantic-
    settings and runs field ``default_factory`` callables, so env + factory
    values land without extra work. ``load_config_d`` then merges fragments
    on top, respecting per-field merge strategies.

    Safe to call concurrently: only reads env/filesystem, no mutation.
    Factory functions (e.g. ``_default_memory_dirs``) must remain pure.
    """
    comparand = Mem2MemConfig()
    load_config_d(comparand, quiet=quiet)
    # Provider memory dirs are now explicit ``memory_dirs`` entries (added by
    # the ``mm init`` wizard or migrated once from legacy ``auto_discover``),
    # not env-dependent factory output — so the comparand no longer needs a
    # discovery step here. Runtime and comparand both reflect the same
    # explicit list, and delta-only save still drops anything that matches
    # defaults + env + fragments.
    return comparand


def save_config_overrides(
    config: Mem2MemConfig,
    mutable_fields: dict[str, set[str]] | None = None,
) -> None:
    """Persist user-set overrides to ~/.memtomem/config.json.

    **Delta-only write**: compare *config* to a freshly built comparand
    (defaults + env + fragments + env-dependent factories). Only fields
    that differ are written; fields that match the comparand are dropped
    from the output (and any matching existing entry is pruned).

    This closes three silent-persistence patterns in one mechanism:

    - default-equal fields (PR #256 drop-default) — comparand contains
      the default value for fields not set by env/fragment.
    - env-sourced values (e.g. ``MEMTOMEM_MMR__ENABLED=true`` no longer
      drag-pins into ``config.json``).
    - fragment-sourced values (e.g. ``config.d/noise.json`` contents
      don't copy into ``config.json`` when an unrelated field is saved;
      the fragment stays the source of truth).

    Uses **read-merge-write** so non-mutable keys (init-only settings like
    ``embedding.provider``, ``storage.sqlite_path``) carry across saves.
    """
    from pydantic import ValidationError

    try:
        config.session_trace.model_validate(config.session_trace.model_dump())
    except ValidationError as e:
        msgs = []
        for error in e.errors():
            msg = error.get("msg", "")
            if msg.startswith("Value error, "):
                msg = msg[len("Value error, ") :]
            msgs.append(msg)
        raise ValueError("; ".join(msgs) if msgs else str(e))

    import json as _json
    import logging

    _log = logging.getLogger(__name__)
    base_fields: dict[str, set[str]] = mutable_fields or MUTABLE_FIELDS
    # build_comparand is a slow, read-only rebuild — keep it OUTSIDE the lock so
    # the serialized critical section stays as narrow as read→merge→write.
    comparand = build_comparand(quiet=True)

    path = _override_path()

    # Hold the sidecar lock across read→merge→write so a concurrent writer
    # (e.g. the Web UI PATCH path racing a terminal ``mm config set``) can't
    # read the pre-change file and clobber our delta (issue #1567). Web callers
    # already run under an in-process asyncio lock and wrap this in their own
    # timeout; the file lock's TimeoutError surfaces there as a 503.
    with _config_write_lock(path):
        existing: dict = {}
        if path.exists():
            try:
                existing = _json.loads(path.read_text(encoding="utf-8"))
            except (OSError, _json.JSONDecodeError) as exc:
                _log.warning("Cannot read existing config at %s: %s — overwriting", path, exc)

        # Union with dedicated-endpoint fields (memory_dirs). No exemption —
        # env-dependent factory output is already part of the comparand, so
        # "current == factory" still drops cleanly.
        sections = {*base_fields, *_EXTRA_MUTATION_FIELDS}
        for section_name in sections:
            live_section = getattr(config, section_name, None)
            comp_section = getattr(comparand, section_name, None)
            if live_section is None or comp_section is None:
                continue
            keys = base_fields.get(section_name, set()) | _EXTRA_MUTATION_FIELDS.get(
                section_name, set()
            )

            section_data: dict[str, object] = existing.get(section_name, {})
            if not isinstance(section_data, dict):
                section_data = {}

            for key in keys:
                live_val = getattr(live_section, key, None)
                comp_val = getattr(comp_section, key, None)
                if live_val is None or live_val == comp_val:
                    section_data.pop(key, None)
                else:
                    section_data[key] = live_val

            if section_data:
                existing[section_name] = section_data
            else:
                existing.pop(section_name, None)

        _relativize_config_paths_in_place(existing)
        _atomic_write_json(path, existing)


def register_project_memory_dir(target_dir: Path, config_path: Path | None = None) -> bool:
    """Append *target_dir* to ``indexing.project_memory_dirs`` in config.json.

    The dedicated registration write for ``mm mem init`` (ADR-0011 project
    tier opt-in). The whole read→append→write sequence runs inside
    ``_config_write_lock`` so two concurrent registrations cannot each read
    the pre-change file and clobber the other's entry.

    ``project_memory_dirs`` is deliberately **not** in
    ``_EXTRA_MUTATION_FIELDS``: ``save_config_overrides`` compares the live
    (possibly stale — a long-running ``mm web`` loaded config at startup)
    value against the comparand, and membership would let an unrelated
    settings save silently drop a registration made after that process
    started. Keeping the field out of every generic save path means those
    writers never touch the key; this helper and manual editing are the only
    writers.

    Because ``config.json`` is a REPLACE-on-load layer (unlike ``config.d``
    fragments, which APPEND), the helper persists the full merged list —
    fragment-contributed entries included — not just the new entry;
    otherwise the write would mask fragment registrations on the next load.

    Returns ``True`` when newly registered, ``False`` when *target_dir* is
    already registered (config.json or a fragment). Raises ``ValueError``
    when *target_dir* is not a canonical ``.memtomem/memories[.local]``
    tier directory, ``TimeoutError`` when another writer holds the config
    lock, and ``OSError`` when the write itself fails.
    """
    import json as _json

    resolved = target_dir.expanduser().resolve()
    # Must be ``<project_root>/.memtomem/memories[.local]`` with a NON-EMPTY
    # project root above ``.memtomem`` — the scope classifier only matches
    # ``/.memtomem/...`` at a positive offset (something before the marker),
    # so a root-level ``/.memtomem/memories`` would register but never
    # classify as a project tier. ``project_root == project_root.parent`` is
    # true only at the filesystem root.
    memtomem_dir = resolved.parent
    project_root = memtomem_dir.parent
    if (
        resolved.name not in ("memories", "memories.local")
        or memtomem_dir.name != ".memtomem"
        or not project_root.name
        or project_root == project_root.parent
    ):
        raise ValueError(
            f"not a project memory tier directory: {resolved} "
            "(expected <project>/.memtomem/memories or <project>/.memtomem/memories.local)"
        )

    # Slow read-only rebuild — outside the lock, mirroring
    # ``save_config_overrides``. Fragments are needed for the merged view;
    # the lock only serializes config.json writers.
    comparand = build_comparand(quiet=True)
    path = config_path if config_path is not None else _override_path()

    with _config_write_lock(path):
        existing: dict = {}
        if path.exists():
            existing = _json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(existing, dict):
            raise ValueError(f"config file is not a JSON object: {path}")

        indexing = existing.get("indexing")
        if not isinstance(indexing, dict):
            indexing = {}
            existing["indexing"] = indexing

        # Effective list mirrors load semantics: config.json REPLACES the
        # fragment-appended value when the key is present.
        raw = indexing.get("project_memory_dirs")
        if isinstance(raw, list):
            effective: list[object] = list(raw)
        else:
            effective = list(comparand.indexing.project_memory_dirs)

        registered = {
            Path(str(d)).expanduser().resolve() for d in effective if isinstance(d, (str, Path))
        }
        if resolved in registered:
            return False

        effective.append(resolved)
        indexing["project_memory_dirs"] = effective

        _relativize_config_paths_in_place(existing)
        _atomic_write_json(path, existing)
        return True
