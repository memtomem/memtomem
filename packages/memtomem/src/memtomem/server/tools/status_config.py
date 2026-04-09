"""Tools: mem_stats, mem_status, mem_config, mem_embedding_reset."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register
from memtomem.server.helpers import _set_config_key

if TYPE_CHECKING:
    from memtomem.server.context import AppContext


@mcp.tool()
@tool_handler
async def mem_stats(
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Return current memory index statistics: total chunks, sources, and storage backend.

    Use this to quickly assess how many memories are indexed before searching.
    """
    app = _get_app(ctx)
    data = await app.storage.get_stats()
    total_chunks = data.get("total_chunks", 0)
    total_sources = data.get("total_sources", 0)
    backend = app.config.storage.backend

    return (
        f"Memory index statistics:\n"
        f"- Total chunks: {total_chunks}\n"
        f"- Total sources: {total_sources}\n"
        f"- Storage backend: {backend}"
    )


@mcp.tool()
@tool_handler
async def mem_status(
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Show indexing statistics and current configuration summary."""
    app = _get_app(ctx)
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
            lines[-1] = f"Source files:  {stats['total_sources']} ({orphaned} orphaned — run mem_cleanup_orphans)"
    except Exception:
        pass

    mismatch = getattr(app.storage, "embedding_mismatch", None)
    if mismatch is not None:
        cfg = mismatch["configured"]
        lines.append("")
        lines.append("Warning: Embedding mismatch")
        lines.append(f"  Config:  {cfg['provider']}/{cfg['model']} ({cfg['dimension']}d)")
        lines.append(
            "  -> Run 'mm embedding-reset' (CLI) or mem_embedding_reset(mode=\"apply_current\") (MCP) to resolve."
        )

    return "\n".join(lines)


@mcp.tool()
@tool_handler
@register("advanced")
async def mem_config(
    key: str | None = None,
    value: str | None = None,
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """View or update memtomem configuration values at runtime.

    Args:
        key: Dot-notation key to read or write (e.g. "search.default_top_k").
             If omitted, returns the full configuration as JSON.
        value: New value to assign. Omit to read the current value.
    """
    app = _get_app(ctx)

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

    new_embedder = create_embedder(config.embedding)
    app.embedder = new_embedder

    app.search_pipeline = SearchPipeline(
        storage=storage,
        embedder=new_embedder,
        config=config.search,
        decay_config=config.decay,
        mmr_config=config.mmr,
        access_config=config.access,
        context_window_config=config.context_window,
    )
    app.index_engine = IndexEngine(
        storage=storage,
        embedder=new_embedder,
        config=config.indexing,
        namespace_config=config.namespace,
    )

    storage._dim_mismatch = None
    storage._model_mismatch = None

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
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Check or resolve embedding configuration mismatches between DB and current config.

    Args:
        mode: One of:
            - "status" (default): Show DB stored values vs current config.
            - "apply_current": Reset DB to current config. DESTRUCTIVE — deletes all vectors, re-index required.
            - "revert_to_stored": Switch runtime embedder to match DB stored values. Non-destructive.
    """
    app = _get_app(ctx)

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
        app.storage._dim_mismatch = None
        app.storage._model_mismatch = None
        return (
            f"DB reset to {config.embedding.provider}/{config.embedding.model} "
            f"({config.embedding.dimension}d). All vectors deleted — run mem_index to re-index."
        )

    # mode == "revert_to_stored"
    return _revert_to_stored(app)
