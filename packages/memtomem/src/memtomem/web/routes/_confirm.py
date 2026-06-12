"""Shared disclose-then-confirm envelope for consequential gateway writes.

The shape is extracted from the ``settings_sync.py`` precedent (campaign
#1270: A-5 and A-6 share one host-write confirmation helper; A-5 landed
first and owns the extraction). Contract:

- The first POST without the required opt-in flag performs **no write**
  and returns HTTP 200 with ``status: "needs_confirmation"`` — consent
  is an application state, not a transport error (the settings promote
  route established the 200-with-status shape).
- ``confirm`` names the exact request-body flag the client must re-POST
  with after the user approves — machine-actionable, so the UI never
  parses prose to find out *how* to proceed.
- ``reason`` is the human disclosure. ``host_targets`` (host-write
  gates only) lists the absolute paths outside any project root that
  the confirmed request will write, mirroring
  ``generate_all_settings``'s per-target disclosure.
- Callers attach surface-specific context via ``extra`` (the transfer
  route nests its dry-run preview under ``plan``).

The artifact write/sync routes (skills / commands / agents, #1263)
gate their ``target_scope=user`` writes through :func:`host_write_gate`
below. Settings sync is the one deliberate hold-out: its refusal is
engine-level and per-generator (``generate_all_settings`` returns a
``needs_confirmation`` *result row* per runtime target, consumed by the
CLI prompt, the web route, and the dashboard's Sync All summary), so
migrating it onto this envelope is a behavior-visible refactor tracked
separately — not part of #1263.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from memtomem.config import TargetScope


def needs_confirmation_envelope(
    reason: str,
    *,
    confirm: str,
    host_targets: Sequence[str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build the ``needs_confirmation`` response body.

    ``confirm`` is the request-body flag (e.g. ``confirm_project_shared``,
    ``allow_host_writes``) whose ``true`` re-POST completes the round-trip.
    """
    payload: dict[str, Any] = {
        "status": "needs_confirmation",
        "confirm": confirm,
        "reason": reason,
    }
    if host_targets is not None:
        payload["host_targets"] = list(host_targets)
    payload.update(extra)
    return payload


def host_write_gate(
    target_scope: TargetScope,
    allow_host_writes: bool,
    *,
    action: str,
    host_targets: Sequence[str],
    **extra: Any,
) -> dict[str, Any] | None:
    """``needs_confirmation`` envelope for an unconfirmed user-tier write, else ``None``.

    The #1263 contract for the artifact write/sync routes: a
    ``target_scope=user`` request whose pending writes land on host paths
    (the ``~/.memtomem/<kind>/`` canonicals and the ``~/.claude/...``-family
    fan-out roots both live outside any project root) must disclose those
    paths and complete only on a confirmed re-request. Callers compute
    ``host_targets`` from the *pending* mutation — an empty sequence means
    the request would not write anything, and the gate stays open
    (``None``) so no-op requests (idempotent deletes, nothing-to-import
    imports, empty canonical sets) never prompt; cheap conflict checks
    (409 duplicate create, 404 missing update) likewise belong *before*
    this gate so a request that cannot succeed is refused, not confirmed.

    ``allow_host_writes`` is the request's opt-in flag: a body field on
    POST/PUT routes, a query parameter on DELETE (bodies on DELETE are
    client-hostile). The reason prose therefore says "re-send", not
    "re-POST". Project tiers pass through untouched (``None``) — their
    write policy is the caller's business.
    """
    if target_scope != "user" or allow_host_writes or not host_targets:
        return None
    return needs_confirmation_envelope(
        f"{action} targets the user tier — host paths outside any project "
        f"root. Re-send the request with allow_host_writes=true after "
        f"confirming with the user.",
        confirm="allow_host_writes",
        host_targets=host_targets,
        **extra,
    )
