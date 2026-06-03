"""Context gateway — per-artifact version snapshots + label pointers (ADR-0022).

Surfaces the edit/deploy split for ``agents`` and ``commands``: freeze the
working canonical into an immutable ``versions/vN.md`` snapshot, move label
pointers (``production`` / ``staging`` / …) over those versions — where a
*promote* doubles as a *rollback*, since both just move a pointer — and list
the manifest.

Scope boundaries (ADR-0022):

- **agents + commands only.** ``skills`` are directory-tree artifacts whose
  "version" is a tree snapshot, not a single ``.md`` copy (invariant 7); any
  other type returns 404 here.
- **directory layout only.** A flat-layout artifact (``agents/<name>.md``) has
  no per-artifact home for a ``versions/`` store (invariant 3). Mutations on a
  flat artifact return 409 (run ``mm context migrate`` first); the read route
  returns a benign ``migrate_required`` flag so the UI can hint instead of error.
- **per ``(scope, type, name)``.** The version store lives under the artifact's
  canonical directory, which is scope-specific (ADR-0011 / ADR-0022 Decision b);
  the user-tier and project_shared ``my-agent`` have independent histories.
  Writes are restricted to ``project_shared`` in this release, mirroring the
  other context write routes.

The HTTP layer is thin: all version state lives in ``context/versioning.py``
(pure filesystem, lock-guarded). Routes never echo raw filesystem paths —
:func:`_version_http` redacts them, matching the app-level ``ValueError``
handler's privacy boundary.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from memtomem.config import TargetScope
from memtomem.context import versioning
from memtomem.context._names import Layout, validate_name
from memtomem.context.agents import resolve_canonical_agent
from memtomem.context.commands import resolve_canonical_command
from memtomem.context.versioning import (
    LabelNotFoundError,
    VersionError,
    VersionNotFoundError,
    VersionsDirMissingError,
)
from memtomem.web.routes._locks import _gateway_lock
from memtomem.web.routes.context_projects import resolve_scope_root

logger = logging.getLogger(__name__)

router = APIRouter(tags=["context-versions"])

# Artifact types eligible for versioning → (resolver, validate_name kind).
# Skills are excluded by design (ADR-0022 invariant 7); any other type 404s.
_Resolver = Callable[[Path, str, TargetScope], "tuple[Path, Layout] | None"]
_ELIGIBLE: dict[str, tuple[_Resolver, str]] = {
    "agents": (
        lambda root, name, scope: resolve_canonical_agent(root, name, scope=scope),
        "agent",
    ),
    "commands": (
        lambda root, name, scope: resolve_canonical_command(root, name, scope=scope),
        "command",
    ),
}

# Same shape as the app-level ValueError handler's redaction so a version
# error's filesystem path never reaches the client (privacy boundary).
_PATH_RE = re.compile(r"(?:[A-Za-z]:)?(?:[/\\][\w.\-]+){2,}")


# ── Helpers ──────────────────────────────────────────────────────────────


def _reject_non_shared_write(target_scope: TargetScope, action: str) -> None:
    """Reject writes on non-``project_shared`` tiers with HTTP 400.

    Mirrors ``context_agents._reject_non_shared_write`` so each route file
    stays self-contained; versioning writes are canonical writes and follow
    the same tier policy as create/update in this release.
    """
    if target_scope != "project_shared":
        raise HTTPException(
            status_code=400,
            detail=(
                f"{action} is supported only on project_shared in this release; "
                f"got target_scope={target_scope!r}."
            ),
        )


def _version_http(exc: VersionError) -> HTTPException:
    """Map a versioning exception to an HTTPException with a path-redacted body.

    ``VersionError`` is a ``ValueError`` subclass, so an *uncaught* one would
    fall through to the app's 400 handler. We catch it to assign accurate
    status codes (404 for not-found, 409 for the dir-layout requirement) while
    keeping the same path-redaction the global handler applies.
    """
    msg = _PATH_RE.sub("<path>", str(exc))
    if isinstance(exc, (VersionNotFoundError, LabelNotFoundError)):
        return HTTPException(status_code=404, detail=msg)
    if isinstance(exc, VersionsDirMissingError):
        return HTTPException(status_code=409, detail=msg)
    # ReservedLabelError / InvalidLabelError / InvalidTagError / base VersionError
    return HTTPException(status_code=400, detail=msg)


def _resolve_versionable(
    artifact_type: str,
    project_root: Path,
    raw_name: str,
    scope: TargetScope,
) -> tuple[str, Path, Layout]:
    """Resolve ``(artifact_type, name, scope)`` → ``(name, working_file, layout)``.

    Raises HTTP 404 for an unsupported type or a missing artifact, and lets
    ``validate_name``'s ``InvalidNameError`` (a ``ValueError``) propagate to the
    app's 400 handler — matching every other context route's name policy.
    """
    entry = _ELIGIBLE.get(artifact_type)
    if entry is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Versioning is not supported for {artifact_type!r} (agents and commands only)."
            ),
        )
    resolver, kind = entry
    name = validate_name(raw_name, kind=kind)
    resolved = resolver(project_root, name, scope)
    if resolved is None:
        raise HTTPException(status_code=404, detail=f"{kind} {name!r} not found")
    working_file, layout = resolved
    return name, working_file, layout


def _require_dir_layout(name: str, layout: Layout) -> None:
    """409 when *layout* is flat — versioning needs the per-artifact directory."""
    if layout != "dir":
        raise HTTPException(
            status_code=409,
            detail=(
                f"{name!r} uses flat layout, which has no per-artifact version store. "
                f"Run `mm context migrate` to convert it to directory layout first."
            ),
        )


# ── Read ─────────────────────────────────────────────────────────────────


@router.get("/context/{artifact_type}/{name}/versions")
async def list_artifact_versions(
    artifact_type: str,
    name: str,
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description="Canonical-residency tier whose version store to read (ADR-0016/0022).",
    ),
) -> dict:
    """List versions + label pointers for one ``(scope, type, name)``.

    A flat-layout artifact returns ``migrate_required: true`` with empty
    versions/labels rather than an error, so the UI can render a hint.
    """
    name, working_file, layout = _resolve_versionable(
        artifact_type, project_root, name, target_scope
    )
    if layout != "dir":
        return {
            "name": name,
            "artifact_type": artifact_type,
            "target_scope": target_scope,
            "layout": layout,
            "versions": [],
            "labels": {},
            "has_versions": False,
            "migrate_required": True,
        }

    artifact_dir = working_file.parent
    try:
        manifest = versioning.load_manifest(artifact_dir)
    except VersionError as exc:
        raise _version_http(exc) from exc

    versions = [
        {"tag": rec.tag, "created_at": rec.created_at, "note": rec.note}
        for rec in sorted(manifest.versions.values(), key=lambda r: int(r.tag[1:]), reverse=True)
    ]
    return {
        "name": name,
        "artifact_type": artifact_type,
        "target_scope": target_scope,
        "layout": layout,
        "versions": versions,
        "labels": dict(manifest.labels),
        "has_versions": bool(manifest.versions),
        "migrate_required": False,
    }


# ── Create version (freeze working canonical) ────────────────────────────


class VersionCreateRequest(BaseModel):
    note: str = ""


@router.post("/context/{artifact_type}/{name}/versions")
async def create_artifact_version(
    artifact_type: str,
    name: str,
    body: VersionCreateRequest | None = None,
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description="Canonical-residency tier to freeze. Non-shared tiers rejected (ADR-0011).",
    ),
) -> dict:
    """Freeze the current working canonical into a new immutable ``vN`` snapshot.

    The bytes are not privacy-scanned here: a labeled sync re-scans the frozen
    ``versions/vN.md`` at deploy time (ADR-0022 / ``make_label_resolver``), so
    the trust boundary stays at fan-out, exactly as the working-file path does.
    """
    _reject_non_shared_write(target_scope, "Create version")
    name, working_file, layout = _resolve_versionable(
        artifact_type, project_root, name, target_scope
    )
    _require_dir_layout(name, layout)
    note = (body.note if body else "") or ""
    artifact_dir = working_file.parent

    try:
        async with asyncio.timeout(60):
            # Hold the gateway lock so a concurrent update to the working file
            # cannot tear the snapshot; create_version also takes its own
            # non-reentrant file lock on the versions.json sidecar.
            async with _gateway_lock:
                record = versioning.create_version(artifact_dir, working_file, note=note)
    except TimeoutError:
        raise HTTPException(503, "Version create timed out — another sync may be in progress")
    except VersionError as exc:
        raise _version_http(exc) from exc

    return {
        "name": name,
        "artifact_type": artifact_type,
        "target_scope": target_scope,
        "version": {"tag": record.tag, "created_at": record.created_at, "note": record.note},
    }


# ── Promote / rollback a label ───────────────────────────────────────────


class LabelPromoteRequest(BaseModel):
    version: str


@router.put("/context/{artifact_type}/{name}/labels/{label}")
async def promote_artifact_label(
    artifact_type: str,
    name: str,
    label: str,
    body: LabelPromoteRequest,
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description="Canonical-residency tier whose label to move. Non-shared rejected (ADR-0011).",
    ),
) -> dict:
    """Point *label* at *version* (create-or-move). Promote and rollback are the
    same operation — both just move the pointer to a frozen version."""
    _reject_non_shared_write(target_scope, "Promote label")
    name, working_file, layout = _resolve_versionable(
        artifact_type, project_root, name, target_scope
    )
    _require_dir_layout(name, layout)
    artifact_dir = working_file.parent

    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                versioning.promote_label(artifact_dir, label, body.version)
                manifest = versioning.load_manifest(artifact_dir)
    except TimeoutError:
        raise HTTPException(503, "Label promote timed out — another sync may be in progress")
    except VersionError as exc:
        raise _version_http(exc) from exc

    return {
        "name": name,
        "artifact_type": artifact_type,
        "target_scope": target_scope,
        "label": label,
        "version": body.version,
        "labels": dict(manifest.labels),
    }


@router.delete("/context/{artifact_type}/{name}/labels/{label}")
async def delete_artifact_label(
    artifact_type: str,
    name: str,
    label: str,
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description="Canonical-residency tier whose label to drop. Non-shared rejected (ADR-0011).",
    ),
) -> dict:
    """Remove *label* from the manifest. No-op (still 200) when the label is
    absent; rejects the reserved ``latest`` with 400."""
    _reject_non_shared_write(target_scope, "Delete label")
    name, working_file, layout = _resolve_versionable(
        artifact_type, project_root, name, target_scope
    )
    _require_dir_layout(name, layout)
    artifact_dir = working_file.parent

    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                versioning.delete_label(artifact_dir, label)
                manifest = versioning.load_manifest(artifact_dir)
    except TimeoutError:
        raise HTTPException(503, "Label delete timed out — another sync may be in progress")
    except VersionError as exc:
        raise _version_http(exc) from exc

    return {
        "name": name,
        "artifact_type": artifact_type,
        "target_scope": target_scope,
        "deleted_label": label,
        "labels": dict(manifest.labels),
    }
