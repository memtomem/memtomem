"""Memory add/upload/index schemas."""

from __future__ import annotations

from pydantic import BaseModel, Field


class AddMemoryRequest(BaseModel):
    content: str = Field(min_length=1)
    title: str | None = None
    tags: list[str] = []
    file: str | None = None
    namespace: str | None = None


class AddMemoryResponse(BaseModel):
    file: str
    indexed_chunks: int


class UploadFileResult(BaseModel):
    filename: str
    indexed_chunks: int
    error: str | None = None


class UploadResponse(BaseModel):
    files: list[UploadFileResult]
    total_indexed: int


class ExportStatsResponse(BaseModel):
    total_chunks: int


class ImportResponse(BaseModel):
    total_chunks: int
    imported_chunks: int
    skipped_chunks: int
    failed_chunks: int


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
