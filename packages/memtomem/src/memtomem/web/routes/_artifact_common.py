"""Shared policy helpers for the per-kind context route files (#1514).

Leaf module (like ``_errors`` / ``_locks`` / ``_confirm``): the per-kind route
files — skills / commands / agents / mcp-servers — import from here, never the
reverse. These helpers used to be copy-pasted per file ("mirrored here so each
route file stays self-contained"), and that self-containment is exactly what
let hardening fixes drift between siblings (#1514: the #1412 ``_safe_rel``
fix, the #1229/#1233 lenient reads, and the mtime-conflict envelope each
reached some copies and not others).

The request models here are bases: each per-kind module subclasses them under
its historical class name (``CommandCreateRequest``, ``SyncRequest``, …)
because the class name is the OpenAPI component name — a plain alias would
rename the published schema components. ``ImportRequest`` is the exception:
the three per-kind copies were byte-identical, so FastAPI already collapsed
them into one ``ImportRequest`` component; sharing the class keeps that name.
"""

from __future__ import annotations

from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from memtomem.config import TargetScope
from memtomem.context._runtime_targets import KNOWN_RUNTIMES, runtime_fanout_root
from memtomem.context._sync_atomic import ON_DROP_LEVELS
from memtomem.context.scope_resolver import ArtifactKind
from memtomem.web.routes._errors import _error

# ── Mtime conflict envelope ──────────────────────────────────────────────

MTIME_CONFLICT_REASON = "File was modified by another process. Reload and retry."


def mtime_conflict_response(current_mtime_ns: int) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "status": "aborted",
            "reason": MTIME_CONFLICT_REASON,
            "mtime_ns": str(current_mtime_ns),
            "error_kind": "conflict",
            "reason_code": "stale_mtime",
        },
    )


# ── Tier policy ──────────────────────────────────────────────────────────


def reject_project_local_write(target_scope: TargetScope, action: str) -> None:
    """Reject writes on the ``project_local`` tier with HTTP 400.

    Reads honor every tier (the canonical content differs per scope).
    Writes accept ``project_shared`` (ungated, today's default) and —
    since #1263 — ``user``, whose host-path writes complete only through
    the ``allow_host_writes`` disclose-then-confirm round-trip
    (:func:`memtomem.web.routes._confirm.host_write_gate`).
    ``project_local`` stays rejected: it is a gitignored draft tier with
    no runtime fan-out (ADR-0011 §3), so sync would be a no-op and its
    authoring policy is still deliberately deferred. Rejecting the
    explicit param is preferred over silently overriding the client's
    selection — silent override is exactly the cross-tier crossover
    (ADR-0011) P1 flagged ("mutates a same-named project_shared artifact
    while the UI is showing a user/local row").
    """
    if target_scope == "project_local":
        raise _error(
            400,
            "validation",
            (
                f"{action} is supported on the project_shared and user tiers; "
                f"project_local is a draft tier with no runtime fan-out "
                f"(ADR-0011 §3)."
            ),
            reason_code="project_local_unsupported",
        )


# ── Scan-dir hints ───────────────────────────────────────────────────────


def user_scan_dirs(kind: ArtifactKind) -> list[str]:
    """User-tier runtime roots the import scan reads (absolute, expanded).

    ``kind`` is the artifact-kind key ``runtime_fanout_root`` understands
    (``"skills"`` / ``"commands"`` / ``"agents"``). The project-relative
    scan-dir hints would lie on ``target_scope=user`` — the engine's
    user-tier import reads ``~/.claude/skills`` etc. via
    ``runtime_fanout_root``, not the project's runtime dirs.
    """
    dirs: list[str] = []
    for runtime in KNOWN_RUNTIMES:
        root = runtime_fanout_root(kind, runtime, "user", None)
        if root is not None:
            dirs.append(str(root))
    return sorted(set(dirs))


def scanned_dirs_for(
    target_scope: TargetScope, *, kind: ArtifactKind, project_scan_dirs: list[str]
) -> list[str]:
    """Where the import read from, for the list / import response hint."""
    return user_scan_dirs(kind) if target_scope == "user" else project_scan_dirs


# ── Request models ───────────────────────────────────────────────────────


class ArtifactCreateRequest(BaseModel):
    name: str
    content: str
    # #1263 host-write opt-in: required true for target_scope=user (the
    # canonical lands under ~/.memtomem/, outside any project root). The
    # first POST without it returns the needs_confirmation envelope.
    allow_host_writes: bool = False


class ArtifactUpdateRequest(BaseModel):
    content: str
    # mtime_ns is transported as a string (JS bigint-unsafe); parsed to int in handler.
    mtime_ns: str
    # Bypass the mtime guard. The Web UI sets this only after the user
    # explicitly chose "Force save" in the conflict resolution dialog
    # (see issue #763); every force-save emits a WARNING with both mtime
    # values for the audit trail.
    force: bool = False
    # #1263 host-write opt-in for target_scope=user (see ArtifactCreateRequest).
    allow_host_writes: bool = False


class ImportRequest(BaseModel):
    overwrite: bool = False
    # #1263 host-write opt-in for target_scope=user (the canonical
    # destination is ~/.memtomem/<kind>/, outside any project root).
    allow_host_writes: bool = False
    # Gate A bypass valve — mirrors the CLI's --force-unsafe-import and the
    # ``force_unsafe`` field the upload/memory/chunk web write surfaces already
    # expose. Lets a reviewed false positive (e.g. ``api_key: str`` type
    # annotations, ``secret_key=settings.x`` kwargs) proceed on the only
    # bypassable web import tier: ``user``. ``project_local`` is rejected
    # outright and ``project_shared`` hard-refuses regardless of this flag
    # (ADR-0011 §5 — git history is forever), enforced in the import engine.
    force_unsafe_import: bool = False


class HostWriteSyncRequest(BaseModel):
    # #1263 host-write opt-in for target_scope=user (see ArtifactCreateRequest).
    allow_host_writes: bool = False
    # Gate A bypass valve for fan-out — the sync-side mirror of
    # ImportRequest.force_unsafe_import (#1379). Lets a reviewed false
    # positive (e.g. an ``api_key: str`` type annotation in a doc) fan out
    # to the only bypassable tier: ``user``. ``project_local`` is rejected
    # outright and ``project_shared`` hard-refuses regardless of this flag
    # (ADR-0011 §5 — the engine's Gate A is authoritative).
    force_unsafe_sync: bool = False


class AtomicSyncRequest(HostWriteSyncRequest):
    on_drop: str = "warn"

    # An out-of-vocabulary value used to slip through to the engine, where it
    # silently behaved as "ignore" (#1247 id 47) — reject at the boundary
    # instead (FastAPI renders the ValueError as a native 422). Validates
    # against the engine's ON_DROP_LEVELS so the vocabulary has one owner
    # (no Literal duplication; CLI click.Choice and MCP _validate_on_drop
    # already gate the same way).
    @field_validator("on_drop")
    @classmethod
    def _check_on_drop(cls, value: str) -> str:
        if value not in ON_DROP_LEVELS:
            raise ValueError(f"on_drop must be one of {ON_DROP_LEVELS}, got {value!r}")
        return value
