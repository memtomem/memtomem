"""Shared error-envelope constructors for the context gateway (B-1 #1284).

This module is a leaf on purpose (the ``_sync_phase.py`` precedent): it owns
the ADR-0023 §10 object-detail envelope constructor ``_error`` plus the
exception classifier / message redactor it relies on, and imports nothing
from the ``web.routes`` package. ``context_transfer`` used to own ``_error``
while ``context_gateway`` owned the classifier/redactor, which forced
``context_gateway`` to hand-roll its tier-gate envelope inline to dodge a
circular import (``context_transfer`` already imports ``context_gateway``).
Pulling all three into this leaf lets every surface — gateway, transfer,
sync-all, settings-sync, and the retrofitted per-type routes — import one
envelope constructor with no cycle.

The wire shape (FastAPI nests the ``detail`` under ``{"detail": {...}}``)::

    {"error_kind": <vocabulary>, "message": <str>, **extra}

``error_kind`` vocabulary = the classifier four (``parse`` / ``permission`` /
``missing`` / ``internal``) plus the HTTP-semantic kinds ``validation`` /
``conflict`` / ``busy``. ``extra`` carries optional keys such as
``reason_code`` and ``project_scope_id``. The one deliberate exception to the
object envelope is the privacy 422 block, which keeps a *string* detail
(issue-pinned) and therefore never routes through ``_error``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from memtomem.privacy import scan as _privacy_scan

try:
    import tomllib
except ImportError:  # pragma: no cover — py<3.11 fallback, repo targets py312
    tomllib = None  # type: ignore[assignment]

try:
    import yaml  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover — PyYAML may be absent on minimal installs
    yaml = None  # type: ignore[assignment]

_HOME = str(Path.home())
_ERROR_MESSAGE_LIMIT = 200
_SECRET_REDACTED_MARKER = "<redacted: secret-shape>"


def _classify_exception(exc: BaseException) -> str:
    """Map an exception to one of {parse, permission, missing, internal}.

    Order matters: ``PermissionError`` and ``FileNotFoundError`` are both
    ``OSError`` subclasses, so they must be checked before bare ``OSError``.
    Generic ``OSError`` is ``internal`` rather than ``permission``/``missing``
    because ``errno`` may be ``EIO``/``EMFILE``/``ELOOP`` etc.
    """
    if isinstance(exc, PermissionError):
        return "permission"
    if isinstance(exc, (FileNotFoundError, NotADirectoryError, IsADirectoryError)):
        return "missing"
    if isinstance(exc, ModuleNotFoundError):
        return "missing"
    if isinstance(exc, UnicodeDecodeError):
        return "parse"
    if isinstance(exc, json.JSONDecodeError):
        return "parse"
    if tomllib is not None and isinstance(exc, tomllib.TOMLDecodeError):
        return "parse"
    if yaml is not None and isinstance(exc, yaml.YAMLError):
        return "parse"
    return "internal"


def _redact_message(message: str) -> str:
    """Collapse ``$HOME`` → ``~``, drop secret-shape messages, then truncate.

    The ``internal`` classification is a catch-all for unexpected
    exceptions, so ``str(exc)`` may incidentally contain provider tokens,
    PEM headers, or ``api_key=...`` fragments pulled from a config parse
    or a third-party library's error. Truncation alone leaves the first
    200 chars verbatim, which is not enough at this trust boundary.

    We reuse the LTM secret-class scanner from ``memtomem.privacy``. If
    *any* hit is detected, the whole message is replaced with a fixed
    marker. Span-splicing was considered and rejected: several patterns
    (notably ``api_key=...``) match the assignment anchor only, so the
    secret *value* would survive a span splice. Whole-message replace
    matches the convention already established in
    ``privacy._sanitize_audit_value``. The ``error_kind`` field still
    tells the operator which category the failure fell into.
    """
    redacted = message.replace(_HOME, "~") if _HOME else message
    if _privacy_scan(redacted):
        return _SECRET_REDACTED_MARKER
    if len(redacted) > _ERROR_MESSAGE_LIMIT:
        redacted = redacted[:_ERROR_MESSAGE_LIMIT]
    return redacted


def _error(status_code: int, error_kind: str, message: str, **extra: Any) -> HTTPException:
    """Object-envelope ``HTTPException`` (ADR-0023 §10 / B-1 #1284 shape)."""
    return HTTPException(
        status_code=status_code,
        detail={"error_kind": error_kind, "message": message, **extra},
    )
