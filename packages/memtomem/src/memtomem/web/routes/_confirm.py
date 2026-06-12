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

A-6 (#1263) migrates the settings-sync host-write refusals onto this
helper; until then ``generate_all_settings`` keeps its engine-level
per-generator ``needs_confirmation`` results.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


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
