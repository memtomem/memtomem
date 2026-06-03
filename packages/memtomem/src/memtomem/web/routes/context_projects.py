"""Context gateway — multi-project discovery + Add Project.

PR2 of the multi-project context UI series — see
``memtomem-docs/memtomem/planning/multi-project-context-ui-rfc.md``.

Endpoints:
- ``GET /api/context/projects`` — list all discovered scopes with health and
  optional (``?include=counts``) per-type item counts.
- ``POST /api/context/known-projects`` — register a project root for the
  Add Project UI; idempotent.
- ``PATCH /api/context/known-projects/{scope_id}`` — update a registration's
  label and/or ``enabled`` (sync-enrollment) flag.
- ``DELETE /api/context/known-projects/{scope_id}`` — drop a registration
  (including stale entries whose root no longer exists).

Sibling per-scope item routes and mutating routes re-use
``resolve_scope_root`` from this module so the active-project contract has
exactly one implementation.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from memtomem.context.agents import canonical_agent_name, diff_agents, list_canonical_agents
from memtomem.context.commands import (
    canonical_command_name,
    diff_commands,
    list_canonical_commands,
)
from memtomem.context.projects import (
    _MARKER_DIRS,
    KnownProjectsStore,
    ProjectScope,
    compute_scope_id,
    discover_project_scopes,
    has_runtime_marker,
)
from memtomem.context.mcp_servers import diff_mcp_servers, list_canonical_mcp_servers
from memtomem.context.skills import diff_skills, list_canonical_skills
from memtomem.config import TargetScope
from memtomem.web.deps import get_project_root

logger = logging.getLogger(__name__)

router = APIRouter(tags=["context-projects"])


# ── Helpers shared with sibling context_*.py routes ────────────────────


def _gateway_config(request: Request):
    """Return the ``ContextGatewayConfig`` from app state, or sane defaults
    if the app was instantiated without a config (test paths)."""
    config = getattr(request.app.state, "config", None)
    if config is None:
        # Build a default with experimental scan off — matches production default.
        from memtomem.config import ContextGatewayConfig

        return ContextGatewayConfig()
    return config.context_gateway


def _discover_for(request: Request) -> list[ProjectScope]:
    cfg = _gateway_config(request)
    cwd = _default_project_root(request)
    return discover_project_scopes(
        cwd,
        Path(cfg.known_projects_path).expanduser(),
        experimental_claude_projects_scan=cfg.experimental_claude_projects_scan,
        auto_display_configured_projects=cfg.auto_display_configured_projects,
    )


def _default_project_root(request: Request) -> Path:
    """Return the server default project root.

    Production ``mm web`` sets ``app.state.project_root`` during lifespan.
    Some focused route tests mount these routers on a minimal FastAPI app and
    override the old ``get_project_root`` dependency directly; keep honoring
    that override so migrating routes to ``resolve_scope_root`` does not make
    those tests construct the full web app.
    """
    override = request.app.dependency_overrides.get(get_project_root)
    if override is not None:
        return Path(override())
    state_root = getattr(request.app.state, "project_root", None)
    if state_root is not None:
        return Path(state_root)
    return Path.cwd()


def resolve_scope_root(
    request: Request,
    project_scope_id: str | None = Query(default=None),
    scope_id: str | None = Query(default=None),
) -> Path:
    """FastAPI dependency that maps an optional project selector to a root.

    No selector → server cwd (legacy single-project behavior preserved so PR1's
    mutating cwd flow keeps working). ``project_scope_id`` is the canonical
    query name; ``scope_id`` stays accepted as a permanent alias per ADR-0015.
    Unknown selectors → 404. Stale selectors (registered but root no longer
    exists) → 404 too — read endpoints can't usefully serve from a missing dir.
    """
    if project_scope_id is not None and scope_id is not None and project_scope_id != scope_id:
        raise HTTPException(
            status_code=400,
            detail="project_scope_id and scope_id must match when both are provided",
        )
    selected_scope_id = project_scope_id or scope_id
    if selected_scope_id is None:
        return _default_project_root(request)

    for scope in _discover_for(request):
        if scope.scope_id != selected_scope_id:
            continue
        if scope.root is None or scope.missing:
            raise HTTPException(
                status_code=404,
                detail=f"scope {selected_scope_id!r} is registered but its root is missing",
            )
        return scope.root
    raise HTTPException(status_code=404, detail=f"unknown project_scope_id: {selected_scope_id!r}")


# ── GET /context/projects ────────────────────────────────────────────────


def _counts_for(root: Path, *, target_scope: TargetScope) -> dict[str, int]:
    """Per-type unique-name counts for a project root.

    Mirrors the union the existing ``list_*`` routes render: canonical files
    plus runtime-only items the diff layer surfaces. Each ``diff_*`` call
    returns ``(runtime, name, status)`` triples; we count distinct names
    plus any canonical names with no runtime trace yet.

    Cost: 3 × (canonical scan + N runtime scans) per scope, executed every
    time the UI fetches ``GET /api/context/projects`` (every tab switch).
    Acceptable at <30 scopes; revisit with caching if discovery growth
    pushes that ceiling.
    """
    counts: dict[str, int] = {}
    try:
        names = {name for _runtime, name, _status in diff_skills(root, scope=target_scope)}
        names.update(p.name for p in list_canonical_skills(root, scope=target_scope))
        counts["skills"] = len(names)
    except Exception:
        logger.warning("counts: skills failed for %s", root, exc_info=True)
        counts["skills"] = 0

    try:
        names = {name for _runtime, name, _status in diff_commands(root, scope=target_scope)}
        # ``list_canonical_commands`` returns ``list[tuple[Path, Layout]]``
        # since ADR-0008 PR-C (#624) added directory layout. Name extraction
        # MUST be layout-aware: under directory layout the manifest is
        # ``<name>/command.md`` so ``p.stem == "command"`` collapses every
        # draft to a single ``"command"`` row, undercounting the actual
        # inventory and polluting the union with a phantom name. Especially
        # acute for ``target_scope=project_local`` where ``diff_commands``
        # returns nothing (no runtime fan-out — ADR-0011 §3 / ADR-0016 §7)
        # so the canonical count is the only contributor (review P2 on
        # ADR-0011). Use ``canonical_command_name`` — the single source of
        # truth for path → name dispatch, mirrored by
        # ``canonical_agent_name`` below.
        names.update(
            canonical_command_name(p, layout)
            for p, layout in list_canonical_commands(root, scope=target_scope)
        )
        counts["commands"] = len(names)
    except Exception:
        logger.warning("counts: commands failed for %s", root, exc_info=True)
        counts["commands"] = 0

    try:
        names = {name for _runtime, name, _status in diff_agents(root, scope=target_scope)}
        # Same layout-aware extraction as commands above — directory-layout
        # agents at ``<name>/agent.md`` collapse to a phantom ``"agent"``
        # name under ``p.stem``. ``canonical_agent_name`` is the agent-side
        # mirror of ``canonical_command_name``.
        names.update(
            canonical_agent_name(p, layout)
            for p, layout in list_canonical_agents(root, scope=target_scope)
        )
        counts["agents"] = len(names)
    except Exception:
        logger.warning("counts: agents failed for %s", root, exc_info=True)
        counts["agents"] = 0

    try:
        if target_scope == "project_shared":
            names = {name for _runtime, name, _status in diff_mcp_servers(root)}
            names.update(p.stem for p in list_canonical_mcp_servers(root))
            counts["mcp-servers"] = len(names)
        else:
            counts["mcp-servers"] = 0
    except Exception:
        logger.warning("counts: mcp-servers failed for %s", root, exc_info=True)
        counts["mcp-servers"] = 0

    return counts


def _scope_to_dict(
    scope: ProjectScope,
    *,
    with_counts: bool,
    with_coverage: bool,
    target_scope: TargetScope,
) -> dict:
    from memtomem.context.runtime_coverage import compute_runtime_coverage

    computable = scope.root is not None and not scope.missing

    # Counts are opt-in via ``?include=counts``: each scope costs several
    # per-type artifact scans (see ``_counts_for``), so the N-project Portal
    # list stays cheap by default. ``null`` means "not computed" — distinct
    # from a genuine zero — and a missing root has nothing to count.
    counts = (
        _counts_for(scope.root, target_scope=target_scope) if (with_counts and computable) else None
    )

    # Runtime coverage is opt-in via ``?include=runtime_coverage`` for the same
    # reason: ``compute_runtime_coverage`` runs a ``probe_all_runtimes`` pass
    # (per-client config-file reads) for every scope, so it must not be paid by
    # default callers (ADR-0021 PR2 "cheap by default"). ``null`` means "not
    # computed"; a present root with no detected runtimes yields ``[]``.
    coverage: list[dict[str, object]] | None = None
    if with_coverage:
        coverage = compute_runtime_coverage(scope.root) if computable else []

    return {
        "project_scope_id": scope.scope_id,
        "scope_id": scope.scope_id,
        "label": scope.label,
        "root": str(scope.root) if scope.root is not None else None,
        "tier": scope.tier,
        "sources": list(scope.sources),
        "missing": scope.missing,
        "stale": scope.stale,
        "experimental": scope.experimental,
        # ``enabled`` — the known_projects sync-enrollment flag. ``sync_eligible``
        # — derived (server-cwd OR enrolled+enabled); the frontend gates the Sync
        # button on it. ``enrolled`` is intentionally not a separate field — the
        # client derives it from ``sources`` containing "known-projects".
        "enabled": scope.enabled,
        "sync_eligible": scope.sync_eligible,
        "counts": counts,
        "runtime_coverage": coverage,
    }


@router.get("/context/projects")
async def list_projects(
    request: Request,
    target_scope: TargetScope = Query(
        "project_shared",
        description=(
            "Canonical-residency tier for per-project counts. project_local "
            "is counted only when explicitly requested."
        ),
    ),
    include: str = Query(
        "",
        description=(
            "Comma-separated optional sections to compute. Recognized: 'counts' "
            "(per-type item counts) and 'runtime_coverage' (per-runtime "
            "detected/installed/registered status) — both omitted by default "
            "because each scope costs extra per-project scans / config probes. "
            "Unknown tokens are ignored."
        ),
    ),
) -> dict:
    """Enumerate discovered project scopes with health and optional item counts.

    Response shape (RFC §Decision 4, extended by ADR-0021 PR2):

    ``{target_scope, scopes: [{project_scope_id, scope_id, label, root, tier,
    sources, missing, stale, experimental, enabled, sync_eligible, counts}]}``
    where ``counts`` is ``{skills, commands, agents, mcp-servers}`` only when
    ``?include=counts`` is requested (else ``null``). ``enabled`` is the
    known_projects sync-enrollment flag; ``sync_eligible`` is the derived
    server-cwd-OR-enrolled-and-enabled gate.
    """
    include_tokens = {tok.strip() for tok in include.split(",") if tok.strip()}
    with_counts = "counts" in include_tokens
    with_coverage = "runtime_coverage" in include_tokens
    scopes = _discover_for(request)
    return {
        "target_scope": target_scope,
        "scopes": [
            _scope_to_dict(
                s,
                with_counts=with_counts,
                with_coverage=with_coverage,
                target_scope=target_scope,
            )
            for s in scopes
        ],
    }


# ── POST /context/known-projects ─────────────────────────────────────────


class AddProjectRequest(BaseModel):
    root: str
    label: str | None = None


@router.post("/context/known-projects")
async def add_known_project(body: AddProjectRequest, request: Request) -> dict:
    """Register a project root for the Add Project UI.

    Validation:
    - ``root`` must be an absolute path that resolves to an existing directory.
    - Without a recognized runtime marker directory (the ``_MARKER_DIRS`` set:
      ``.claude``/``.gemini``/``.codex``/``.agents``/``.kimi``/``.memtomem``) the
      registration still succeeds (HTTP 200) but carries a ``warning`` field so
      the user can intentionally pre-register an empty checkout.
    """
    raw = body.root.strip()
    if not raw:
        raise HTTPException(status_code=400, detail="root must not be empty")

    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise HTTPException(status_code=400, detail=f"root must be absolute: {raw!r}")
    if not candidate.exists():
        raise HTTPException(status_code=400, detail=f"root does not exist: {raw!r}")
    if not candidate.is_dir():
        raise HTTPException(status_code=400, detail=f"root is not a directory: {raw!r}")

    cfg = _gateway_config(request)
    store = KnownProjectsStore(Path(cfg.known_projects_path).expanduser())
    # Normalize the label the same way PATCH does: a blank / whitespace-only
    # value stores None (basename fallback). Since a stored label now wins label
    # precedence in discovery, an unnormalized "   " here would otherwise render
    # a blank project name.
    label = (body.label or "").strip() or None
    entry = store.add(candidate, label=label)
    project_scope_id = compute_scope_id(entry.root)

    response: dict = {
        "project_scope_id": project_scope_id,
        "scope_id": project_scope_id,
        "root": str(entry.root),
        "label": entry.label,
    }
    if not has_runtime_marker(entry.root):
        # ``warning_code`` follows the PR1 (#549) machine-readable pattern so
        # client matching is i18n-stable. ``warning`` carries the human prose
        # for back-compat; new clients should switch on the code.
        response["warning_code"] = "no_runtime_marker"
        # Derive the list from _MARKER_DIRS so it never drifts from the actual
        # marker set (e.g. when .codex was added). Clients should switch on the
        # i18n-stable warning_code, not this human prose.
        response["warning"] = f"No {'/'.join(_MARKER_DIRS)} directory found under this root."
    return response


# ── PATCH /context/known-projects/{scope_id} ─────────────────────────────


class UpdateProjectLabelRequest(BaseModel):
    label: str | None = None
    enabled: bool | None = None


@router.patch("/context/known-projects/{scope_id}")
async def update_known_project(
    scope_id: str, body: UpdateProjectLabelRequest, request: Request
) -> dict:
    """Update a registered project's label and/or sync-enrollment flag.

    The store is otherwise append/delete-only; this is the one mutator that
    edits an existing entry in place. ``root`` and ``added_at`` are preserved —
    the scope_id is path-derived, so this never moves the project.

    Each field is applied only when *present* so a single-field PATCH never
    clobbers the other: a ``{"enabled": ...}`` request leaves the label intact,
    and a label-only request leaves ``enabled`` intact. ``label`` presence is
    detected via ``model_fields_set`` (so an explicit ``label: null`` still
    *clears* the custom label → basename fallback); ``enabled`` is persisted
    only for a real bool — an omitted key or ``enabled: null`` means "unchanged".
    With no applicable field the request is a 400. 404 when no registration
    matches (mirrors DELETE) so the client can tell "updated" from "never
    registered".

    Only known-projects entries are editable — the implicit server-cwd scope
    must be registered (POST) before it can be relabeled or paused. (Pausing a
    server-cwd scope has no sync effect: ``sync_eligible`` keeps server-cwd
    eligible regardless, since the running directory cannot be paused.)
    """
    cfg = _gateway_config(request)
    store = KnownProjectsStore(Path(cfg.known_projects_path).expanduser())

    label_present = "label" in body.model_fields_set
    set_enabled = body.enabled is not None  # null / omitted both mean "unchanged"
    if not set_enabled and not label_present:
        raise HTTPException(status_code=400, detail="no fields to update (label and/or enabled)")

    # Single atomic store write — applying label + enabled in one lock window so a
    # concurrent DELETE / PATCH can't interleave and partially apply the combined
    # update (Codex review). ``set_*`` flags gate which fields are touched.
    updated = store.update_entry_by_scope_id(
        scope_id,
        label=(body.label or "").strip() or None,
        set_label=label_present,
        enabled=bool(body.enabled),
        set_enabled=set_enabled,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail=f"unknown scope_id: {scope_id!r}")
    return {
        "project_scope_id": scope_id,
        "scope_id": scope_id,
        "root": str(updated.root),
        "label": updated.label,
        "enabled": updated.enabled,
    }


# ── DELETE /context/known-projects/{scope_id} ────────────────────────────


@router.delete("/context/known-projects/{scope_id}")
async def delete_known_project(scope_id: str, request: Request) -> dict:
    """Drop a known-projects registration by scope_id.

    Removable for stale entries too (matching is path-derived, not
    existence-derived). Idempotent: missing entry → 404 so the client can
    distinguish "already gone" from "still here".
    """
    cfg = _gateway_config(request)
    store = KnownProjectsStore(Path(cfg.known_projects_path).expanduser())
    if not store.remove_by_scope_id(scope_id):
        raise HTTPException(status_code=404, detail=f"unknown scope_id: {scope_id!r}")
    return {"deleted": scope_id}


__all__ = ["router", "resolve_scope_root"]
