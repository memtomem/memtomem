"""Tag-related schemas."""

from __future__ import annotations

from pydantic import BaseModel

__all__ = [
    "TagCount",
    "TagsListResponse",
    "AutoTagRequest",
    "AutoTagResponse",
    "TagsUpdateRequest",
    "TagsUpdateResponse",
]


class TagCount(BaseModel):
    tag: str
    count: int


class TagsListResponse(BaseModel):
    tags: list[TagCount]
    total: int
    offset: int = 0
    limit: int = 0


class AutoTagRequest(BaseModel):
    source_filter: str | None = None
    max_tags: int = 5
    overwrite: bool = False
    dry_run: bool = True


class AutoTagResponse(BaseModel):
    total_chunks: int
    tagged_chunks: int
    skipped_chunks: int
    dry_run: bool


class TagsUpdateRequest(BaseModel):
    tags: list[str]


class TagsUpdateResponse(BaseModel):
    id: str
    tags: list[str]
