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
  flat artifact return 409 (enable versioning via ``POST .../versions/enable``,
  or ``mm context migrate`` for a CLI-installed artifact); the read route
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
from memtomem.context._names import InvalidNameError, Layout, validate_name
from memtomem.context.agents import resolve_canonical_agent
from memtomem.context.commands import resolve_canonical_command
from memtomem.context.migrate import adopt_flat_to_dir
from memtomem.context.versioning import (
    LabelNotFoundError,
    VersionError,
    VersionNotFoundError,
    VersionsDirMissingError,
)
from memtomem.web.routes._errors import _error
from memtomem.web.routes._locks import _gateway_lock
from memtomem.web.routes.context_projects import resolve_scope_root

logger = logging.getLogger(__name__)

router = APIRouter(tags=["context-versions"])

# Engine-side bound on the versions.json sidecar-lock wait, kept under the
# routes' outer ``asyncio.timeout(60)`` so the worker thread self-aborts
# in-window — an unbounded ``portalocker`` wait is uncancellable and would
# keep writing after the handler already returned 503 (#1145 shape;
# 30s < 60s mirrors context_mutations/context_transfer/wiki-commit).
_VERSIONS_LOCK_BUDGET_S = 30.0

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
        raise _error(
            400,
            "validation",
            (
                f"{action} is supported only on project_shared in this release; "
                f"got target_scope={target_scope!r}."
            ),
            reason_code="non_shared_tier",
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
        return _error(404, "missing", msg)
    if isinstance(exc, VersionsDirMissingError):
        return _error(409, "conflict", msg, reason_code="versions_dir_missing")
    # ReservedLabelError / InvalidLabelError / InvalidTagError / base VersionError
    return _error(400, "validation", msg)


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
        raise _error(
            404,
            "missing",
            (f"Versioning is not supported for {artifact_type!r} (agents and commands only)."),
        )
    resolver, kind = entry
    name = validate_name(raw_name, kind=kind)
    resolved = resolver(project_root, name, scope)
    if resolved is None:
        raise _error(404, "missing", f"{kind} {name!r} not found")
    working_file, layout = resolved
    return name, working_file, layout


def _require_dir_layout(name: str, layout: Layout) -> None:
    """409 when *layout* is flat — versioning needs the per-artifact directory."""
    if layout != "dir":
        raise _error(
            409,
            "conflict",
            (
                f"{name!r} uses flat layout, which has no per-artifact version store. "
                f"Enable versioning (POST /context/<type>/<name>/versions/enable) to convert it "
                f"to directory layout; for a CLI-installed artifact, `mm context migrate` also works."
            ),
            reason_code="flat_layout_not_versionable",
        )


# ── List-card enrichment (?include=versions) ─────────────────────────────
#
# Imported by ``context_agents.list_agents`` / ``context_commands.list_commands``
# so a single ``GET /context/{type}?include=versions`` can feed the per-card
# label chips (ADR-0022 PR4) without an N+1 fan-out to the per-artifact
# ``/versions`` route. Kept here next to ``versioning`` so the list routes stay
# version-store-agnostic and the eligible-type / dir-layout rules live in one file.


def include_has(include: str | None, token: str) -> bool:
    """True iff *token* appears in a comma-separated ``?include=`` query value.

    Tolerant of surrounding whitespace (``include=versions, foo``) and of the
    param being absent/empty, so callers branch on a plain bool.
    """
    if not include:
        return False
    return token in {part.strip() for part in include.split(",")}


def version_summary(canonical_path: Path, layout: Layout) -> dict:
    """Compact per-artifact version summary for ``?include=versions`` enrichment.

    Called once per canonical list item so the list cards can render label chips
    (``production → v2``) without a round-trip per artifact. The shape always
    carries the same four keys so the JS reader never has to branch on presence:

    - ``labels`` — ``{label: tag}`` pointer map (empty unless dir-layout w/ labels)
    - ``count`` — number of frozen versions
    - ``versionable`` — ``True`` iff a version store can live here (dir layout)
    - ``migrate_required`` — ``True`` iff a canonical exists but is flat-layout
      (invariant 3: enable versioning to convert it to dir layout first)

    A flat-layout artifact has no per-artifact home for a ``versions/`` store
    (invariant 3) → ``versionable=False, migrate_required=True``, no labels. A
    corrupt ``versions.json`` is isolated per-artifact (``error=True``, empty
    labels) rather than 500-ing the whole list — mirroring how the sync engine
    isolates a per-item read/parse failure as a skip instead of aborting the
    fan-out. The manifest read is unsynchronized (``load_manifest`` takes no
    lock), matching every other read path against this sidecar.
    """
    if layout != "dir":
        return {"labels": {}, "count": 0, "versionable": False, "migrate_required": True}
    artifact_dir = canonical_path.parent
    try:
        manifest = versioning.load_manifest(artifact_dir)
    except VersionError:
        # Name only (not the full path) in the server log — keep the corrupt
        # sidecar diagnosable without 500-ing the list or leaking the abs path.
        logger.warning(
            "versions manifest unreadable for %r; listing without chips", artifact_dir.name
        )
        return {
            "labels": {},
            "count": 0,
            "versionable": True,
            "migrate_required": False,
            "error": True,
        }
    return {
        "labels": dict(manifest.labels),
        "count": len(manifest.versions),
        "versionable": True,
        "migrate_required": False,
    }


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
            # non-reentrant file lock on the versions.json sidecar. The engine
            # call is offloaded — it blocks on that cross-process lock, and
            # running it on the loop would stall every concurrent request AND
            # make the outer timeout unable to fire (the loop never yields).
            # The builtin TimeoutError from an expired lock budget lands in
            # the same 503 arm as the outer asyncio.timeout.
            async with _gateway_lock:
                record = await asyncio.to_thread(
                    versioning.create_version,
                    artifact_dir,
                    working_file,
                    note=note,
                    lock_timeout=_VERSIONS_LOCK_BUDGET_S,
                )
    except TimeoutError:
        raise _error(503, "busy", "Version create timed out — another sync may be in progress")
    except VersionError as exc:
        raise _version_http(exc) from exc

    return {
        "name": name,
        "artifact_type": artifact_type,
        "target_scope": target_scope,
        "version": {"tag": record.tag, "created_at": record.created_at, "note": record.note},
    }


# ── Enable versioning (adopt a flat artifact into dir layout) ─────────────


@router.post("/context/{artifact_type}/{name}/versions/enable")
async def enable_artifact_versioning(
    artifact_type: str,
    name: str,
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description="Canonical-residency tier to adopt into dir layout. Non-shared rejected.",
    ),
) -> dict:
    """Adopt a flat-layout artifact into directory layout so it can be versioned.

    ADR-0022 invariant 3 keeps the version store dir-layout-only, and the
    create/promote routes *refuse* a flat artifact with 409 ("migrate first").
    That escape hatch — ``mm context migrate`` — provably skips a web-created
    flat file (no lockfile entry ⇒ ``skip_manual``), so a UI-created artifact
    was permanently locked out of versioning (rank 6). This route is the
    explicit, deliberate adopt action the ``migrate_required`` hint points at:
    a single byte-identical ``os.replace`` of ``<type>/<name>.md`` →
    ``<type>/<name>/<manifest>`` (via :func:`adopt_flat_to_dir`), after which
    the version routes work normally.

    Resolution + the rename run **under the gateway lock** so the layout check
    cannot race the adopt: two concurrent enables resolve serially, the first
    adopts, and the second re-resolves to ``dir`` and returns
    ``migrated: false`` (idempotent) rather than 404-ing on the moved file. An
    already-``dir`` artifact is likewise an idempotent no-op.

    Two conflict states are refused with 409 rather than silently mishandled:

    - **flat+dir collision** — both ``<name>.md`` and ``<name>/<manifest>``
      exist. The dir is already versionable, but the stray flat sibling is an
      unresolved layout collision; ``mm context migrate`` cannot clean a
      no-lockfile flat, so we surface it for manual resolution.
    - **orphaned version store** — the target ``<name>/`` already holds a
      ``versions.json`` / ``versions/`` left by a prior artifact of the same
      name. Adopting into it would silently attach stale version history to the
      new artifact, so we refuse.

    The bytes do not change and stay in the same scope, so no privacy re-scan is
    needed here (a labeled sync still re-scans the frozen ``versions/vN.md`` at
    deploy time — Gate A). Writes the canonical only (``.memtomem/…``), never
    runtime fan-out, so it follows the canonical-write tier policy
    (``project_shared``-only this release), not the sync-eligibility gate.
    """
    _reject_non_shared_write(target_scope, "Enable versioning")
    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                # Resolve UNDER the lock so a concurrent enable can't move the
                # flat file out from under a layout check made outside it.
                name, working_file, layout = _resolve_versionable(
                    artifact_type, project_root, name, target_scope
                )
                # canonical ``<type>`` root: parent of the dir for dir layout,
                # parent of the file for flat layout.
                canonical_root = (
                    working_file.parent.parent if layout == "dir" else working_file.parent
                )
                flat_path = canonical_root / f"{name}.md"
                dir_path = canonical_root / name

                if layout == "dir":
                    if flat_path.exists():
                        # flat+dir collision — refuse rather than report a
                        # misleading idempotent success while a stray flat lingers.
                        raise _error(
                            409,
                            "conflict",
                            _PATH_RE.sub(
                                "<path>",
                                f"{name!r} has both flat ({flat_path}) and directory layouts; "
                                "remove the redundant flat file to resolve the collision.",
                            ),
                            reason_code="flat_dir_collision",
                        )
                    return {
                        "name": name,
                        "artifact_type": artifact_type,
                        "target_scope": target_scope,
                        "layout": "dir",
                        "migrated": False,
                    }

                # Flat layout → adopt. Refuse if a version store already lives in
                # the target dir (orphaned from a prior same-named artifact),
                # else the adopted file would inherit stale version history.
                if (
                    versioning.versions_json_path(dir_path).exists()
                    or versioning.versions_dir(dir_path).is_dir()
                ):
                    raise _error(
                        409,
                        "conflict",
                        _PATH_RE.sub(
                            "<path>",
                            f"{name!r} cannot be adopted: an orphaned version store already "
                            f"exists at {dir_path}. Resolve it manually first.",
                        ),
                        reason_code="orphaned_version_store",
                    )
                adopt_flat_to_dir(artifact_type, flat_path, dir_path)
                return {
                    "name": name,
                    "artifact_type": artifact_type,
                    "target_scope": target_scope,
                    "layout": "dir",
                    "migrated": True,
                }
    except TimeoutError:
        raise _error(503, "busy", "Enable versioning timed out — another sync may be in progress")
    except FileNotFoundError as exc:
        # The flat file vanished mid-rename (external deletion under the lock).
        raise _error(404, "missing", _PATH_RE.sub("<path>", str(exc)))
    except FileExistsError as exc:
        # flat+dir collision surfaced by the adopt guard (defense in depth).
        raise _error(
            409, "conflict", _PATH_RE.sub("<path>", str(exc)), reason_code="destination_exists"
        )
    except InvalidNameError:
        # The sibling version routes resolve the name outside any try, so an
        # invalid name reaches the app-level ValueError handler (string
        # ``detail``). Resolution here must stay under the gateway lock — so
        # re-raise instead of letting the generic ValueError arm below
        # re-shape the same input into the ``_error`` object envelope (#1519).
        raise
    except (OSError, ValueError) as exc:
        raise _error(400, "validation", _PATH_RE.sub("<path>", str(exc)))


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
                # Offloaded + lock-budgeted like create (the engine blocks on
                # the cross-process sidecar lock); the follow-up manifest read
                # rides the same worker thread.
                def _promote_and_load() -> versioning.VersionsManifest:
                    versioning.promote_label(
                        artifact_dir,
                        label,
                        body.version,
                        lock_timeout=_VERSIONS_LOCK_BUDGET_S,
                    )
                    return versioning.load_manifest(artifact_dir)

                manifest = await asyncio.to_thread(_promote_and_load)
    except TimeoutError:
        raise _error(503, "busy", "Label promote timed out — another sync may be in progress")
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
                # Same offload + lock budget as create/promote.
                def _delete_and_load() -> versioning.VersionsManifest:
                    versioning.delete_label(
                        artifact_dir, label, lock_timeout=_VERSIONS_LOCK_BUDGET_S
                    )
                    return versioning.load_manifest(artifact_dir)

                manifest = await asyncio.to_thread(_delete_and_load)
    except TimeoutError:
        raise _error(503, "busy", "Label delete timed out — another sync may be in progress")
    except VersionError as exc:
        raise _version_http(exc) from exc

    return {
        "name": name,
        "artifact_type": artifact_type,
        "target_scope": target_scope,
        "deleted_label": label,
        "labels": dict(manifest.labels),
    }
