"""Source and chunk-list schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from memtomem.config import MemoryDirKind
from memtomem.web.schemas.core import ChunkOut


class SourceOut(BaseModel):
    path: str
    chunk_count: int = 0
    last_indexed_at: datetime | None = None
    file_size: int | None = None
    namespaces: list[str] = ["default"]
    avg_tokens: int = 0
    min_tokens: int = 0
    max_tokens: int = 0
    # The configured ``memory_dir`` that contains this source, expanded
    # to an absolute path. ``None`` for orphan sources whose owning dir
    # was unregistered after indexing — they still appear in the General
    # view so users can prune or re-register them.
    memory_dir: str | None = None
    # ``"memory"`` for agent / user-memory dirs (auto-classified by path
    # pattern) and ``"general"`` for arbitrary indexed folders. ``None``
    # for orphans (no owning dir to classify). Drives the Sources page's
    # Memory / General sub-toggle.
    kind: MemoryDirKind | None = None
    # ADR-0016 §7 canonical-residency tier of the source itself,
    # path-classified via :func:`memtomem.config.classify_scope`. Always
    # one of ``user`` / ``project_shared`` / ``project_local``. The
    # SPA renders this verbatim as a per-row tier badge; the route also
    # supports ``?target_scope=`` filtering (project_local hidden by
    # default per ADR-0015 §4a).
    target_scope: str = "user"
    # Heuristic preview derived at read-time from the first indexed chunk:
    # ``title`` is the file's first heading (``#`` markers stripped) and
    # ``excerpt`` is up to ~200 chars of the first section's body. Both
    # ``None`` when the file has no heading / no body. The Source tab
    # falls back to these when no LLM-generated summary is cached yet.
    title: str | None = None
    excerpt: str | None = None
    # LLM-generated 2-3 sentence prose summary (per-source AI preview).
    # Populated from the ``ai_summary`` cache in ``_memtomem_meta`` when
    # ``IndexingConfig.auto_summarize`` is enabled and a summary has been
    # generated. ``ai_summary_language`` carries the language tag of the
    # cached entry so the frontend can flag drift against the current
    # ``summary_language`` setting.
    ai_summary: str | None = None
    ai_summary_language: str | None = None


class LanguageDriftInfo(BaseModel):
    """How many cached AI summaries are in a language that differs from the
    current ``IndexingConfig.summary_language`` setting.

    Surfaced in the ``/api/sources`` response when ``count > 0`` so the
    Source tab can show a "regenerate all" banner. Always omitted (null in
    JSON) when nothing drifts — keeps the banner trigger trivially derivable.
    """

    count: int
    current_setting: str


class SourcesResponse(BaseModel):
    sources: list[SourceOut]
    total: int = 0
    offset: int = 0
    limit: int = 0
    # Optional drift summary — populated only when one or more cached
    # AI summaries are in a language that doesn't match the current
    # config. The banner UI checks for non-null + count > 0.
    language_drift: LanguageDriftInfo | None = None


class RegenerateStartResponse(BaseModel):
    """Reply from ``POST /api/sources/regenerate-summaries``.

    ``started`` is True when this call kicked off a fresh job; False
    means a job is already running (idempotent — caller polls status).
    ``total`` is the number of paths the running job will touch.
    """

    started: bool
    total: int


class RegenerateStatusResponse(BaseModel):
    """Reply from ``GET /api/sources/regenerate-status``.

    All counters are 0 when no job has run since startup. ``running`` is
    True while a background task is still iterating; the totals (done +
    failed + skipped) eventually equal ``total`` once it completes.
    """

    running: bool
    total: int
    done: int
    failed: int
    skipped: int


class ChunksListResponse(BaseModel):
    chunks: list[ChunkOut]
    total: int


class EditRequest(BaseModel):
    new_content: str
    # Bypass the trust-boundary redaction guard. Default False — the SPA
    # confirms the matched-pattern count with the user before retrying
    # with this flag set.
    force_unsafe: bool = False


class ChunkSizeBucket(BaseModel):
    bucket: str
    count: int


class StatsResponse(BaseModel):
    total_chunks: int
    total_sources: int
    chunk_size_distribution: list[ChunkSizeBucket] = []


class TimelineResponse(BaseModel):
    chunks: list[ChunkOut]
    total: int
