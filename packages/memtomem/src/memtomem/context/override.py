from __future__ import annotations

from pathlib import Path
from typing import cast

from memtomem.config import TargetScope
from memtomem.context._names import OVERRIDE_FORMATS
from memtomem.context.scope_resolver import (
    ArtifactKind,
    ContextScopeError,
    canonical_artifact_dir,
)

__all__ = ["resolve"]


# ADR-0011 PR-E: scope lookup order. Narrow tier wins on tie-break,
# matching memory's project-aware default precedence (project_local
# overrides project_shared overrides user). Single source of truth so
# the migration / sync surfaces stay consistent.
_SCOPE_LOOKUP_ORDER: tuple[TargetScope, ...] = ("project_local", "project_shared", "user")


def resolve(
    project_root: Path,
    asset_type: str,
    name: str,
    vendor: str,
    *,
    scope: TargetScope | None = None,
) -> Path | None:
    """Returns the per-vendor override file for a canonical artifact.

    Layout (PRESERVED across scopes — ADR-0011 PR-E does not change this):

        <canonical_artifact_dir(asset_type, scope, project_root)>
          / <name> / "overrides" / f"{vendor}.{ext}"

    Args:
        project_root: Project root that owns the canonical subtree. For
            ``scope="user"`` the project_root is unused but still required
            (passed by every existing caller).
        asset_type: One of ``agents`` / ``skills`` / ``commands``.
        name: Artifact name (skill / agent / command identifier).
        vendor: Runtime vendor key (``claude`` / ``gemini`` / ``codex``)
            from :data:`memtomem.context._names.OVERRIDE_FORMATS`.
        scope: When set, look only in that scope. When ``None`` (default,
            preserves pre-PR-E behavior at the call sites in
            ``agents.py`` / ``skills.py`` / ``commands.py``), search
            narrow→broad in :data:`_SCOPE_LOOKUP_ORDER` and return the
            first hit (project_local > project_shared > user).

    Returns:
        Path to the override file if it exists, else ``None``.

    Reads ONLY from the canonical tree(s). ADR-0008 Invariant 1; ADR-0011
    PR-E extends the invariant from project_shared-only to all three
    scopes with narrow-wins tie-break.
    """
    fmt = OVERRIDE_FORMATS.get((asset_type, vendor))
    if fmt is None:
        return None
    _, ext = fmt
    artifact = cast(ArtifactKind, asset_type)

    scopes = (scope,) if scope is not None else _SCOPE_LOOKUP_ORDER
    for s in scopes:
        try:
            base = canonical_artifact_dir(artifact, s, project_root)
        except ContextScopeError:
            # ``scope="user"`` with project_root=None is fine — the user
            # base is independent of project_root. But ``project_*`` with
            # project_root=None raises; skip that scope rather than abort
            # the whole lookup.
            continue
        candidate = base / name / "overrides" / f"{vendor}.{ext}"
        if candidate.is_file():
            return candidate
    return None
