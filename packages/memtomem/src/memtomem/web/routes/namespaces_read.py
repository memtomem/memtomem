"""Read-side + cosmetic-edit namespace endpoints, mounted in the prod tier.

The structural admin verbs (per-namespace info GET, rename, delete) stay on
``namespaces.admin_router`` and remain dev-only — see
``web/app.py: _DEV_ONLY_ROUTERS``. Splitting the surface lets the Search /
Timeline / Export filter dropdowns and the Settings → Namespaces tab's
cosmetic edit (color, description) populate in prod without exposing
chunk-migrating verbs.

PATCH was promoted from admin to prod under ADR-0007 (cosmetic-only edit
needs no chunk migration). Rename and delete remain admin-only pending
ADR-0005's chunk-id stability follow-up.
"""

from __future__ import annotations

from fastapi import APIRouter

from memtomem.web.routes.namespaces import list_namespaces, update_metadata
from memtomem.web.schemas import NamespaceInfoResponse, NamespacesListResponse

router = APIRouter(prefix="/namespaces", tags=["namespaces"])
router.add_api_route(
    "",
    list_namespaces,
    methods=["GET"],
    response_model=NamespacesListResponse,
)
router.add_api_route(
    "/{namespace}",
    update_metadata,
    methods=["PATCH"],
    response_model=NamespaceInfoResponse,
)
