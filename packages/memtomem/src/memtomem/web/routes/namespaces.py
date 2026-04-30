"""Namespace management endpoints.

Tier split (after ADR-0007):

* ``list_namespaces`` and ``update_metadata`` are tier-mounted in
  ``namespaces_read`` (prod). They read or cosmetically edit per-namespace
  metadata (color, description) — both are safe to expose without
  chunk-migration policy.
* ``get_namespace``, ``rename_namespace``, and ``delete_namespace`` stay on
  ``admin_router`` (dev-only). Rename and delete need chunk-id stability
  design (ADR-0005) before promotion; the per-namespace info GET is
  redundant with the list endpoint and stays admin-side.
"""

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

admin_router = APIRouter(prefix="/namespaces", tags=["namespaces"])


# Registered on the read router in namespaces_read.py; not on admin_router
# (read-side surface lives in the prod tier — see web/app.py _PROD_ROUTERS).
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


@admin_router.get("/{namespace}", response_model=NamespaceInfoResponse)
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


# Registered on the read router in namespaces_read.py (prod tier — cosmetic
# edit doesn't migrate chunks). Rename and delete stay on admin_router below.
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


@admin_router.post("/{namespace}/rename", response_model=NamespaceInfoResponse)
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


@admin_router.delete("/{namespace}", response_model=DeleteResponse)
async def delete_namespace(namespace: str, storage=Depends(get_storage)) -> DeleteResponse:
    """Delete all chunks in a namespace."""
    deleted = await storage.delete_by_namespace(namespace)
    return DeleteResponse(deleted=deleted)


# Module-attribute alias keeping web/app.py's include loop (`module.router`)
# wired to the dev-only admin surface. The read-side endpoint is mounted via
# the sibling ``namespaces_read`` module in _PROD_ROUTERS.
router = admin_router
