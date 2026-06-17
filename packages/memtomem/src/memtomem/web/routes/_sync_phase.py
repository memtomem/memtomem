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

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse


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


async def _sync_phase_error_handler(request: Request, exc: SyncPhaseError) -> JSONResponse:
    """Render a standalone-route ``SyncPhaseError`` with its ``reason_code`` (#1409).

    The standalone per-type sync routes raise ``SyncPhaseError`` and let it
    propagate — ``context_sync_all`` catches it FIRST (``_run_phase`` ``except
    HTTPException``) to build its own per-phase envelope, so this handler only
    ever sees the standalone routes, never the sync-all aggregation.

    FastAPI's built-in ``HTTPException`` handler would emit ``{"detail": …}``
    and DROP the ``reason_code`` the core attached. The privacy 422 keeps a
    deliberately path-free *string* ``detail`` (#1385/#1387 issue-pinned), and
    the per-type sync route has OTHER 422 causes (``parse``, ``strict_drop``),
    so the client cannot tell a privacy block from a parse error by status code
    alone — it falls back to rendering the raw English ``PRIVACY_BLOCK_DETAIL``
    in the localized UI. Hoisting ``reason_code`` to a top-level sibling lets
    the client disambiguate and localize.

    We hoist ONLY for *string* details: the privacy 422 (and any future
    string-detail reason_code) gains a sibling ``reason_code`` while its detail
    stays byte-identical. Anything structured — today the ``agents``/``commands``
    ``strict_drop`` *dict* (``{reason_code, message, generated}``) which already
    embeds its ``reason_code`` for the client to read THERE, and any future
    non-string detail — keeps its bare ``{"detail": …}`` wire shape untouched,
    so this change cannot perturb the strict-drop contract or silently broaden a
    yet-unwritten structured detail. The ``isinstance(str)`` test (not merely
    ``not dict``) is deliberate: a string detail is the only shape that can't
    carry its own ``reason_code``, so it's the only one that needs the hoist.
    """
    content: dict[str, Any] = {"detail": exc.detail}
    if exc.reason_code is not None and isinstance(exc.detail, str):
        content["reason_code"] = exc.reason_code
    return JSONResponse(status_code=exc.status_code, content=content, headers=exc.headers or None)


def register_sync_phase_error_handler(app: FastAPI) -> None:
    """Install :func:`_sync_phase_error_handler` for ``SyncPhaseError``.

    A single registrar shared by the production factory (``web.app.create_app``)
    and the route tests, so the path-free + ``reason_code`` regression exercises
    the SAME handler the app ships (no hand-rolled test double to drift).
    """
    # Starlette types the handler's exception param as the base ``Exception``
    # (handlers are looked up by the class key, then invoked with the concrete
    # instance), so a precise ``SyncPhaseError`` annotation is contravariant
    # against the stub — the standard typed-FastAPI exception-handler idiom.
    app.add_exception_handler(SyncPhaseError, _sync_phase_error_handler)  # type: ignore[arg-type]
