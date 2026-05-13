"""Filesystem listing endpoint for folder pickers.

Single endpoint ``GET /api/fs/list`` powers the Browse modal next to the
Index tab's Folder-mode path input and the Context Gateway Add Project picker.
It returns either the picker's roots view (no ``path``) or the subdirectories
of an allow-listed directory.

The default Index allow-list is ``~`` plus every ``config.indexing.memory_dirs``
entry. ``purpose=project`` adds project-discovery roots for Context Gateway
Add Project without widening the Index tab's existing picker scope. The
allow-list is a *discoverability* boundary, not a security one — ``mm web`` is
bound to localhost and the user can still type paths directly in surfaces that
offer free-form input. 422 here means "this path is outside the picker's
scope", not "you don't have permission". Keeping the status distinct from 403
lets the frontend map outside-scope replies to the picker-specific toast
without conflating with real auth failures elsewhere.
"""

from __future__ import annotations

import os
import unicodedata
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from memtomem.context.projects import KnownProjectsStore
from memtomem.storage.sqlite_helpers import norm_path
from memtomem.web.deps import get_config

router = APIRouter(prefix="/fs", tags=["fs"])


class FsEntry(BaseModel):
    name: str
    path: str


class FsListResponse(BaseModel):
    path: str | None
    parent: str | None
    is_root: bool
    entries: list[FsEntry]


def _dedupe_roots(candidates: list[Path]) -> list[Path]:
    """Return roots in first-seen order, normalized for comparison."""

    seen: set[str] = set()
    out: list[Path] = []
    for cand in candidates:
        key = norm_path(cand)
        if key in seen:
            continue
        seen.add(key)
        out.append(Path(key))
    return out


def _index_allow_list_roots(config) -> list[Path]:
    """Return the Index picker's allow-list roots in display order.

    Order is ``~`` first, then ``config.indexing.memory_dirs`` in config
    order. Duplicates (same NFC-normalized resolved path) are dropped on
    first occurrence so the response stays stable when ``~`` itself is also
    listed in ``memory_dirs``.
    """
    candidates: list[Path] = [Path.home()]
    candidates.extend(Path(d).expanduser() for d in config.indexing.memory_dirs)
    return _dedupe_roots(candidates)


def _known_project_parent_roots(config) -> list[Path]:
    cg = getattr(config, "context_gateway", None)
    known_projects_path = getattr(cg, "known_projects_path", None)
    if known_projects_path is None:
        return []

    roots: list[Path] = []
    for entry in KnownProjectsStore(Path(known_projects_path).expanduser()).load():
        parent = entry.root.expanduser().parent
        # Same filesystem-root guard as _project_allow_list_roots: a known
        # project registered at ``/foo`` would otherwise add ``/`` to the
        # picker's discovery roots, bypassing the protection used for the
        # server cwd's parent a few lines below.
        if parent != Path(parent.anchor):
            roots.append(parent)
    return roots


def _project_allow_list_roots(request: Request, config) -> list[Path]:
    """Return wider project-discovery roots for Context Gateway Add Project.

    This extends, rather than replaces, the Index roots so Home and configured
    memory dirs remain visible. Adding the server cwd's parent lets users pick
    sibling worktrees/projects without making the picker browse from ``/``.
    Known-project parents keep previously registered project families
    discoverable after restart.
    """
    candidates = _index_allow_list_roots(config)
    project_root = Path(getattr(request.app.state, "project_root", Path.cwd())).expanduser()
    project_parent = project_root.parent
    if project_parent != project_root and project_parent != Path(project_parent.anchor):
        candidates.append(project_parent)
    candidates.extend(_known_project_parent_roots(config))
    return _dedupe_roots(candidates)


def _allow_list_roots(request: Request, config, purpose: str) -> list[Path]:
    if purpose == "index":
        return _index_allow_list_roots(config)
    if purpose == "project":
        return _project_allow_list_roots(request, config)
    raise HTTPException(status_code=400, detail="invalid_picker_purpose")


def _display_name(p: Path) -> str:
    """Short label for a root entry; ``~/...`` if under home, else absolute."""
    home = Path(norm_path(Path.home()))
    p_norm = Path(norm_path(p))
    if p_norm == home:
        return "Home (~)"
    try:
        rel = p_norm.relative_to(home)
        return f"~/{rel}"
    except ValueError:
        return str(p_norm)


def _request_paths(raw: str) -> tuple[Path, Path]:
    """Compute the two views of a request path.

    The first is *symbolic*: NFC-normalized and lexically cleaned (``..``
    collapsed), but symlinks are NOT followed. This is what the response's
    ``path`` / ``parent`` fields carry and what ``iterdir`` runs against,
    so children inherit the symbolic prefix the user clicked. The second
    is *resolved*: ``.resolve()`` + NFC, used for the boundary check and
    the existence / is_dir gates so security stays anchored to the real
    on-disk location regardless of how many symlinks the symbolic path
    crosses.
    """
    expanded = Path(raw).expanduser()
    symbolic_str = unicodedata.normalize("NFC", os.path.normpath(str(expanded)))
    symbolic = Path(symbolic_str)
    resolved = Path(norm_path(expanded))
    return symbolic, resolved


def _inside_any_root(target: Path, roots: list[Path]) -> bool:
    for r in roots:
        try:
            if target == r or target.is_relative_to(r):
                return True
        except (ValueError, OSError):
            continue
    return False


@router.get("/list", response_model=FsListResponse)
async def list_directory(
    request: Request,
    path: str | None = Query(None),
    purpose: str = Query(
        "index",
        description="Picker purpose: index keeps the legacy memory-dir scope; project widens discovery for Add Project.",
    ),
    config=Depends(get_config),
) -> FsListResponse:
    roots = _allow_list_roots(request, config, purpose)

    if not path:
        return FsListResponse(
            path=None,
            parent=None,
            is_root=True,
            entries=[FsEntry(name=_display_name(r), path=str(r)) for r in roots],
        )

    symbolic, resolved = _request_paths(path)

    if not _inside_any_root(resolved, roots):
        raise HTTPException(status_code=422, detail="outside_picker_scope")
    if not resolved.exists():
        raise HTTPException(status_code=404, detail="not_found")
    if not resolved.is_dir():
        raise HTTPException(status_code=400, detail="not_a_directory")

    children: list[FsEntry] = []
    try:
        # Iterdir runs against the symbolic path so child Path objects
        # carry the symbolic prefix (e.g. ``…/ln_inside/foo`` instead of
        # the resolve target's ``…/alpha/foo``). The OS still follows the
        # symlink internally — only the prefix Python keeps in front of
        # each entry differs.
        iterator = symbolic.iterdir()
    except (PermissionError, OSError) as exc:
        raise HTTPException(status_code=400, detail="not_a_directory") from exc

    for child in iterator:
        try:
            if not child.is_dir():
                continue
            # Symlink-out exclusion: if the resolved target leaves every
            # allow-list root, drop the entry. Without this the user would
            # click an allow-list child and hit 422 — confusing, given the
            # entry was rendered as part of an allow-listed parent. Only
            # the boundary check uses .resolve; the response carries the
            # symbolic path so the visible tree matches what iterdir
            # reported.
            resolved_child = Path(norm_path(child))
            if not _inside_any_root(resolved_child, roots):
                continue
        except (PermissionError, OSError):
            continue
        children.append(FsEntry(name=child.name, path=unicodedata.normalize("NFC", str(child))))

    children.sort(key=lambda e: e.name.casefold())

    parent_path = symbolic.parent
    parent_str: str | None = None
    if parent_path != symbolic:
        # Boundary check on the resolved parent so a symlinked parent
        # whose target leaves the allow-list isn't reachable via Up. The
        # displayed parent stays symbolic so Up returns to a path the
        # user recognises from the breadcrumb.
        parent_resolved = Path(norm_path(parent_path))
        if _inside_any_root(parent_resolved, roots):
            parent_str = unicodedata.normalize("NFC", str(parent_path))

    return FsListResponse(
        path=unicodedata.normalize("NFC", str(symbolic)),
        parent=parent_str,
        is_root=False,
        entries=children,
    )
