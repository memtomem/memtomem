"""Read-only namespace endpoint mounted in both prod and dev tiers.

The admin (CRUD) routes live on ``namespaces.admin_router`` and stay
dev-only — see ``web/app.py: _DEV_ONLY_ROUTERS``. Splitting the read
surface lets the Search / Timeline / Export filter dropdowns and the
Home dashboard's namespace donut populate in prod without exposing
PATCH/POST/DELETE.
"""

from __future__ import annotations

from fastapi import APIRouter

from memtomem.web.routes.namespaces import list_namespaces
from memtomem.web.schemas import NamespacesListResponse

router = APIRouter(prefix="/namespaces", tags=["namespaces"])
router.add_api_route(
    "",
    list_namespaces,
    methods=["GET"],
    response_model=NamespacesListResponse,
)
