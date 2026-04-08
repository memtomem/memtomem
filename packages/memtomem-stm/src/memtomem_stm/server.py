"""STM MCP server — proxy gateway with proactive memory surfacing."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession

from memtomem_stm.config import STMConfig
from memtomem_stm.proxy.manager import ProxyManager
from memtomem_stm.proxy.metrics import TokenTracker
from memtomem_stm.surfacing.engine import SurfacingEngine
from memtomem_stm.surfacing.feedback import FeedbackTracker

logger = logging.getLogger(__name__)


@dataclass
class STMContext:
    """Dependency container for STM services."""

    config: STMConfig
    proxy_manager: ProxyManager
    tracker: TokenTracker
    surfacing_engine: SurfacingEngine | None
    feedback_tracker: FeedbackTracker | None


CtxType = Context[ServerSession, STMContext]

mcp = FastMCP(
    "memtomem-stm",
    instructions=(
        "Short-term memory proxy gateway with proactive memory surfacing. "
        "Proxies upstream MCP servers with response compression and caching, "
        "and automatically surfaces relevant memories from memtomem LTM."
    ),
)


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[STMContext]:
    config = STMConfig()

    # Initialize persistent metrics store
    from memtomem_stm.proxy.metrics_store import MetricsStore

    metrics_store: MetricsStore | None = None
    if config.proxy.metrics.enabled:
        metrics_store = MetricsStore(
            config.proxy.metrics.db_path.expanduser().resolve(),
            max_history=config.proxy.metrics.max_history,
        )
        metrics_store.initialize()

    tracker = TokenTracker(metrics_store=metrics_store)

    # Initialize surfacing engine (in-process mode with optional memtomem LTM)
    surfacing_engine: SurfacingEngine | None = None
    if config.surfacing.enabled:
        search_pipeline = None
        storage = None
        webhook_manager = None

        mcp_adapter = None

        if config.surfacing.ltm_mode == "mcp_client":
            # MCP Client mode: connect to a remote memtomem server
            try:
                from memtomem_stm.surfacing.mcp_client import McpClientSearchAdapter

                mcp_adapter = McpClientSearchAdapter(config.surfacing)
                await mcp_adapter.start()
                logger.info(
                    "Surfacing engine connected via MCP client to %s",
                    config.surfacing.ltm_mcp_command,
                )
            except Exception:
                logger.warning("MCP client surfacing initialization failed", exc_info=True)
        else:
            # In-process mode: import memtomem directly
            try:
                from memtomem.server.component_factory import create_components
                from memtomem.config import Mem2MemConfig

                ltm_config = Mem2MemConfig()
                comp = await create_components(ltm_config)
                search_pipeline = comp.search_pipeline
                storage = comp.storage
                logger.info("Surfacing engine connected to in-process LTM")
            except ImportError:
                logger.info("memtomem not installed — trying MCP client mode")
                # Fall back to MCP client
                try:
                    from memtomem_stm.surfacing.mcp_client import McpClientSearchAdapter

                    mcp_adapter = McpClientSearchAdapter(config.surfacing)
                    await mcp_adapter.start()
                    logger.info("Surfacing engine connected via MCP client fallback")
                except Exception:
                    logger.warning("MCP client fallback failed — surfacing disabled", exc_info=True)
            except Exception:
                logger.warning("Failed to initialize LTM for surfacing", exc_info=True)

        # Initialize feedback tracker before engine so it can be injected
        feedback_tracker: FeedbackTracker | None = None
        if config.surfacing.feedback_enabled:
            feedback_tracker = FeedbackTracker(config.surfacing)

        if search_pipeline is not None or mcp_adapter is not None:
            surfacing_engine = SurfacingEngine(
                config.surfacing,
                search_pipeline=search_pipeline,
                storage=storage,
                webhook_manager=webhook_manager,
                mcp_adapter=mcp_adapter,
                feedback_tracker=feedback_tracker,
            )
    else:
        feedback_tracker = None

    # Initialize proxy cache
    from memtomem_stm.proxy.cache import ProxyCache

    proxy_cache: ProxyCache | None = None
    if config.proxy.cache.enabled:
        proxy_cache = ProxyCache(
            config.proxy.cache.db_path.expanduser().resolve(),
            max_entries=config.proxy.cache.max_entries,
        )
        proxy_cache.initialize()

    # Langfuse (optional)
    langfuse_client = None
    try:
        from memtomem_stm.observability.tracing import init_langfuse

        langfuse_client = init_langfuse(config.langfuse)
    except ImportError:
        pass
    except Exception:
        logger.warning("Langfuse init failed, continuing without tracing", exc_info=True)

    # Initialize proxy manager with surfacing and cache
    proxy_manager = ProxyManager(
        config.proxy,
        tracker,
        surfacing_engine=surfacing_engine,
        cache=proxy_cache,
    )
    await proxy_manager.start()

    # Register proxy tools with upstream schema + annotations
    from memtomem_stm.proxy._fastmcp_compat import register_proxy_tool

    def _make_proxy_handler(pm: ProxyManager, server_name: str, tool_name: str):  # noqa: ANN202
        async def proxy_tool(**kwargs: object) -> str | list:
            return await pm.call_tool(server_name, tool_name, dict(kwargs))

        return proxy_tool

    for info in proxy_manager.get_proxy_tools():
        register_proxy_tool(
            mcp,
            _make_proxy_handler(proxy_manager, info.server, info.original_name),
            info,
        )

    ctx = STMContext(
        config=config,
        proxy_manager=proxy_manager,
        tracker=tracker,
        surfacing_engine=surfacing_engine,
        feedback_tracker=feedback_tracker,
    )
    try:
        yield ctx
    finally:
        for info in proxy_manager.get_proxy_tools():
            try:
                mcp.remove_tool(info.prefixed_name)
            except Exception:
                pass
        try:
            await proxy_manager.stop()
        except Exception:
            logger.warning("Failed to stop proxy manager", exc_info=True)
        for resource, name in [
            (proxy_cache, "proxy_cache"),
            (metrics_store, "metrics_store"),
            (feedback_tracker, "feedback_tracker"),
        ]:
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    logger.warning("Failed to close %s", name, exc_info=True)
        if mcp_adapter is not None:
            try:
                await mcp_adapter.stop()
            except Exception:
                logger.warning("Failed to stop MCP adapter", exc_info=True)
        if langfuse_client is not None:
            try:
                from memtomem_stm.observability.tracing import shutdown_langfuse

                shutdown_langfuse(langfuse_client)
            except Exception:
                pass


mcp._lifespan_handler = app_lifespan  # type: ignore[attr-defined]


def _get_ctx(ctx: CtxType) -> STMContext:
    return ctx.request_context.lifespan_context


# ---------------------------------------------------------------------------
# Tool: stm_proxy_stats
# ---------------------------------------------------------------------------


@mcp.tool()
async def stm_proxy_stats(
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Show token savings and cache statistics for proxied MCP tool calls."""
    app = _get_ctx(ctx)
    summary = app.tracker.get_summary()

    lines = [
        "STM Proxy Stats",
        "===============",
        f"Total calls:     {summary['total_calls']}",
        f"Original chars:  {summary['total_original_chars']}",
        f"Compressed:      {summary['total_compressed_chars']}",
        f"Savings:         {summary['total_savings_pct']:.1f}%",
        f"Cache hits:      {summary['cache_hits']}",
        f"Cache misses:    {summary['cache_misses']}",
    ]

    if summary["by_server"]:
        lines.append("\nBy server:")
        for name, s in summary["by_server"].items():
            lines.append(
                f"  {name}: {s['calls']} calls, "
                f"{s['original_chars']} → {s['compressed_chars']} chars "
                f"({s['savings_pct']:.1f}% saved)"
            )

    surfacing = app.surfacing_engine
    if surfacing is not None:
        lines.append(f"\nSurfacing: enabled (min_score={app.config.surfacing.min_score})")
    else:
        lines.append("\nSurfacing: disabled")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool: stm_proxy_select_chunks
# ---------------------------------------------------------------------------


@mcp.tool()
async def stm_proxy_select_chunks(
    key: str,
    sections: list[str],
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Retrieve selected sections from a TOC response.

    When a proxied tool returns a TOC (selective compression), use this tool
    to fetch only the sections you need.

    Args:
        key: The selection_key from the TOC response.
        sections: List of section keys to retrieve.
    """
    app = _get_ctx(ctx)
    return app.proxy_manager.select_chunks(key, sections)


# ---------------------------------------------------------------------------
# Tool: stm_proxy_read_more
# ---------------------------------------------------------------------------


@mcp.tool()
async def stm_proxy_read_more(
    key: str,
    offset: int = 0,
    limit: int | None = None,
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Read more content from a progressive delivery response.

    When a proxied tool response includes progressive delivery metadata
    (has_more=true), use this tool to fetch the next chunk of content.

    Args:
        key: The continuation key from the progressive response footer.
        offset: Character offset to start reading from (use next_offset from footer).
        limit: Max characters to return. Defaults to the configured chunk_size.
    """
    app = _get_ctx(ctx)
    return app.proxy_manager.read_more(key, offset, limit)


# ---------------------------------------------------------------------------
# Tool: stm_proxy_cache_clear
# ---------------------------------------------------------------------------


@mcp.tool()
async def stm_proxy_cache_clear(
    server: str | None = None,
    tool: str | None = None,
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Clear the proxy response cache.

    Args:
        server: If given, only clear entries for this upstream server prefix.
        tool: If given (with server), only clear entries for this specific tool.
    """
    app = _get_ctx(ctx)
    pm = app.proxy_manager
    if not hasattr(pm, "_cache") or pm._cache is None:
        return "Cache not enabled. Set proxy.cache.enabled = true in stm_proxy.json."

    removed = pm._cache.clear(server=server, tool=tool)
    if server and tool:
        return f"Cleared {removed} cache entries for {server}/{tool}."
    elif server:
        return f"Cleared {removed} cache entries for server '{server}'."
    return f"Cleared all {removed} cache entries."


# ---------------------------------------------------------------------------
# Tool: stm_surfacing_feedback
# ---------------------------------------------------------------------------


@mcp.tool()
async def stm_surfacing_feedback(
    surfacing_id: str,
    rating: str,
    memory_id: str | None = None,
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Provide feedback on proactively surfaced memories.

    This helps improve future surfacing relevance via auto-tuning.

    Args:
        surfacing_id: The surfacing ID shown in the memory section.
        rating: One of 'helpful', 'not_relevant', 'already_known'.
        memory_id: Optional specific memory chunk ID to rate.
    """
    app = _get_ctx(ctx)
    # Route through SurfacingEngine to trigger access boost for helpful feedback
    if app.surfacing_engine is not None:
        return await app.surfacing_engine.handle_feedback(surfacing_id, rating, memory_id)
    if app.feedback_tracker is None:
        return "Feedback tracking is not enabled."
    return app.feedback_tracker.record_feedback(surfacing_id, rating, memory_id)


# ---------------------------------------------------------------------------
# Tool: stm_surfacing_stats
# ---------------------------------------------------------------------------


@mcp.tool()
async def stm_surfacing_stats(
    tool: str | None = None,
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Show proactive surfacing statistics and feedback ratings.

    Args:
        tool: Optional filter by tool name.
    """
    app = _get_ctx(ctx)
    if app.feedback_tracker is None:
        return "Feedback tracking is not enabled."

    stats = app.feedback_tracker.get_stats(tool)

    lines = [
        "Surfacing Stats",
        "===============",
        f"Total surfacings: {stats['total_surfacings']}",
        f"Total feedback:   {stats['total_feedback']}",
    ]

    if stats["by_rating"]:
        lines.append("\nBy rating:")
        for rating, count in stats["by_rating"].items():
            lines.append(f"  {rating}: {count}")

    if stats["total_feedback"] > 0:
        helpful = stats["by_rating"].get("helpful", 0)
        pct = round(helpful / stats["total_feedback"] * 100, 1)
        lines.append(f"\nHelpfulness: {pct}%")

    if tool:
        lines.append(f"\n(filtered by tool: {tool})")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the STM MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
