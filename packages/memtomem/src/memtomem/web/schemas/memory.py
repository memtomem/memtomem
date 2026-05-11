"""Memory add/upload/index schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from memtomem.config import TargetScope


class AddMemoryRequest(BaseModel):
    content: str = Field(min_length=1)
    title: str | None = None
    tags: list[str] = []
    file: str | None = None
    namespace: str | None = None
    # Bypass the trust-boundary redaction guard for this write. Default
    # False so the server rejects accidental secret pastes; the SPA
    # surfaces the matched-pattern count and asks the user to confirm
    # before retrying with this flag set.
    force_unsafe: bool = False
    # ADR-0011 Gate B / ADR-0016 §7: explicit tier selection for the
    # canonical residency of this write. Defaults to ``user`` so the
    # existing SPA flow (which does not yet send the field) keeps writing
    # to the user-tier ``memory_dirs[0]``. Project tiers require explicit
    # opt-in from the caller; ``project_shared`` additionally requires
    # ``confirm_project_shared=True`` (Gate B confirm — mirrors the MCP
    # ``mem_add`` kwarg and the chunks.py PATCH/DELETE confirm pattern).
    scope: TargetScope = "user"
    confirm_project_shared: bool = False


class RedactionBlockedResponse(BaseModel):
    """Body shape returned with HTTP 403 when the redaction guard blocks
    a write. The matched bytes are intentionally not echoed; only the
    hit count is exposed so the SPA can surface a confirm-and-retry UI.
    """

    detail: str = "redaction_blocked"
    hits: int
    surface: str


class ProjectTierBlockedResponse(BaseModel):
    """Body shape returned with HTTP 403 when ``/api/add`` is rejected
    for a ``scope='project_shared'`` write without an explicit
    ``confirm_project_shared=true`` (ADR-0011 §5 Gate B — Web-side
    parallel of the MCP ``confirm_project_shared`` kwarg).

    ``cli_hint`` names the equivalent CLI invocation so the SPA can
    surface "rejected — to proceed, either retry with confirm or run
    this command" without rewriting the message client-side. ``docs_url``
    points at the canonical-residency ADR; ADR-0011 §1's settings-row
    cleanup landed in PR #925 so the link target is stable.
    """

    detail: Literal["blocked_project_shared"] = "blocked_project_shared"
    surface: str
    scope: TargetScope
    message: str
    cli_hint: str
    docs_url: str


class AddMemoryResponse(BaseModel):
    file: str
    indexed_chunks: int


class UploadFileResult(BaseModel):
    filename: str
    indexed_chunks: int
    path: str | None = None
    error: str | None = None


class UploadResponse(BaseModel):
    files: list[UploadFileResult]
    total_indexed: int


class UploadUsageResponse(BaseModel):
    file_count: int
    total_bytes: int
    oldest_mtime: float | None = None


class ExportStatsResponse(BaseModel):
    total_chunks: int


class ImportResponse(BaseModel):
    total_chunks: int
    imported_chunks: int
    skipped_chunks: int
    failed_chunks: int
    conflict_skipped_chunks: int = 0
    updated_chunks: int = 0


class IndexRequest(BaseModel):
    path: str = "."
    recursive: bool = True
    force: bool = False
    namespace: str | None = None


class IndexResponse(BaseModel):
    total_files: int
    total_chunks: int
    indexed_chunks: int
    skipped_chunks: int
    deleted_chunks: int
    duration_ms: float
    errors: list[str] = []
    resolved_namespaces: list[str | None] = []


class PreviewNamespaceResponse(BaseModel):
    resolved_namespaces: list[str | None] = []
    truncated: bool = False
    scanned_files: int = 0
