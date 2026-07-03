"""Context gateway — Commands CRUD, diff, sync, import, and rendered output.

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
from memtomem.context.commands import (
    _parse_canonical_command_text,
    CANONICAL_COMMAND_ROOT,
    COMMAND_DIR_FILENAME,
    COMMAND_GENERATORS,
    CommandParseError,
    StrictDropError,
    canonical_command_name,
    diff_commands,
    extract_commands_to_canonical,
    generate_all_commands,
    list_canonical_commands,
    parse_canonical_command,
    resolve_canonical_command,
)

# Re-exports kept for test contracts: engine spies build ``module.ExtractResult``
# values, and the lock-identity test imports ``_gateway_lock`` from each kind
# module to pin the shared singleton.
from memtomem.context.commands import ExtractResult as ExtractResult
from memtomem.context.detector import COMMAND_DIRS
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
_COMMAND_SCAN_DIRS: list[str] = [rel for rel, _suffix in COMMAND_DIRS.values()]

router = APIRouter(tags=["context-commands"])

# Frontmatter keys that any command generator may drop. Hyphenated to match
# the strings the renderers emit on ``dropped_fields`` (see
# ``memtomem.context.commands._subcommand_to_gemini_toml``). Required fields
# (``name``, ``description``) are never dropped, so they aren't tracked here.
_ALL_OPTIONAL_FIELDS = ("argument-hint", "allowed-tools", "model")


def _command_fields(parsed) -> dict:
    return {
        "description": parsed.description,
        "argument_hint": parsed.argument_hint,
        "allowed_tools": parsed.allowed_tools,
        "model": parsed.model,
    }


_SPEC = _atomic_kind.AtomicKindSpec(
    kind="command",
    kind_plural="commands",
    canonical_root=CANONICAL_COMMAND_ROOT,
    dir_filename=COMMAND_DIR_FILENAME,
    scan_dirs=_COMMAND_SCAN_DIRS,
    optional_fields=_ALL_OPTIONAL_FIELDS,
    rendered_includes_fields=False,
    parse_error=CommandParseError,
    strict_drop_error=StrictDropError,
    sync_surface="web_context_commands_sync",
    import_surface="web_context_commands_import",
    # Late-binding lambdas, not function references — they resolve this
    # module's globals at call time so ``monkeypatch.setattr(context_commands,
    # "generate_all_commands", ...)`` still intercepts the engine call.
    generators=lambda: COMMAND_GENERATORS,
    list_canonicals=lambda *a, **kw: list_canonical_commands(*a, **kw),
    resolve_canonical=lambda *a, **kw: resolve_canonical_command(*a, **kw),
    diff=lambda *a, **kw: diff_commands(*a, **kw),
    parse_canonical=lambda *a, **kw: parse_canonical_command(*a, **kw),
    parse_text=lambda *a, **kw: _parse_canonical_command_text(*a, **kw),
    canonical_name=lambda *a, **kw: canonical_command_name(*a, **kw),
    generate_all=lambda *a, **kw: generate_all_commands(*a, **kw),
    extract_to_canonical=lambda *a, **kw: extract_commands_to_canonical(*a, **kw),
    fields_from_parsed=_command_fields,
)


# ── List ─────────────────────────────────────────────────────────────────


@router.get("/context/commands")
async def list_commands(
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
    """List canonical commands. Accepts project selector aliases like list_skills."""
    return await _atomic_kind.list_artifacts(_SPEC, project_root, target_scope, include)


# ── Read ─────────────────────────────────────────────────────────────────


@router.get("/context/commands/{name}")
async def read_command(
    name: str,
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description="Canonical-residency tier to read from (ADR-0016).",
    ),
) -> dict:
    return await _atomic_kind.read_artifact(_SPEC, name, project_root, target_scope)


# ── Rendered (per-runtime output with dropped fields + field map) ────────


@router.get("/context/commands/{name}/rendered")
async def rendered_command(
    name: str,
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description="Canonical-residency tier to render (ADR-0016).",
    ),
) -> JSONResponse:
    return await _atomic_kind.rendered_artifact(_SPEC, name, project_root, target_scope)


# ── Create ───────────────────────────────────────────────────────────────


class CommandCreateRequest(ArtifactCreateRequest):
    pass


@router.post("/context/commands")
async def create_command(
    body: CommandCreateRequest,
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


class CommandUpdateRequest(ArtifactUpdateRequest):
    pass


@router.put("/context/commands/{name}")
async def update_command(
    name: str,
    body: CommandUpdateRequest,
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


@router.delete("/context/commands/{name}")
async def delete_command(
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


@router.get("/context/commands/{name}/diff")
async def diff_command(
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


async def _sync_commands_core(
    project_root: Path,
    target_scope: TargetScope,
    *,
    on_drop: str = "warn",
    surface: str = "web_context_commands_sync",
    force_unsafe: bool = False,
) -> dict:
    """Lock-free commands sync core — the caller MUST hold ``_gateway_lock``.

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


@router.post("/context/commands/sync")
async def sync_commands(
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


@router.post("/context/commands/import")
async def import_commands(
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


@router.post("/context/commands/{name}/import")
async def import_command(
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
    """Import a single runtime command into the scoped canonical dir.

    Same response shape as the section-level import so the web UI can reuse
    its rendering. 404 when no runtime file matches the name (the section
    import would silently report 0 imported, which is the wrong shape of
    feedback for "you clicked a specific item that doesn't exist") — pinned
    on the gate's dry-run preview too.
    """
    return await _atomic_kind.import_artifact(_SPEC, name, body, project_root, target_scope)
