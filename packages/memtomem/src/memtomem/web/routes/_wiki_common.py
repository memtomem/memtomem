"""Shared validators for the wiki web routes (ADR-0008 PR-E).

Leaf module (the ``_errors.py`` precedent): the read-only browser
(``wiki.py``, prod tier) and the override-seed mutation
(``wiki_mutations.py``, dev tier) validate the same ``asset_type`` / ``name``
/ ``vendor`` path inputs and map an absent wiki onto the same ``_error``
envelope. Keeping these here keeps the two sibling routers from drifting and
avoids a route↔route import (both depend on this leaf, not on each other).
"""

from __future__ import annotations

from typing import Literal

from memtomem.context._names import InvalidNameError, override_vendors, validate_name
from memtomem.wiki.store import WikiNotFoundError
from memtomem.web.routes._errors import _error

# Literal path param → FastAPI returns 422 for any other value, so a hostile
# ``asset_type`` can never reach a ``store.root / asset_type`` path join (the
# model layer validates ``name`` but not ``asset_type``).
AssetType = Literal["skills", "agents", "commands"]


def _validate_name_or_error(asset_type: str, name: str) -> None:
    try:
        validate_name(name, kind=f"{asset_type.removesuffix('s')} name")
    except InvalidNameError as exc:
        raise _error(400, "validation", str(exc), reason_code="invalid_name") from exc


def _require_vendor(asset_type: str, vendor: str) -> None:
    if vendor not in override_vendors(asset_type):
        raise _error(
            400,
            "validation",
            f"unknown vendor {vendor!r} for {asset_type}",
            reason_code="unknown_vendor",
        )


def _wiki_absent(exc: WikiNotFoundError) -> Exception:
    # Fixed message, NOT ``str(exc)`` — ``WikiNotFoundError`` embeds the absolute
    # wiki path (``wiki not found at <root>``), which would leak the host's
    # ``MEMTOMEM_WIKI_PATH`` into the HTTP envelope. The ``exc`` is still chained
    # via ``raise ... from exc`` at the call sites for server-side tracebacks.
    return _error(404, "missing", "wiki not found; run `mm wiki init`", reason_code="wiki_absent")
