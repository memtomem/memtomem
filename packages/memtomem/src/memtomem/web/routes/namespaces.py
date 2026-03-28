"""Namespace management endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from memtomem.web.deps import get_storage
from memtomem.web.schemas import (
    DeleteResponse,
    NamespaceInfoResponse,
    NamespaceMetaRequest,
    NamespaceOut,
    NamespacesListResponse,
    RenameRequest,
)

router = APIRouter(prefix="/namespaces", tags=["namespaces"])


@router.get("", response_model=NamespacesListResponse)
async def list_namespaces(storage=Depends(get_storage)) -> NamespacesListResponse:
    """List all namespaces with chunk counts and metadata."""
    meta_list = await storage.list_namespace_meta()
    out = [
        NamespaceOut(
            namespace=m["namespace"],
            chunk_count=m["chunk_count"],
            description=m.get("description", ""),
            color=m.get("color", ""),
        )
        for m in meta_list
    ]
    return NamespacesListResponse(namespaces=out, total=len(out))


@router.get("/{namespace}", response_model=NamespaceInfoResponse)
async def get_namespace(namespace: str, storage=Depends(get_storage)) -> NamespaceInfoResponse:
    """Get info for a specific namespace."""
    ns_list = await storage.list_namespaces()
    count = dict(ns_list).get(namespace, 0)
    if count == 0:
        # Check if namespace exists at all
        all_ns = dict(ns_list)
        if namespace not in all_ns:
            raise HTTPException(status_code=404, detail=f"Namespace '{namespace}' not found")

    meta = await storage.get_namespace_meta(namespace)
    return NamespaceInfoResponse(
        namespace=namespace,
        chunk_count=count,
        description=meta.get("description", "") if meta else "",
        color=meta.get("color", "") if meta else "",
    )


@router.patch("/{namespace}", response_model=NamespaceInfoResponse)
async def update_metadata(
    namespace: str,
    body: NamespaceMetaRequest,
    storage=Depends(get_storage),
) -> NamespaceInfoResponse:
    """Update namespace description and/or color."""
    await storage.set_namespace_meta(namespace, description=body.description, color=body.color)
    meta = await storage.get_namespace_meta(namespace)
    ns_list = await storage.list_namespaces()
    count = dict(ns_list).get(namespace, 0)
    return NamespaceInfoResponse(
        namespace=namespace,
        chunk_count=count,
        description=meta.get("description", "") if meta else "",
        color=meta.get("color", "") if meta else "",
    )


@router.post("/{namespace}/rename", response_model=NamespaceInfoResponse)
async def rename_namespace(
    namespace: str,
    body: RenameRequest,
    storage=Depends(get_storage),
) -> NamespaceInfoResponse:
    """Rename a namespace."""
    count = await storage.rename_namespace(namespace, body.new_name)
    meta = await storage.get_namespace_meta(body.new_name)
    return NamespaceInfoResponse(
        namespace=body.new_name,
        chunk_count=count,
        description=meta.get("description", "") if meta else "",
        color=meta.get("color", "") if meta else "",
    )


@router.delete("/{namespace}", response_model=DeleteResponse)
async def delete_namespace(namespace: str, storage=Depends(get_storage)) -> DeleteResponse:
    """Delete all chunks in a namespace."""
    deleted = await storage.delete_by_namespace(namespace)
    return DeleteResponse(deleted=deleted)
