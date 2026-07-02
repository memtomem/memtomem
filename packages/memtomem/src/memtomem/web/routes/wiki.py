"""Read-only wiki browser endpoints (ADR-0008 PR-E, prod tier).

The wiki (``~/.memtomem-wiki/``) is a GLOBAL single-host git repo, unlike the
per-project ``context_*`` surfaces. These routes therefore use none of the
project-scope machinery (``resolve_scope_root`` / ``target_scope`` / the
host-write gate) — they read the wiki working tree directly via
:class:`WikiStore`. All three endpoints are GET / read-only and mount in the
prod tier; the mutating override-seed verb lives in the dev-tier sibling
``wiki_mutations.py`` (ADR-0008 PR-E E-2), sharing this module's validators
via the ``_wiki_common`` leaf.

Mirrors the read-only template of ``namespaces_read.py``: each handler wraps
the blocking git subprocess in :func:`asyncio.to_thread` and maps the model
layer's exceptions onto the shared ``_error`` envelope, so an absent or
malformed wiki is a precise status the UI can render — never a traceback
(ADR-0008 Invariant 3).
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Query

from memtomem.context._names import override_vendors, renderable_vendors
from memtomem.wiki import inspect as wiki_inspect
from memtomem.wiki.store import WikiNotFoundError, WikiStore, WikiUnbornHeadError
from memtomem.web.routes._errors import _error
from memtomem.web.routes._wiki_common import (
    AssetType,
    _require_vendor,
    _validate_name_or_error,
    _wiki_absent,
    _wiki_unborn,
)

router = APIRouter(prefix="/wiki", tags=["wiki"])


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
    except WikiUnbornHeadError as exc:
        raise _wiki_unborn(exc) from exc


@router.get("/status")
async def wiki_status() -> dict:
    """Lightweight wiki HEAD + dirty probe for the nav-level glance badge (#1417).

    Unlike :func:`list_wiki` this lists NO assets — it is a single HEAD read
    plus ``git status``, cheap enough to fire on every Context Gateway open so
    the sidebar can flag a wiki whose uncommitted edits ``mm context install``
    would not yet reach (``install`` reads committed git objects only). Declared
    before the ``/{asset_type}/...`` routes so the literal ``/status`` path can
    never be captured as an asset type.

    A missing wiki is the common onboarding state, not an error here: it returns
    ``present=False`` (the nav badge simply stays hidden) rather than the 404 the
    asset-listing routes raise, so the probe never surfaces an error toast.
    """
    store = WikiStore.at_default()

    def _probe() -> dict:
        return {
            "present": True,
            "wiki_head": store.current_commit(),
            "is_dirty": store.is_dirty(),
        }

    try:
        return await asyncio.to_thread(_probe)
    except WikiNotFoundError:
        return {"present": False, "wiki_head": None, "is_dirty": False}
    except WikiUnbornHeadError:
        # A commit-less wiki (clone of an empty remote) cannot be installed
        # from, so for the glance badge it is the same onboarding state as an
        # absent wiki — hidden, never an error toast.
        return {"present": False, "wiki_head": None, "is_dirty": False}


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
