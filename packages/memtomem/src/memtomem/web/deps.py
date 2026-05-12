"""FastAPI dependency injectors."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from fastapi import HTTPException, Query, Request

from memtomem.config import TargetTier

from memtomem.storage.sqlite_helpers import norm_path

# Mirror of the CLI bootstrap gate at
# ``packages/memtomem/src/memtomem/cli/_bootstrap.py`` (`_CONFIG_PATH`
# + the ``"memtomem is not configured"`` ClickException). The error
# message string is byte-identical; the predicate
# (``~/.memtomem/config.json`` existence) matches in shape but is
# recomputed on every call here, while bootstrap pins the path at
# module load (see ``require_configured`` docstring for the
# rationale). Issue #577 motivated this gate.

if TYPE_CHECKING:
    from memtomem.config import Mem2MemConfig
    from memtomem.embedding.base import EmbeddingProvider
    from memtomem.indexing.engine import IndexEngine
    from memtomem.search.pipeline import SearchPipeline
    from memtomem.storage.sqlite_backend import SqliteBackend


def get_storage(request: Request) -> SqliteBackend:
    return request.app.state.storage


def get_search_pipeline(request: Request) -> SearchPipeline:
    return request.app.state.search_pipeline


def get_index_engine(request: Request) -> IndexEngine:
    return request.app.state.index_engine


def get_embedder(request: Request) -> EmbeddingProvider:
    return request.app.state.embedder


def get_config(request: Request) -> Mem2MemConfig:
    return request.app.state.config


def get_dedup_scanner(request: Request):
    return request.app.state.dedup_scanner


def get_project_root(request: Request) -> Path:
    return request.app.state.project_root


def get_hooks_target_tier(request: Request) -> str:
    """Return ``hooks.target_tier`` from the live ``app.state.config``.

    ``hot_reload.py`` swaps ``app.state.config`` atomically on
    ``config.json`` edits, so handlers depending on this Depends
    automatically pick up tier changes without a server restart
    (mirrors :func:`get_config`).
    """
    return request.app.state.config.hooks.target_tier


def get_hooks_target_scope(request: Request) -> str:
    """Deprecated alias for callers not yet renamed."""
    return get_hooks_target_tier(request)


# ADR-0017: when a client sends BOTH ``?target_tier=`` and the deprecated
# ``?target_scope=``, the canonical name wins. The legacy alias is only the
# fallback for clients that haven't been renamed yet — letting it pre-empt the
# canonical query would invert the rename and let the deprecated surface
# silently override the new one. The default for ``target_tier`` is sentinel
# (``None`` for the optional helper, or ``"project_shared"`` for the required
# one materialized only when neither query was sent), so a bare
# ``?target_scope=...`` still routes to the legacy value as expected.
def get_query_target_tier(
    target_tier: TargetTier | None = Query(
        None,
        description=(
            "Canonical-residency tier to use. project_local is shown only "
            "when explicitly requested. Defaults to project_shared when "
            "neither this nor the deprecated target_scope alias is sent."
        ),
    ),
    target_scope: TargetTier | None = Query(
        None,
        deprecated=True,
        description="Deprecated alias for target_tier; ignored when target_tier is sent.",
    ),
) -> TargetTier:
    if target_tier is not None:
        return target_tier
    if target_scope is not None:
        return target_scope
    return "project_shared"


def get_optional_query_target_tier(
    target_tier: TargetTier | None = Query(
        None,
        description=(
            "Canonical-residency tier filter. Omit to show user and "
            "project_shared while hiding project_local."
        ),
    ),
    target_scope: TargetTier | None = Query(
        None,
        deprecated=True,
        description="Deprecated alias for target_tier; ignored when target_tier is sent.",
    ),
) -> TargetTier | None:
    return target_tier if target_tier is not None else target_scope


def require_configured() -> None:
    """Refuse mutating index routes when ``mm init`` has not run.

    Predicate: ``~/.memtomem/config.json`` exists. Path is recomputed
    on every call so test fixtures that monkeypatch ``HOME`` work
    naturally (the CLI bootstrap pins the path at module load — that's
    fine for a single CLI invocation but unhelpful for the long-lived
    web process and its tests).

    Raises HTTP 409 with the same message the CLI prints, so a
    direct-API caller, the Web UI's existing toast handler, and the
    CLI all see the same signal.
    """
    if not (Path.home() / ".memtomem" / "config.json").exists():
        raise HTTPException(
            status_code=409,
            detail="memtomem is not configured. Run 'mm init' to set up.",
        )


def require_indexed_source(user_path: str, indexed_sources: Iterable[Path]) -> Path:
    """Return the NFC-normalized Path for ``user_path`` if it matches any indexed source.

    Raises 403 when the path is not indexed. Normalizes both sides with
    ``norm_path`` (resolve + NFC) so an NFC user-typed path can still match
    an NFD on-disk path on macOS/APFS (issue #235).
    """
    request_norm = norm_path(Path(user_path))
    indexed_norms = {norm_path(p) for p in indexed_sources}
    if request_norm not in indexed_norms:
        raise HTTPException(status_code=403, detail="Path is not an indexed source file.")
    return Path(request_norm)
