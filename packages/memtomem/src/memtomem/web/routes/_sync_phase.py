"""Typed phase error shared by the per-type sync cores (ADR-0024, #1278).

The per-type sync handlers historically translated engine errors into
``HTTPException`` inline. With ``POST /api/context/sync-all`` aggregating
the same cores into a per-phase report, the translation needs to carry
TWO renderings of one failure:

- standalone per-type route: the historical ``status_code`` + ``detail``
  pair, byte-identical (the privacy 422 stays a *string* detail —
  issue-pinned; strict-drop keeps its ``{reason_code, message, generated}``
  dict);
- sync-all phase entry: the ADR-0023 §10 object envelope
  (``{error_kind, message, reason_code?, …}``) embedded in a 200 report.

``SyncPhaseError`` subclasses ``HTTPException`` so the standalone routes
need no ``except`` clause at all — FastAPI renders the legacy shape from
``status_code``/``detail`` untouched — while sync-all reads the extra
``error_kind`` / ``reason_code`` attributes to build the envelope without
guessing from status codes.

This module is a leaf on purpose: the cores (in ``context_skills`` /
``context_commands`` / ``context_agents`` / ``context_mcp_servers`` /
``settings_sync``) raise it and ``context_sync_all`` imports those cores,
so the exception class must not live in any of them (circular import).
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException


class SyncPhaseError(HTTPException):
    """An engine failure translated by a lock-free sync core.

    ``status_code`` and ``detail`` are exactly what the per-type route
    historically raised; ``error_kind`` (ADR-0023 §10 vocabulary) and
    ``reason_code`` exist only for the sync-all envelope rendering.
    """

    def __init__(
        self,
        status_code: int,
        detail: Any,
        *,
        error_kind: str,
        reason_code: str | None = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=detail)
        self.error_kind = error_kind
        self.reason_code = reason_code
