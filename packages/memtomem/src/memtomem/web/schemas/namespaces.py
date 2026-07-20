"""Namespace schemas."""

from __future__ import annotations

from pydantic import BaseModel, Field, StrictBool

__all__ = [
    "NamespaceOut",
    "NamespacesListResponse",
    "NamespaceMetaRequest",
    "RenameRequest",
    "NamespaceInfoResponse",
]


class NamespaceOut(BaseModel):
    namespace: str
    chunk_count: int
    description: str = ""
    color: str = ""


class NamespacesListResponse(BaseModel):
    namespaces: list[NamespaceOut]
    total: int


class NamespaceMetaRequest(BaseModel):
    description: str | None = Field(None, max_length=500)
    color: str | None = Field(None, pattern=r"^#[0-9a-fA-F]{3,8}$|^$")


class RenameRequest(BaseModel):
    new_name: str = Field(..., min_length=1, max_length=200)
    # StrictBool: consolidating two namespaces is destructive (the source's
    # metadata row is dropped), so consent must be a literal JSON boolean —
    # a coerced ``"false"`` would read as true.
    merge: StrictBool = False


class NamespaceInfoResponse(BaseModel):
    namespace: str
    chunk_count: int
    description: str = ""
    color: str = ""
