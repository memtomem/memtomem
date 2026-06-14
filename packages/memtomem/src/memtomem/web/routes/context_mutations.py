"""Project-scoped wiki install/update mutations (ADR-0008 PR-E E-3, dev tier).

The read-only wiki browser (``wiki.py``) and the override-seed mutation
(``wiki_mutations.py``) both operate on the HOST-GLOBAL wiki and use no
project-scope machinery. These two verbs are different: ``mm context install``
and ``mm context update`` READ the wiki but WRITE into a *project's*
``.memtomem/`` tree, so this router is project-scoped — it resolves the target
project via :func:`resolve_project_shared_writable_scope_root` (which carries
the sync-eligibility gate) and mounts only in the dev tier
(``_DEV_ONLY_ROUTERS``). It is the web parity of the two CLI verbs, single
asset at a time (no ``--all`` batch yet — that stays CLI-only for now).

**Fixed-message envelopes (no ``str(exc)``):** the model-layer exceptions
(``AssetNotFoundError``, ``AlreadyInstalledError``, the Gate-A
``PrivacyBlockedError`` / ``PrivacyScanReadError``, the lockfile errors) embed
absolute wiki/dest paths in their text. That is fine for the CLI, but a
host-path leak over HTTP — so every handler maps them to a fixed message,
mirroring the ``_wiki_absent`` precedent in ``_wiki_common``. The ``exc`` is
still chained via ``raise ... from exc`` for server-side tracebacks.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from memtomem.context.install import (
    AlreadyInstalledError,
    AssetNotFoundError,
    InstallResult,
    NotInstalledError,
    StaleInstallError,
    UpdateResult,
    install_agent,
    install_command,
    install_skill,
    update_agent,
    update_command,
    update_skill,
)
from memtomem.context.lockfile import LockfileError
from memtomem.context.privacy_scan import PrivacyScanError
from memtomem.wiki.store import WikiNotFoundError, WikiStore
from memtomem.web.routes._errors import _error
from memtomem.web.routes._locks import _gateway_lock
from memtomem.web.routes._wiki_common import AssetType, _validate_name_or_error, _wiki_absent
from memtomem.web.routes.context_projects import resolve_project_shared_writable_scope_root

router = APIRouter(tags=["context-mutations"])

#: Engine-side budget for acquiring the project lockfile's sidecar lock,
#: forwarded to ``install_*`` / ``update_*`` → ``Lockfile.upsert_entry``. Kept
#: below the route's ``asyncio.timeout(60)`` so a contended lock makes the
#: worker thread self-abort (``_file_lock`` raises ``TimeoutError``) INSIDE the
#: request window — never an orphaned thread that writes ``.memtomem/`` after
#: the handler already returned 503 (the ``context_transfer`` lock-budget
#: precedent; #1145 / the ``_file_lock`` docstring).
_INSTALL_LOCK_BUDGET_S = 30.0

# Plural asset_type (the wiki / StatusRow vocabulary used everywhere on the
# wire) → the singular-named engine wrappers. Keeping the dispatch here lets the
# path param stay a single shared ``AssetType`` Literal (FastAPI 422s anything
# else before a path join) instead of three near-identical routes per verb.
_INSTALLERS = {"skills": install_skill, "agents": install_agent, "commands": install_command}
_UPDATERS = {"skills": update_skill, "agents": update_agent, "commands": update_command}


class UpdateAssetRequest(BaseModel):
    """Body for ``POST /context/{asset_type}/{name}/update``.

    ``force`` mirrors the CLI ``--force``: overwrite a dirty dest, preserving
    each locally edited file as a ``.bak`` sibling first. Install takes no body
    (a fresh install can never clobber — it refuses if the dest already exists).
    """

    force: bool = False


def _safe_rel(p: Path, project_root: Path) -> str:
    """Project-relative POSIX path for payloads; absolute fallback off-tree.

    Redefined here (not imported from ``context_skills``) so this dev-tier
    router never imports a prod router — the ``_wiki_common`` leaf precedent.
    ``.as_posix()`` keeps separators ``/`` on every platform (#1256).
    """
    try:
        return p.relative_to(project_root).as_posix()
    except ValueError:
        return p.as_posix()


def _privacy_blocked() -> Exception:
    # Fixed message, NOT ``str(exc)`` — the Gate-A block text embeds the absolute
    # wiki path of the offending file (the ``_gate_a_scan_*`` remediation hints),
    # which would leak the host's MEMTOMEM_WIKI_PATH into the HTTP envelope.
    return _error(
        422,
        "validation",
        "wiki asset blocked by the privacy scan: a secret was detected in the "
        "wiki bytes. Remove it from the wiki and retry.",
        reason_code="privacy_blocked",
    )


@router.post("/context/{asset_type}/{name}/install")
async def install_asset(
    asset_type: AssetType,
    name: str,
    project_root: Path = Depends(resolve_project_shared_writable_scope_root),
) -> dict:
    """Install a single wiki asset into the project (parity of ``mm context install``).

    Snapshots ``<wiki>/<type>/<name>/`` into ``<project>/.memtomem/<type>/<name>/``
    at the wiki HEAD and pins it in ``lock.json``. project_shared tier only — the
    engine has no tier axis (see the pinned resolver). Non-destructive: an
    already-installed asset refuses with 409 ``already_installed`` (use update).
    """
    _validate_name_or_error(asset_type, name)
    installer = _INSTALLERS[asset_type]

    def _run() -> InstallResult:
        return installer(
            project_root, name, wiki=WikiStore.at_default(), lock_timeout=_INSTALL_LOCK_BUDGET_S
        )

    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                result = await asyncio.to_thread(_run)
    except TimeoutError:
        raise _error(503, "busy", "install timed out — another sync may be in progress")
    except WikiNotFoundError as exc:
        raise _wiki_absent(exc) from exc
    except AssetNotFoundError as exc:
        raise _error(
            404, "missing", f"{asset_type}/{name} not found in wiki", reason_code="asset_absent"
        ) from exc
    except AlreadyInstalledError as exc:
        raise _error(
            409,
            "conflict",
            f"{asset_type}/{name} is already installed in this project; use update to refresh",
            reason_code="already_installed",
        ) from exc
    except PrivacyScanError as exc:
        raise _privacy_blocked() from exc
    except LockfileError as exc:
        raise _error(
            409, "conflict", "project lock.json is unreadable", reason_code="lockfile_corrupt"
        ) from exc
    return {
        "installed": True,
        "asset_type": result.asset_type,
        "name": result.name,
        "wiki_commit": result.wiki_commit,
        "installed_at": result.installed_at,
        "dest": _safe_rel(result.dest, project_root),
        "files_written": result.files_written,
        "files_removed": [_safe_rel(p, project_root) for p in result.files_removed],
    }


@router.post("/context/{asset_type}/{name}/update")
async def update_asset(
    asset_type: AssetType,
    name: str,
    body: UpdateAssetRequest | None = None,
    project_root: Path = Depends(resolve_project_shared_writable_scope_root),
) -> dict:
    """Refresh an installed wiki asset to wiki HEAD (parity of ``mm context update``).

    No-op when the lockfile pin already matches HEAD (``was_no_op=True``).
    Refuses with 409 ``stale_install`` when local edits would be clobbered; the
    client re-POSTs ``{"force": true}`` to overwrite (each dirty file kept as a
    ``.bak`` sibling). An asset with no lockfile entry → 404 ``not_installed``.
    """
    _validate_name_or_error(asset_type, name)
    force = body.force if body is not None else False
    updater = _UPDATERS[asset_type]

    def _run() -> UpdateResult:
        return updater(
            project_root,
            name,
            wiki=WikiStore.at_default(),
            force=force,
            lock_timeout=_INSTALL_LOCK_BUDGET_S,
        )

    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                result = await asyncio.to_thread(_run)
    except TimeoutError:
        raise _error(503, "busy", "update timed out — another sync may be in progress")
    except WikiNotFoundError as exc:
        raise _wiki_absent(exc) from exc
    except NotInstalledError as exc:
        raise _error(
            404,
            "missing",
            f"{asset_type}/{name} is not installed in this project; install it first",
            reason_code="not_installed",
        ) from exc
    except AssetNotFoundError as exc:
        raise _error(
            404, "missing", f"{asset_type}/{name} not found in wiki", reason_code="asset_absent"
        ) from exc
    except StaleInstallError as exc:
        raise _error(
            409,
            "conflict",
            f"{asset_type}/{name} has local edits; re-run with force to overwrite "
            "(each edited file is kept as a .bak sibling)",
            reason_code="stale_install",
        ) from exc
    except PrivacyScanError as exc:
        raise _privacy_blocked() from exc
    except LockfileError as exc:
        raise _error(
            409, "conflict", "project lock.json is unreadable", reason_code="lockfile_corrupt"
        ) from exc
    return {
        "updated": True,
        "asset_type": result.asset_type,
        "name": result.name,
        "was_no_op": result.was_no_op,
        "old_wiki_commit": result.old_wiki_commit,
        "new_wiki_commit": result.new_wiki_commit,
        "installed_at": result.installed_at,
        "dest": _safe_rel(result.dest, project_root),
        "files_written": result.files_written,
        "files_removed": [_safe_rel(p, project_root) for p in result.files_removed],
        "bak_file_count": len(result.bak_files_written),
        "bak_files": [_safe_rel(p, project_root) for p in result.bak_files_written],
    }
