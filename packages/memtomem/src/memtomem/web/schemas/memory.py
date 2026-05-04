"""Memory add/upload/index schemas."""

from __future__ import annotations

from pydantic import BaseModel, Field


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


class RedactionBlockedResponse(BaseModel):
    """Body shape returned with HTTP 403 when the redaction guard blocks
    a write. The matched bytes are intentionally not echoed; only the
    hit count is exposed so the SPA can surface a confirm-and-retry UI.
    """

    detail: str = "redaction_blocked"
    hits: int
    surface: str


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
