"""Read-only wiki browser endpoints (ADR-0008 PR-E, prod tier).

The wiki (``~/.memtomem-wiki/``) is a GLOBAL single-host git repo, unlike the
per-project ``context_*`` surfaces. These routes therefore use none of the
project-scope machinery (``resolve_scope_root`` / ``target_scope`` / the
host-write gate) — they read the wiki working tree directly via
:class:`WikiStore`. All three endpoints are GET / read-only and mount in the
prod tier; the mutating override-seed verb stays CLI-only until a dev-tier
follow-up (ADR-0008 PR-E E-2).

Mirrors the read-only template of ``namespaces_read.py``: each handler wraps
the blocking git subprocess in :func:`asyncio.to_thread` and maps the model
layer's exceptions onto the shared ``_error`` envelope, so an absent or
malformed wiki is a precise status the UI can render — never a traceback
(ADR-0008 Invariant 3).
"""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, Query

from memtomem.context._names import (
    InvalidNameError,
    override_vendors,
    renderable_vendors,
    validate_name,
)
from memtomem.wiki import inspect as wiki_inspect
from memtomem.wiki.store import WikiNotFoundError, WikiStore
from memtomem.web.routes._errors import _error

router = APIRouter(prefix="/wiki", tags=["wiki"])

# Literal path param → FastAPI returns 422 for any other value, so a hostile
# ``asset_type`` can never reach ``lint_asset``'s ``store.root / asset_type``
# path join (the model layer validates ``name`` but not ``asset_type``).
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
    return _error(404, "missing", str(exc), reason_code="wiki_absent")


@router.get("")
async def list_wiki() -> dict:
    """List wiki assets with per-vendor override slots and renderability.

    ``vendors`` carries every registered ``(asset_type, vendor)`` override slot
    plus whether it is *renderable* — a non-renderable slot (the
    ``("commands", "codex")`` placeholder) has no generator and would only fail
    at diff/lint time, so the UI disables it. Diff/lint are NOT computed here
    (that would be O(assets × vendors) git calls); the UI fetches them lazily
    on selection.
    """
    store = WikiStore.at_default()

    def _collect() -> dict:
        assets = store.list_assets()
        return {
            "wiki_head": store.current_commit(),
            "wiki_root": store.root.as_posix(),
            "is_dirty": store.is_dirty(),
            "items": [
                {
                    "type": a.type,
                    "name": a.name,
                    "vendors": [
                        {"vendor": v, "renderable": v in renderable_vendors(a.type)}
                        for v in override_vendors(a.type)
                    ],
                }
                for a in assets
            ],
        }

    try:
        return await asyncio.to_thread(_collect)
    except WikiNotFoundError as exc:
        raise _wiki_absent(exc) from exc


@router.get("/{asset_type}/{name}/diff")
async def wiki_diff(asset_type: AssetType, name: str, vendor: str = Query(...)) -> dict:
    """Diff a committed override against the freshly rendered canonical baseline."""
    _validate_name_or_error(asset_type, name)
    _require_vendor(asset_type, vendor)
    store = WikiStore.at_default()

    def _diff() -> wiki_inspect.OverrideDiff:
        return wiki_inspect.diff_override(store, asset_type, name, vendor)

    try:
        result = await asyncio.to_thread(_diff)
    except WikiNotFoundError as exc:
        raise _wiki_absent(exc) from exc
    except NotImplementedError as exc:
        raise _error(400, "validation", str(exc), reason_code="vendor_unsupported") from exc
    except FileNotFoundError as exc:
        raise _error(
            404,
            "missing",
            f"{asset_type}/{name} not found in wiki",
            reason_code="asset_absent",
        ) from exc
    return {
        "override_path": result.override_path.as_posix(),
        "exists": result.exists,
        "in_sync": result.in_sync,
        "diff_lines": result.diff_lines,
        "dropped": result.dropped,
    }


@router.get("/{asset_type}/{name}/lint")
async def wiki_lint(asset_type: AssetType, name: str, vendor: str | None = Query(None)) -> dict:
    """Lint a wiki asset (canonical presence/parse, stray overrides, per-vendor).

    ``lint_asset`` returns every condition as a finding (never raises) except
    a missing wiki, so the only error path here is ``wiki_absent``.
    """
    _validate_name_or_error(asset_type, name)
    if vendor is not None:
        _require_vendor(asset_type, vendor)
    store = WikiStore.at_default()

    def _lint() -> wiki_inspect.LintReport:
        return wiki_inspect.lint_asset(store, asset_type, name, vendor)

    try:
        report = await asyncio.to_thread(_lint)
    except WikiNotFoundError as exc:
        raise _wiki_absent(exc) from exc
    return {
        "asset_type": report.asset_type,
        "name": report.name,
        "ok": report.ok,
        "findings": [{"level": f.level, "message": f.message} for f in report.findings],
    }
