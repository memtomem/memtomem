"""Context gateway — Agents CRUD, diff, sync, import, rendered output, and field map.

Thin route layer: the ``@router``-decorated functions below own the URL,
signature, and OpenAPI surface (and the AST invariant registry keys on
them — see ``test_web_invariants_registry.py``); the handler bodies live
once in :mod:`memtomem.web.routes._atomic_kind`, parametrized by
``_SPEC`` (#1514). Engine callables on the spec late-bind this module's
globals so tests can keep monkeypatching them here.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from memtomem.config import TargetScope
from memtomem.context.agents import (
    AGENT_GENERATORS,
    AGENT_DIR_FILENAME,
    CANONICAL_AGENT_ROOT,
    AgentParseError,
    StrictDropError,
    SubAgent,
    _parse_canonical_agent_text,
    canonical_agent_name,
    diff_agents,
    extract_agents_to_canonical,
    generate_all_agents,
    list_canonical_agents,
    parse_canonical_agent,
    resolve_canonical_agent,
)

# Re-exports kept for test contracts: engine spies build ``module.ExtractResult``
# values, and the lock-identity test imports ``_gateway_lock`` from each kind
# module to pin the shared singleton.
from memtomem.context.agents import ExtractResult as ExtractResult
from memtomem.context.detector import AGENT_DIRS
from memtomem.web.routes import _atomic_kind
from memtomem.web.routes._artifact_common import (
    ArtifactCreateRequest,
    ArtifactUpdateRequest,
    AtomicSyncRequest,
    ImportRequest,
)
from memtomem.web.routes._locks import _gateway_lock as _gateway_lock
from memtomem.web.routes.context_projects import (
    resolve_scope_root,
    resolve_scope_root_cascade_gated,
    resolve_writable_scope_root,
)

# Flat list of project-relative runtime scan paths reported on list / import
# responses so the web UI's empty-state hint can name the exact directories
# the detector inspects without hardcoding them client-side.
_AGENT_SCAN_DIRS: list[str] = [d for paths in AGENT_DIRS.values() for d in paths]

router = APIRouter(tags=["context-agents"])

# Fields present in the canonical SubAgent that may be dropped per-runtime.
_ALL_OPTIONAL_FIELDS = ("tools", "model", "skills", "isolation", "kind", "temperature")


def _agent_to_dict(agent: SubAgent) -> dict:
    return {
        "name": agent.name,
        "description": agent.description,
        "tools": agent.tools,
        "model": agent.model,
        "skills": agent.skills,
        "isolation": agent.isolation,
        "kind": agent.kind,
        "temperature": agent.temperature,
    }


_SPEC = _atomic_kind.AtomicKindSpec(
    kind="agent",
    kind_plural="agents",
    canonical_root=CANONICAL_AGENT_ROOT,
    dir_filename=AGENT_DIR_FILENAME,
    scan_dirs=_AGENT_SCAN_DIRS,
    optional_fields=_ALL_OPTIONAL_FIELDS,
    rendered_includes_fields=True,
    parse_error=AgentParseError,
    strict_drop_error=StrictDropError,
    sync_surface="web_context_agents_sync",
    import_surface="web_context_agents_import",
    # Late-binding lambdas, not function references — they resolve this
    # module's globals at call time so ``monkeypatch.setattr(context_agents,
    # "generate_all_agents", ...)`` still intercepts the engine call.
    generators=lambda: AGENT_GENERATORS,
    list_canonicals=lambda *a, **kw: list_canonical_agents(*a, **kw),
    resolve_canonical=lambda *a, **kw: resolve_canonical_agent(*a, **kw),
    diff=lambda *a, **kw: diff_agents(*a, **kw),
    parse_canonical=lambda *a, **kw: parse_canonical_agent(*a, **kw),
    parse_text=lambda *a, **kw: _parse_canonical_agent_text(*a, **kw),
    canonical_name=lambda *a, **kw: canonical_agent_name(*a, **kw),
    generate_all=lambda *a, **kw: generate_all_agents(*a, **kw),
    extract_to_canonical=lambda *a, **kw: extract_agents_to_canonical(*a, **kw),
    fields_from_parsed=_agent_to_dict,
)


# ── List ─────────────────────────────────────────────────────────────────


@router.get("/context/agents")
async def list_agents(
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description=(
            "Canonical-residency tier to list. project_local is shown only "
            "when explicitly requested."
        ),
    ),
    include: str | None = Query(
        None,
        description=(
            "Comma-separated optional enrichments. ``versions`` adds a per-item "
            "``versions`` summary (label pointers + count) to feed the list-card "
            "chips (ADR-0022 PR4); omitted by default so the list stays I/O-free."
        ),
    ),
) -> dict:
    """List canonical agents. Accepts project selector aliases like list_skills."""
    return await _atomic_kind.list_artifacts(_SPEC, project_root, target_scope, include)


# ── Read ─────────────────────────────────────────────────────────────────


@router.get("/context/agents/{name}")
async def read_agent(
    name: str,
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description="Canonical-residency tier to read from (ADR-0016).",
    ),
) -> dict:
    return await _atomic_kind.read_artifact(_SPEC, name, project_root, target_scope)


# ── Rendered (per-runtime output with dropped fields + field map) ────────


@router.get("/context/agents/{name}/rendered")
async def rendered_agent(
    name: str,
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description="Canonical-residency tier to render (ADR-0016).",
    ),
) -> JSONResponse:
    return await _atomic_kind.rendered_artifact(_SPEC, name, project_root, target_scope)


# ── Create ───────────────────────────────────────────────────────────────


class AgentCreateRequest(ArtifactCreateRequest):
    pass


@router.post("/context/agents")
async def create_agent(
    body: AgentCreateRequest,
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description=(
            "Canonical-residency tier to create in. user requires the "
            "allow_host_writes confirm round-trip; project_local rejected (ADR-0011 §3)."
        ),
    ),
) -> dict:
    return await _atomic_kind.create_artifact(_SPEC, body, project_root, target_scope)


# ── Update ───────────────────────────────────────────────────────────────


class AgentUpdateRequest(ArtifactUpdateRequest):
    pass


@router.put("/context/agents/{name}")
async def update_agent(
    name: str,
    body: AgentUpdateRequest,
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description=(
            "Canonical-residency tier to update. user requires the "
            "allow_host_writes confirm round-trip; project_local rejected (ADR-0011 §3)."
        ),
    ),
) -> JSONResponse:
    return await _atomic_kind.update_artifact(_SPEC, name, body, project_root, target_scope)


# ── Delete ───────────────────────────────────────────────────────────────


@router.delete("/context/agents/{name}")
async def delete_agent(
    name: str,
    cascade: bool = Query(False),
    project_root: Path = Depends(resolve_scope_root_cascade_gated),
    target_scope: TargetScope = Query(
        "project_shared",
        description=(
            "Canonical-residency tier to delete from. user requires the "
            "allow_host_writes confirm round-trip; project_local rejected (ADR-0011 §3)."
        ),
    ),
    allow_host_writes: bool = Query(
        False,
        description=(
            "#1263 host-write opt-in for target_scope=user. Query parameter "
            "(not a body field) because DELETE bodies are client-hostile; "
            "the needs_confirmation envelope names the same flag."
        ),
    ),
) -> dict:
    return await _atomic_kind.delete_artifact(
        _SPEC, name, cascade, project_root, target_scope, allow_host_writes
    )


# ── Diff ─────────────────────────────────────────────────────────────────


@router.get("/context/agents/{name}/diff")
async def diff_agent(
    name: str,
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description="Canonical-residency tier to diff against runtime fan-out (ADR-0016).",
    ),
) -> dict:
    return await _atomic_kind.diff_artifact(_SPEC, name, project_root, target_scope)


# ── Sync ─────────────────────────────────────────────────────────────────


class SyncRequest(AtomicSyncRequest):
    pass


async def _sync_agents_core(
    project_root: Path,
    target_scope: TargetScope,
    *,
    on_drop: str = "warn",
    surface: str = "web_context_agents_sync",
    force_unsafe: bool = False,
) -> dict:
    """Lock-free agents sync core — the caller MUST hold ``_gateway_lock``.

    Kept as a module-level name because ``context_sync_all`` imports the
    five per-type cores by name; the body lives in
    :func:`_atomic_kind.sync_core` (see there for the lock and
    thread-offload contract).
    """
    return await _atomic_kind.sync_core(
        _SPEC,
        project_root,
        target_scope,
        on_drop=on_drop,
        surface=surface,
        force_unsafe=force_unsafe,
    )


@router.post("/context/agents/sync")
async def sync_agents(
    body: SyncRequest | None = None,
    project_root: Path = Depends(resolve_writable_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description=(
            "Canonical-residency tier to fan out. user fans out to the host "
            "~/.claude-family roots behind the allow_host_writes confirm "
            "round-trip (#1263); project_local rejected — no runtime fan-out "
            "per ADR-0011 §3."
        ),
    ),
) -> dict:
    return await _atomic_kind.sync_artifacts(_SPEC, body, project_root, target_scope)


# ── Import ───────────────────────────────────────────────────────────────


@router.post("/context/agents/import")
async def import_agents(
    body: ImportRequest | None = None,
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description=(
            "Canonical-residency tier to import into. user requires the "
            "allow_host_writes confirm round-trip; project_local rejected (ADR-0011 §3)."
        ),
    ),
    dry_run: bool = Query(
        False,
        description=(
            "Preview the import without writing to canonical (rank-10): runs the "
            "full scan + privacy walk + dedup and returns the would-import / would-"
            "skip counts, leaving disk untouched. Returned regardless of "
            "confirmation flags (mirrors the transfer route's dry_run)."
        ),
    ),
) -> dict:
    return await _atomic_kind.import_artifacts(_SPEC, body, project_root, target_scope, dry_run)


@router.post("/context/agents/{name}/import")
async def import_agent(
    name: str,
    body: ImportRequest | None = None,
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description=(
            "Canonical-residency tier to import into. user requires the "
            "allow_host_writes confirm round-trip; project_local rejected (ADR-0011 §3)."
        ),
    ),
) -> dict:
    """Import a single runtime agent into the scoped canonical dir.

    Same response shape as the section-level import so the web UI can reuse
    its rendering. 404 when no runtime file matches the name (the section
    import would silently report 0 imported, which is the wrong shape of
    feedback for "you clicked a specific item that doesn't exist") — pinned
    on the gate's dry-run preview too.
    """
    return await _atomic_kind.import_artifact(_SPEC, name, body, project_root, target_scope)
