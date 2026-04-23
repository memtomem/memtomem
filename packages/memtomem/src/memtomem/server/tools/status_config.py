"""Tools: mem_stats, mem_status, mem_config, mem_embedding_reset, mem_version."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from memtomem import __version__
from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app_initialized
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register
from memtomem.server.helpers import _set_config_key

if TYPE_CHECKING:
    from memtomem.server.context import AppContext

logger = logging.getLogger(__name__)


@mcp.tool()
@tool_handler
async def mem_stats(
    ctx: CtxType = None,
) -> str:
    """Return current memory index statistics: total chunks, sources, and storage backend.

    Use this to quickly assess how many memories are indexed before searching.
    """
    app = await _get_app_initialized(ctx)
    data = await app.storage.get_stats()
    total_chunks = data.get("total_chunks", 0)
    total_sources = data.get("total_sources", 0)
    backend = app.config.storage.backend

    out = (
        f"Memory index statistics:\n"
        f"- Total chunks: {total_chunks}\n"
        f"- Total sources: {total_sources}\n"
        f"- Storage backend: {backend}"
    )

    # Surface live degraded-mode state so monitoring probes and the Web UI
    # can detect it without a second tool call. Reads from
    # ``storage.embedding_mismatch`` (not the startup-time
    # ``ctx.embedding_broken`` snapshot) so the line disappears as soon as
    # ``mem_embedding_reset`` clears the mismatch. See ``mem_status`` for
    # the full structured warning block.
    mismatch = getattr(app.storage, "embedding_mismatch", None)
    if mismatch is not None:
        stored = mismatch["stored"]
        cfg = mismatch["configured"]
        out += (
            "\n- Embedding: DEGRADED — "
            f"stored {stored['provider']}/{stored['model']} ({stored['dimension']}d) "
            f"vs configured {cfg['provider']}/{cfg['model']} ({cfg['dimension']}d). "
            'Run mem_embedding_reset(mode="apply_current") to repair.'
        )

    return out


@mcp.tool()
@tool_handler
async def mem_status(
    ctx: CtxType = None,
) -> str:
    """Show indexing statistics and current configuration summary.

    Reports storage backend, embedding info, chunk/source counts, and
    warns when orphaned source files are detected (files removed from
    disk but still indexed — run mem_cleanup_orphans to fix).

    When a configuration drift is detected (e.g. embedding dimension
    mismatch between the DB and the runtime config) the output carries
    a ``Warnings`` block whose entries follow this schema — kept stable
    across versions so external consumers (uptime probes, dashboards)
    can pattern-match on the keys:

    ``kind``    open enum describing the warning. Current values:
                ``embedding_dim_mismatch``. Future releases may add
                ``stale_index``, ``orphan_vectors``, etc. — consumers
                must tolerate unknown kinds rather than erroring.
    ``fix``     the canonical CLI command a user should run.
    ``doc``     a relative-path link into ``docs/guides/`` with the full
                remediation flow (see ``configuration.md#reset-flow``).

    Embedding-mismatch entries also include ``stored`` and ``configured``
    sub-blocks echoing the DB vs runtime provider/model/dimension so the
    user can see what changed without consulting another tool.
    """
    app = await _get_app_initialized(ctx)
    stats = await app.storage.get_stats()
    config = app.config

    stored = getattr(app.storage, "stored_embedding_info", None)
    if stored:
        emb_line = f"{stored['provider']} / {stored['model']}"
        dim_line = str(stored["dimension"])
    else:
        emb_line = f"{config.embedding.provider} / {config.embedding.model}"
        dim_line = str(config.embedding.dimension)

    lines = [
        "memtomem Status",
        "==============",
        f"Storage:   {config.storage.backend}",
        f"DB path:   {Path(config.storage.sqlite_path).expanduser()}",
        f"Embedding: {emb_line}",
        f"Dimension: {dim_line}",
        f"Top-K:     {config.search.default_top_k}",
        f"RRF k:     {config.search.rrf_k}",
        "",
        "Index stats",
        "-----------",
        f"Total chunks:  {stats['total_chunks']}",
        f"Source files:  {stats['total_sources']}",
    ]

    # Orphan check — count source files no longer on disk
    try:
        source_files = await app.storage.get_all_source_files()
        orphaned = sum(1 for sf in source_files if not sf.exists())
        if orphaned:
            lines[-1] = (
                f"Source files:  {stats['total_sources']} ({orphaned} orphaned — run mem_cleanup_orphans)"
            )
    except Exception:
        logger.debug("Orphan detection failed", exc_info=True)

    # Immutable fields — these cannot be changed via mem_config at runtime.
    # Surfacing them here so operators are not surprised when a `mm config set`
    # on one of these paths fails silently.
    lines.append("")
    lines.append("Immutable fields (set once at init)")
    lines.append("------------------------------------")
    lines.append(f"embedding.provider:  {config.embedding.provider}")
    lines.append(f"embedding.model:     {config.embedding.model or '(unset)'}")
    lines.append(f"embedding.dimension: {config.embedding.dimension}")
    lines.append(f"search.tokenizer:    {config.search.tokenizer}")
    lines.append(f"storage.backend:     {config.storage.backend}")
    lines.append(
        "  -> To change: re-run `mm init` for provider/tokenizer/backend, "
        "or `mm embedding-reset` to switch embedder (re-index required)."
    )

    mismatch = getattr(app.storage, "embedding_mismatch", None)
    if mismatch is not None:
        stored_info = mismatch["stored"]
        cfg = mismatch["configured"]
        lines.append("")
        lines.append("Warnings")
        lines.append("--------")
        lines.append("- kind:       embedding_dim_mismatch")
        lines.append(
            f"  stored:     {stored_info['provider']}/{stored_info['model']} "
            f"({stored_info['dimension']}d)"
        )
        lines.append(f"  configured: {cfg['provider']}/{cfg['model']} ({cfg['dimension']}d)")
        lines.append("  fix:        uv run mm embedding-reset --mode apply-current")
        lines.append("  doc:        docs/guides/configuration.md#reset-flow")

    return "\n".join(lines)


@mcp.tool()
@tool_handler
@register("advanced")
async def mem_config(
    key: str | None = None,
    value: str | None = None,
    persist: bool = False,
    ctx: CtxType = None,
) -> str:
    """View or update memtomem configuration values.

    Args:
        key: Dot-notation key to read or write (e.g. "search.default_top_k").
             If omitted, returns the full configuration as JSON.
        value: New value to assign. Omit to read the current value.
        persist: If True, save the change to ~/.memtomem/config.json so it
                 survives server restarts. Default is runtime-only.
    """
    app = await _get_app_initialized(ctx)

    if key and value is not None:
        result = _set_config_key(app.config, key, value)
        # Side effects for specific field changes
        if result.startswith("Set "):
            # Invalidate search cache so changes take effect immediately
            app.search_pipeline.invalidate_cache()
            # Rebuild FTS index when tokenizer changes
            if key == "search.tokenizer":
                from memtomem.storage.fts_tokenizer import set_tokenizer

                set_tokenizer(app.config.search.tokenizer)
                count = await app.storage.rebuild_fts()
                result += f"\nFTS index rebuilt ({count} chunks)."
            # Persist to disk if requested
            if persist:
                from memtomem.config import save_config_overrides

                save_config_overrides(app.config)
                result += " (persisted to config.json)"
            else:
                result += " (runtime only — not persisted)"
        return result

    config_dict = app.config.model_dump()

    if key:
        parts = key.split(".")
        node: object = config_dict
        for part in parts:
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return f"Key '{key}' not found in configuration."
        return f"{key} = {node}"

    import json

    def _serialize(obj: object) -> object:
        if isinstance(obj, Path):
            return str(obj)
        if isinstance(obj, (set, frozenset)):
            return list(obj)
        return obj

    return json.dumps(config_dict, indent=2, default=_serialize)


def _revert_to_stored(app: AppContext) -> str:
    """Switch the runtime embedder to match stored DB settings (non-destructive)."""
    from memtomem.embedding.factory import create_embedder
    from memtomem.indexing.engine import IndexEngine
    from memtomem.search.pipeline import SearchPipeline

    storage = app.storage
    config = app.config
    mismatch = storage.embedding_mismatch
    if mismatch is None:
        return "No mismatch detected — nothing to revert."

    stored = mismatch["stored"]

    config.embedding.provider = stored["provider"]
    config.embedding.model = stored["model"]
    config.embedding.dimension = stored["dimension"]

    # ``app.embedder`` / ``app.search_pipeline`` / ``app.index_engine`` are
    # read-only properties that proxy to ``app._components.<name>`` (#399
    # Phase 1). Direct assignment would raise ``AttributeError``. The
    # ``Components`` dataclass is mutable, so we swap fields on the inner
    # container and the properties pick up the new values automatically.
    # ``app.storage`` above already dereferenced ``_components``, so the
    # container is guaranteed non-None by the time we reach here.
    comp = app._components
    assert comp is not None, (
        "_revert_to_stored called before ensure_initialized — "
        "handler must go through _get_app_initialized"
    )
    new_embedder = create_embedder(config.embedding)
    comp.embedder = new_embedder
    comp.search_pipeline = SearchPipeline(
        storage=storage,
        embedder=new_embedder,
        config=config.search,
        decay_config=config.decay,
        mmr_config=config.mmr,
        access_config=config.access,
        context_window_config=config.context_window,
        llm_provider=app.llm_provider,
    )
    comp.index_engine = IndexEngine(
        storage=storage,
        embedder=new_embedder,
        config=config.indexing,
        namespace_config=config.namespace,
    )

    storage.clear_embedding_mismatch()

    return (
        f"Reverted to stored DB settings: "
        f"{stored['provider']}/{stored['model']} ({stored['dimension']}d). "
        f"Search should work normally now."
    )


@mcp.tool()
@tool_handler
@register("advanced")
async def mem_embedding_reset(
    mode: str = "status",
    ctx: CtxType = None,
) -> str:
    """Check or resolve embedding configuration mismatches between DB and current config.

    Args:
        mode: One of:
            - "status" (default): Show DB stored values vs current config.
            - "apply_current": Reset DB to current config. DESTRUCTIVE — deletes all vectors, re-index required.
            - "revert_to_stored": Switch runtime embedder to match DB stored values. Non-destructive.
    """
    app = await _get_app_initialized(ctx)

    if mode not in ("status", "apply_current", "revert_to_stored"):
        return f"Invalid mode '{mode}'. Use: status, apply_current, or revert_to_stored."

    stored = getattr(app.storage, "stored_embedding_info", None)
    mismatch = getattr(app.storage, "embedding_mismatch", None)
    config = app.config

    if mode == "status":
        lines = ["Embedding Status"]
        if stored:
            lines.append(
                f"  DB stored:  {stored['provider']}/{stored['model']} ({stored['dimension']}d)"
            )
        lines.append(
            f"  Config:     {config.embedding.provider}/{config.embedding.model} "
            f"({config.embedding.dimension}d)"
        )
        if mismatch is None:
            lines.append("\nNo mismatch — DB and config are in sync.")
        else:
            lines.append("\nWarning: Mismatch detected!")
            lines.append('  -> "apply_current": reset DB to config (destructive, re-index needed)')
            lines.append('  -> "revert_to_stored": switch embedder to match DB (non-destructive)')
        return "\n".join(lines)

    if mode == "apply_current":
        await app.storage.reset_embedding_meta(
            dimension=config.embedding.dimension,
            provider=config.embedding.provider,
            model=config.embedding.model,
        )
        return (
            f"DB reset to {config.embedding.provider}/{config.embedding.model} "
            f"({config.embedding.dimension}d). All vectors deleted — run mem_index to re-index."
        )

    # mode == "revert_to_stored"
    return _revert_to_stored(app)


@mcp.tool()
@tool_handler
@register("advanced")
async def mem_reset(
    confirm: bool = False,
    ctx: CtxType = None,
) -> str:
    """Delete ALL data (chunks, sessions, history, etc.) and reinitialize the DB.

    Embedding configuration is preserved. A re-index is required afterwards.

    Args:
        confirm: Must be True to proceed. Prevents accidental data loss.
    """
    if not confirm:
        app = await _get_app_initialized(ctx)
        stats = await app.storage.get_stats()
        total = stats.get("total_chunks", 0)
        return (
            f"Database has {total} chunks. "
            "This will permanently delete ALL data. "
            "Pass confirm=True to proceed."
        )

    app = await _get_app_initialized(ctx)
    deleted = await app.storage.reset_all()
    summary = ", ".join(f"{t}: {c}" for t, c in deleted.items() if c > 0)
    return f"Database reset complete. Deleted: {summary or 'empty'}. Run mem_index to re-index."


@tool_handler
@register("advanced")
async def mem_version(
    ctx: CtxType = None,
) -> str:
    """Return server version and supported capabilities for protocol negotiation.

    Used by external systems (e.g. memtomem-stm) to discover which features
    are available before using them. Callable via mem_do(action="version").
    """
    return json.dumps(
        {
            "version": __version__,
            "capabilities": {
                "search_formats": ["compact", "verbose", "structured"],
            },
        },
        ensure_ascii=False,
    )
