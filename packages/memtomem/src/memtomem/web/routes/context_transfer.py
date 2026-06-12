"""Context gateway — cross-project / cross-tier artifact transfer (A-5 #1276).

One mutator: ``POST /api/context/{kind}/{name}/transfer`` — the web face
of :func:`memtomem.context.transfer.transfer_artifact` (ADR-0023; move or
copy one canonical artifact between tiers AND between projects). The
dashboard's per-artifact "Move/Copy to…" action (B-6 #1289) is built on
this endpoint.

Contracts pinned in ADR-0023 §10:

- **ADR-0015 §4d exception.** Every other create/update/delete/import
  route is cwd-locked; this route accepts a destination project selector
  in the request body (``to_project_scope_id``), bounded to exactly one
  artifact per invocation, destinations restricted to the registered
  discovery set (no typed-path consent valve — that is CLI-only), and
  the two risky tiers gated behind a disclose-then-confirm round-trip.
- **Two-step confirmation** (:mod:`memtomem.web.routes._confirm`):
  a ``project_shared`` destination requires ``confirm_project_shared``
  (Gate B), a ``user``-tier destination requires ``allow_host_writes``
  (host path outside any project root). The first POST without the flag
  writes nothing and returns the dry-run plan inside the
  ``needs_confirmation`` envelope.
- **Destination eligibility.** A project-tier destination whose
  discovered scope is not sync-eligible is refused 409 with the
  existing ``sync_paused`` / ``sync_not_enrolled`` reason-code shape —
  including the IMPLICIT destination (``to_project_scope_id`` omitted)
  when the *source* selector names a non-cwd discovered scope, so the
  implicit spelling of a destination can never write where the explicit
  spelling is refused.
- **Error envelope.** Every route-raised non-2xx detail is an object
  ``{error_kind, message, reason_code?, …}`` — the B-1 (#1284)
  vocabulary (``parse`` / ``permission`` / ``missing`` / ``internal``)
  plus the HTTP-semantic kinds ``validation`` (bad input/combination),
  ``conflict`` (409 state-refusal family) and ``busy`` (503). The one
  deliberate exception: ``PrivacyScanError`` keeps the standard
  project_shared block envelope (422 with a string detail) that every
  sync surface emits and the JS block path already renders.
- **Engine offload.** The transfer core runs in a worker thread under
  ``_gateway_lock`` + ``asyncio.timeout(60)``, with the engine's
  pair-lock waits bounded by ``_TRANSFER_LOCK_BUDGET_S`` (whole-call
  deadline, 30s < 60s) so a cross-process lock holder cannot leave an
  un-cancellable worker writing after the 503 (#1145 shape). The outer
  timeout can still expire mid-write — the 503 wording deliberately
  makes no no-commit claim.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Literal, cast

import click
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from memtomem.config import TargetScope
from memtomem.context._names import InvalidNameError, validate_name
from memtomem.context.migrate import (
    SCOPE_MIGRATABLE_KINDS,
    ArtifactNotFoundError,
    MigratePartialError,
)
from memtomem.context.privacy_scan import PrivacyScanError
from memtomem.context.projects import ProjectScope, compute_scope_id
from memtomem.context.scope_resolver import ArtifactKind
from memtomem.context.transfer import (
    TransferCollisionError,
    TransferResult,
    transfer_artifact,
)
from memtomem.web.routes._confirm import needs_confirmation_envelope
from memtomem.web.routes._locks import _gateway_lock
from memtomem.web.routes.context_gateway import _classify_exception, _redact_message
from memtomem.web.routes.context_projects import (
    _default_project_root,
    _discover_for,
    _resolve_selected_scope,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["context-transfer"])

#: Whole-call pair-lock acquisition budget forwarded to the engine
#: (``transfer_artifact(lock_timeout=…)``). Must stay below the route's
#: ``asyncio.timeout(60)`` so the worker self-aborts inside the request
#: window — the ``_SETTINGS_LOCK_BUDGET_S`` / ``_SKILLS_LOCK_BUDGET_S``
#: precedent.
_TRANSFER_LOCK_BUDGET_S = 30.0


def _error(status_code: int, error_kind: str, message: str, **extra: Any) -> HTTPException:
    """Object-envelope ``HTTPException`` (ADR-0023 §10 / B-1 #1284 shape)."""
    return HTTPException(
        status_code=status_code,
        detail={"error_kind": error_kind, "message": message, **extra},
    )


class TransferRequest(BaseModel):
    """Body for ``POST /context/{kind}/{name}/transfer`` (issue #1276)."""

    mode: Literal["move", "copy"]
    to_target_scope: TargetScope
    to_project_scope_id: str | None = None
    from_scope: TargetScope | None = None
    as_name: str | None = None
    confirm_project_shared: bool = False
    allow_host_writes: bool = False


def _resolve_source(
    request: Request, project_scope_id: str | None, scope_id: str | None
) -> tuple[Path, ProjectScope | None]:
    """Source ``(root, scope record)`` from the standard query selectors.

    Same resolution as the shared ``resolve_scope_root`` dependency
    (``_resolve_selected_scope`` is the single implementation), but this
    route needs the scope RECORD too — the implicit-destination
    eligibility gate reads ``sync_eligible`` off it. The helper's plain
    string 400/404 details are re-shaped into this route's object
    envelope; status codes and message text are preserved exactly.
    """
    try:
        scope = _resolve_selected_scope(request, project_scope_id, scope_id)
    except HTTPException as exc:
        kind = "missing" if exc.status_code == 404 else "validation"
        raise _error(exc.status_code, kind, str(exc.detail)) from exc
    if scope is None:
        return _default_project_root(request), None
    assert scope.root is not None  # _resolve_selected_scope 404s on a missing root
    return scope.root, scope


def _resolve_destination(
    request: Request,
    body: TransferRequest,
    src_root: Path,
    src_scope: ProjectScope | None,
) -> tuple[Path, ProjectScope | None]:
    """Destination ``(root, scope record)`` through the same discovery.

    ``to_project_scope_id=None`` → the source project (cross-tier,
    same-project transfer): the destination inherits the SOURCE's scope
    record so the eligibility gate below sees a paused source project
    even when the destination is only implicit. Message literals for
    the 404s mirror ``_resolve_selected_scope`` verbatim.
    """
    if body.to_project_scope_id is None:
        return src_root, src_scope
    for scope in _discover_for(request):
        if scope.scope_id != body.to_project_scope_id:
            continue
        if scope.root is None or scope.missing:
            raise _error(
                404,
                "missing",
                f"scope {body.to_project_scope_id!r} is registered but its root is missing",
            )
        return scope.root, scope
    raise _error(404, "missing", f"unknown project_scope_id: {body.to_project_scope_id!r}")


def _reject_ineligible_destination(scope: ProjectScope | None, to_scope: TargetScope) -> None:
    """409 a project-tier destination in a non-sync-eligible project.

    Same rule and reason-code shape as ``resolve_writable_scope_root``
    (#1203 §1i), applied to the transfer destination: a paused or
    never-enrolled project must not gain a canonical that can never fan
    out there. ``scope is None`` is the server cwd (always eligible —
    you cannot pause the running directory); user-tier destinations are
    host writes, not project runtime, and are gated by
    ``allow_host_writes`` instead.
    """
    if to_scope == "user" or scope is None or scope.sync_eligible:
        return
    paused = "known-projects" in scope.sources
    raise HTTPException(
        status_code=409,
        detail={
            "error_kind": "conflict",
            "reason_code": "sync_paused" if paused else "sync_not_enrolled",
            # Message wording verbatim from ``resolve_writable_scope_root`` —
            # sibling trust-UX prose must not drift (the JS matches on the
            # reason_code; the prose is what humans compare across surfaces).
            "message": (
                f"Project {scope.scope_id!r} is not enrolled for sync "
                + (
                    "(enrollment paused). Resume sync"
                    if paused
                    else "(discovery-only; never enrolled). Enroll the project"
                )
                + " from the Projects portal before writing to its runtime."
            ),
            "project_scope_id": scope.scope_id,
        },
    )


def _required_confirm(body: TransferRequest) -> tuple[str, str] | None:
    """``(flag, reason)`` for the gate the request still has to clear.

    The two gates are tier-keyed and therefore mutually exclusive.
    Gate B prose mirrors the CLI confirmation prompt; the host-write
    prose mirrors ``generate_all_settings``'s refusal shape.
    """
    if body.to_target_scope == "project_shared" and not body.confirm_project_shared:
        return (
            "confirm_project_shared",
            f"This will {body.mode} the canonical into the git-tracked "
            f"project_shared tier. Re-POST with confirm_project_shared=true "
            f"after confirming with the user.",
        )
    if body.to_target_scope == "user" and not body.allow_host_writes:
        return (
            "allow_host_writes",
            "The destination is the user tier — a host path outside any "
            "project root. Re-POST with allow_host_writes=true after "
            "confirming with the user.",
        )
    return None


def _scope_id_for(scope: TargetScope, root: Path | None) -> str | None:
    """Project scope_id for a result side; ``None`` for the global user tier."""
    if scope == "user" or root is None:
        return None
    return compute_scope_id(root)


def _serialize(result: TransferResult) -> dict[str, Any]:
    """Wire shape for one plan/apply result.

    Paths are absolute strings — relativization is ambiguous when two
    project roots (or a host path) are in play. ``dst_project_scope_id``
    is the issue-pinned field the UI uses for one-click follow-up sync.
    The provenance triple is the A-4 ``_skip_reasons`` contract: the
    human ``provenance_reason`` for tooltips, the stable
    ``provenance_reason_code`` for client matching.
    """
    return {
        "transferred": result.transferred,
        "kind": result.kind,
        "name": result.name,
        "dst_name": result.dst_name,
        "mode": result.mode,
        "from_scope": result.from_scope,
        "to_scope": result.to_scope,
        "layout": result.layout,
        "src_path": str(result.src_path),
        "dst_path": str(result.dst_path),
        "src_project_scope_id": _scope_id_for(result.from_scope, result.src_project_root),
        "dst_project_scope_id": _scope_id_for(result.to_scope, result.dst_project_root),
        "fanout_planned": [str(p) for p in result.fanout_planned],
        "fanout_cleaned": [str(p) for p in result.fanout_cleaned],
        "fanout_backed_up": [str(p) for p in result.fanout_backed_up],
        "needs_sync": result.needs_sync,
        "sync_command": result.sync_command,
        "notes": list(result.notes),
        "provenance": result.provenance,
        "provenance_reason": result.provenance_reason,
        "provenance_reason_code": result.provenance_reason_code,
    }


@router.post("/context/{kind}/{name}/transfer")
async def transfer_context_artifact(
    kind: str,
    name: str,
    body: TransferRequest,
    request: Request,
    project_scope_id: str | None = Query(default=None),
    scope_id: str | None = Query(default=None),
    dry_run: bool = Query(
        False,
        description=(
            "Preview the transfer without touching disk: returns the engine's "
            "dry-run plan (status='plan') regardless of confirmation flags. "
            "Mirrors the import routes' dry_run."
        ),
    ),
) -> dict:
    """Move or copy one canonical artifact between tiers and/or projects.

    Source = ``{kind}/{name}`` under the ``?project_scope_id=`` project
    (server cwd when omitted) at ``body.from_scope`` (auto-detected when
    omitted). Destination = ``body.to_target_scope`` under
    ``body.to_project_scope_id`` (the source project when omitted).
    Responses: ``status="plan"`` (dry_run), ``status="needs_confirmation"``
    (gate round-trip, plan nested under ``plan``), ``status="ok"``
    (applied). See the module docstring for the gate and error contracts.
    """
    if kind not in SCOPE_MIGRATABLE_KINDS:
        raise _error(
            400,
            "validation",
            f"unsupported kind for artifact transfer: {kind!r} "
            f"(use one of {SCOPE_MIGRATABLE_KINDS})",
        )
    kind_t = cast(ArtifactKind, kind)
    try:
        validate_name(name, kind=f"{kind[:-1]} name")
        if body.as_name is not None:
            validate_name(body.as_name, kind=f"{kind[:-1]} name")
    except InvalidNameError as exc:
        raise _error(400, "validation", str(exc)) from exc

    if body.to_project_scope_id is not None and body.to_target_scope == "user":
        raise _error(
            400,
            "validation",
            "to_project_scope_id cannot be combined with to_target_scope='user': "
            "the user tier is global (~/.memtomem), not per-project.",
        )

    src_root, src_scope = _resolve_source(request, project_scope_id, scope_id)
    dst_root, dst_scope = _resolve_destination(request, body, src_root, src_scope)
    _reject_ineligible_destination(dst_scope, body.to_target_scope)

    if (
        body.to_target_scope != "user"
        and dst_root.resolve() != src_root.resolve()
        and not (dst_root / ".memtomem").is_dir()
    ):
        # CLI-parity gate (#1274): a cross-project destination must already be
        # a memtomem project — don't seed a half-initialized store into an
        # arbitrary registered directory. Within-project transfers keep
        # migrate's implicit-store behavior.
        raise HTTPException(
            status_code=409,
            detail={
                "error_kind": "conflict",
                "reason_code": "no_memtomem_store",
                "message": (
                    f"destination project has no .memtomem/ store: {dst_root}. "
                    f"Initialize it first: cd {dst_root} && mm context init"
                ),
                "project_scope_id": body.to_project_scope_id,
            },
        )

    gate = None if dry_run else _required_confirm(body)
    apply_ = not dry_run and gate is None

    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                # Worker thread: the engine takes cross-process sidecar locks
                # (the artifact pair lock) that would otherwise block the event
                # loop AND keep the enclosing asyncio.timeout from firing. The
                # engine-side lock budget (30s < 60s) bounds those waits — see
                # the module docstring for the residual mid-write window.
                result = await asyncio.to_thread(
                    transfer_artifact,
                    kind_t,
                    name,
                    src_project_root=src_root,
                    from_scope=body.from_scope,
                    dst_project_root=None if body.to_target_scope == "user" else dst_root,
                    to_scope=body.to_target_scope,
                    mode=body.mode,
                    apply_=apply_,
                    surface="web_context_transfer",
                    new_name=body.as_name,
                    lock_timeout=_TRANSFER_LOCK_BUDGET_S,
                )
    except TimeoutError:
        # Either the route's own 60s window or the engine's lock budget.
        # The lock-budget path commits nothing by construction; the outer
        # window can expire mid-write, so no no-commit claim here.
        raise _error(
            503, "busy", "Transfer timed out — another sync or transfer may be in progress"
        )
    except PrivacyScanError as exc:
        # The standard project_shared block envelope (string detail) every
        # sync surface emits — issue-pinned exception to the object envelope.
        raise HTTPException(422, exc.message) from exc
    except ArtifactNotFoundError as exc:
        raise _error(404, "missing", exc.message) from exc
    except TransferCollisionError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error_kind": "conflict",
                "reason_code": "destination_exists",
                "message": exc.message,
            },
        ) from exc
    except MigratePartialError as exc:
        # Partial commit (EXDEV cleanup failure): the message carries the
        # manual-recovery steps and is deliberately NOT redacted — the paths
        # ARE the remediation, and the CLI prints the same text raw.
        raise _error(500, "internal", exc.message) from exc
    except InvalidNameError as exc:
        raise _error(400, "validation", str(exc)) from exc
    except click.ClickException as exc:
        # Engine-validated combinations (same store, rename outside copy,
        # multi-`name:` manifest, …) — bad request input, engine wording.
        raise _error(400, "validation", exc.message) from exc
    except Exception as exc:  # pragma: no cover - classified catch-all
        logger.error("transfer %s/%s failed", kind, name, exc_info=True)
        raise _error(500, _classify_exception(exc), _redact_message(str(exc))) from exc

    if gate is not None:
        flag, reason = gate
        host_targets = [str(result.dst_path)] if flag == "allow_host_writes" else None
        return needs_confirmation_envelope(
            reason,
            confirm=flag,
            host_targets=host_targets,
            plan=_serialize(result),
        )
    return {"status": "plan" if dry_run else "ok", **_serialize(result)}
