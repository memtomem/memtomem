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

from memtomem.errors import NamespaceConflictError
from memtomem.web.deps import get_storage
from memtomem.web.schemas import (
    DeleteResponse,
    NamespaceInfoResponse,
    NamespaceMetaRequest,
    NamespaceOut,
    NamespaceRenameResponse,
    NamespacesListResponse,
    RenameRequest,
)

admin_router = APIRouter(prefix="/namespaces", tags=["namespaces"])

# Per-``reason_code`` 409 wording for the rename route. The UI offers no merge
# affordance (``renameNamespace()`` posts ``{new_name}`` only), so the only
# remedy that exists on this surface is "choose a different name" — telling a
# UI user to pass ``merge=True`` or call ``ns_assign`` would be advice they
# cannot follow.
_RENAME_CONFLICT_DETAIL = {
    "target_exists": "A namespace named '{new}' already exists. Choose a different name.",
    "same_name": "'{new}' is the current name — pick a different one.",
}


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


@admin_router.post("/{namespace}/rename", response_model=NamespaceRenameResponse)
async def rename_namespace(
    namespace: str,
    body: RenameRequest,
    storage=Depends(get_storage),
) -> NamespaceRenameResponse:
    """Rename a namespace.

    Refuses (409) when the target already exists, unless ``merge`` is a
    literal ``true`` — see ``NamespaceOps.rename_namespace``.
    """
    try:
        result = await storage.rename_namespace(namespace, body.new_name, merge=body.merge)
    except NamespaceConflictError as exc:
        # Caller-resolvable collision (existing target, or old == new), not an
        # internal fault — without this it would land on the generic 500
        # handler in web/app.py. The detail is phrased for *this* surface:
        # forwarding storage's message would tell a UI user to "pass
        # merge=True" or call a tool the UI has no affordance for (#1870).
        detail = _RENAME_CONFLICT_DETAIL.get(
            exc.reason_code, f"Cannot rename '{namespace}' to '{body.new_name}'."
        ).format(old=namespace, new=body.new_name)
        raise HTTPException(status_code=409, detail=detail) from exc
    if not (result.chunks_moved or result.metadata_renamed or result.merged):
        # Nothing moved because the source held nothing. Falling through would
        # answer 200 with the *target's* count and metadata — a rename that
        # never happened, presented as one that did. 404 matches the info GET.
        raise HTTPException(status_code=404, detail=f"Namespace '{namespace}' not found")
    # Report the target's resulting total, the way ``get_namespace`` does:
    # on a merge the moved-row count and the namespace's size differ, and the
    # list / info endpoints would immediately contradict a moved count.
    ns_list = await storage.list_namespaces()
    count = dict(ns_list).get(body.new_name, 0)
    meta = await storage.get_namespace_meta(body.new_name)
    return NamespaceRenameResponse(
        namespace=body.new_name,
        chunk_count=count,
        description=meta.get("description", "") if meta else "",
        color=meta.get("color", "") if meta else "",
        chunks_moved=result.chunks_moved,
        # A merge deletes the source's copy of chunks the target already had;
        # the receipt says how many so the deletion is never silent.
        duplicates_dropped=result.duplicates_dropped,
        merged=result.merged,
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
