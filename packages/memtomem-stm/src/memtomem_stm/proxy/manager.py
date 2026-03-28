"""Proxy manager — upstream MCP server connection, tool discovery, and forwarding."""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from memtomem_stm.proxy.cache import ProxyCache
    from memtomem_stm.proxy.protocols import FileIndexer
    from memtomem_stm.surfacing.engine import SurfacingEngine

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client

from memtomem_stm.proxy.cleaning import DefaultContentCleaner
from memtomem_stm.proxy.compression import (
    HybridCompressor,
    LLMCompressor,
    SelectiveCompressor,
    TruncateCompressor,
    get_compressor,
)
from memtomem_stm.proxy.config import (
    CleaningConfig,
    CompressionStrategy,
    HybridConfig,
    LLMCompressorConfig,
    ProxyConfig,
    ProxyConfigLoader,
    SelectiveConfig,
    TransportType,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.metrics import CallMetrics, TokenTracker

# JSON-RPC error codes that indicate bad input, not connection problems.
# Retrying these wastes time and can damage the connection.
_NO_RETRY_CODES = {-32600, -32601, -32602, -32603}  # INVALID_REQUEST/METHOD/PARAMS/INTERNAL

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProxyToolInfo:
    prefixed_name: str
    description: str
    input_schema: dict[str, Any]
    server: str
    original_name: str
    annotations: Any = None  # MCP ToolAnnotations (readOnlyHint, destructiveHint, etc.)


@dataclass(frozen=True, slots=True)
class ToolConfig:
    """Resolved per-tool configuration for compression/indexing."""

    compression: CompressionStrategy
    max_chars: int
    llm: LLMCompressorConfig | None
    auto_index_enabled: bool
    selective: SelectiveConfig | None
    cleaning: CleaningConfig
    hybrid: HybridConfig | None


@dataclass
class UpstreamConnection:
    name: str
    config: UpstreamServerConfig
    session: ClientSession
    tools: list[Any]
    stack: AsyncExitStack | None = None


class ProxyManager:
    def __init__(
        self,
        config: ProxyConfig,
        tracker: TokenTracker,
        index_engine: FileIndexer | None = None,
        surfacing_engine: SurfacingEngine | None = None,
        cache: ProxyCache | None = None,
    ) -> None:
        self._config_loader = ProxyConfigLoader(config.config_path)
        self._config_loader.seed(config)
        self.tracker = tracker
        self._index_engine = index_engine
        self._surfacing_engine = surfacing_engine
        self._cache = cache
        self._connections: dict[str, UpstreamConnection] = {}
        self._stack: AsyncExitStack | None = None
        self._selective_compressor: SelectiveCompressor | None = None
        self._selective_lock = asyncio.Lock()

    async def start(self) -> None:
        """Connect to all upstream servers, discover their tools."""
        self._stack = AsyncExitStack()

        servers = self._config.upstream_servers
        if not servers:
            loaded = ProxyConfig.load_from_file(self._config.config_path)
            servers = loaded.upstream_servers

        seen_prefixed: set[str] = set()
        for name, cfg in servers.items():
            try:
                await self._connect_server(name, cfg, seen_prefixed)
            except Exception:
                logger.exception("Failed to connect to upstream server '%s'", name)

    def _open_transport(self, cfg: UpstreamServerConfig):  # noqa: ANN201
        match cfg.transport:
            case TransportType.SSE:
                return sse_client(cfg.url, headers=cfg.headers)
            case TransportType.STREAMABLE_HTTP:
                return streamablehttp_client(cfg.url, headers=cfg.headers)
            case _:
                return stdio_client(
                    StdioServerParameters(command=cfg.command, args=cfg.args, env=cfg.env)
                )

    async def _connect_server(
        self, name: str, cfg: UpstreamServerConfig, seen_prefixed: set[str]
    ) -> None:
        if self._stack is None:
            raise RuntimeError("ProxyManager.start() not called")

        if cfg.transport != TransportType.STDIO and not cfg.url:
            logger.warning("Skipping server '%s': transport=%s requires url", name, cfg.transport)
            return

        transport_ctx = self._open_transport(cfg)
        streams = await self._stack.enter_async_context(transport_ctx)
        read, write = streams[0], streams[1]
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()

        result = await session.list_tools()
        valid_tools = []
        for t in result.tools:
            prefixed = f"{cfg.prefix}__{t.name}"
            if prefixed in seen_prefixed:
                logger.warning("Skipping duplicate tool: %s", prefixed)
                continue
            seen_prefixed.add(prefixed)
            valid_tools.append(t)

        self._connections[name] = UpstreamConnection(
            name=name, config=cfg, session=session, tools=valid_tools
        )
        logger.info("Connected to '%s' (%s tools)", name, len(valid_tools))

    async def _reconnect_server(self, name: str) -> None:
        conn = self._connections[name]
        cfg = conn.config

        if conn.stack is not None:
            try:
                await conn.stack.aclose()
            except Exception:
                logger.debug("Failed to close previous stack for '%s'", name, exc_info=True)

        conn_stack = AsyncExitStack()
        transport_ctx = self._open_transport(cfg)
        streams = await conn_stack.enter_async_context(transport_ctx)
        read, write = streams[0], streams[1]
        session = await conn_stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        await session.list_tools()

        conn.session = session
        conn.stack = conn_stack
        logger.info("Reconnected to '%s'", name)

    async def stop(self) -> None:
        for conn in self._connections.values():
            if conn.stack is not None:
                try:
                    await conn.stack.aclose()
                except Exception:
                    logger.debug("Failed to close connection stack", exc_info=True)
        if self._stack:
            await self._stack.aclose()
            self._stack = None
        self._connections.clear()

    @property
    def _config(self) -> ProxyConfig:
        return self._config_loader.get()

    def get_proxy_tools(self) -> list[ProxyToolInfo]:
        result: list[ProxyToolInfo] = []
        for conn in self._connections.values():
            for t in conn.tools:
                result.append(
                    ProxyToolInfo(
                        prefixed_name=f"{conn.config.prefix}__{t.name}",
                        description=t.description or "",
                        input_schema=t.inputSchema or {"type": "object"},
                        server=conn.name,
                        original_name=t.name,
                        annotations=getattr(t, "annotations", None),
                    )
                )
        return result

    def _resolve_tool_config(self, server: str, tool: str) -> ToolConfig:
        conn = self._connections[server]
        cfg = conn.config

        compression = cfg.compression
        max_chars = cfg.max_result_chars
        llm_cfg = cfg.llm
        sel_cfg = cfg.selective
        hybrid_cfg = cfg.hybrid
        cleaning_cfg = cfg.cleaning or CleaningConfig()

        auto_index_enabled = self._config.auto_index.enabled
        if cfg.auto_index is not None:
            auto_index_enabled = cfg.auto_index

        override = cfg.tool_overrides.get(tool)
        if override is not None:
            if override.compression is not None:
                compression = override.compression
            if override.max_result_chars is not None:
                max_chars = override.max_result_chars
            if override.llm is not None:
                llm_cfg = override.llm
            if override.selective is not None:
                sel_cfg = override.selective
            if override.hybrid is not None:
                hybrid_cfg = override.hybrid
            if override.cleaning is not None:
                cleaning_cfg = override.cleaning
            if override.auto_index is not None:
                auto_index_enabled = override.auto_index

        return ToolConfig(
            compression=compression,
            max_chars=max_chars,
            llm=llm_cfg,
            auto_index_enabled=auto_index_enabled,
            selective=sel_cfg,
            cleaning=cleaning_cfg,
            hybrid=hybrid_cfg,
        )

    def _clean_content(self, text: str, cleaning_cfg: CleaningConfig) -> str:
        if not cleaning_cfg.enabled:
            return text
        return DefaultContentCleaner(cleaning_cfg).clean(text)

    async def _apply_compression(
        self,
        text: str,
        compression: CompressionStrategy,
        max_chars: int,
        sel_cfg: SelectiveConfig | None,
        llm_cfg: LLMCompressorConfig | None,
        hybrid_cfg: HybridConfig | None,
        server: str,
        tool: str,
    ) -> str:
        if compression == CompressionStrategy.HYBRID:
            return await self._apply_hybrid(text, max_chars, hybrid_cfg, sel_cfg)

        if compression == CompressionStrategy.SELECTIVE:
            async with self._selective_lock:
                if self._selective_compressor is None:
                    kwargs: dict[str, Any] = {}
                    if sel_cfg is not None:
                        kwargs = {
                            "max_pending": sel_cfg.max_pending,
                            "pending_ttl_seconds": sel_cfg.pending_ttl_seconds,
                            "json_depth": sel_cfg.json_depth,
                            "min_section_chars": sel_cfg.min_section_chars,
                        }
                    self._selective_compressor = SelectiveCompressor(**kwargs)
            return self._selective_compressor.compress(text, max_chars=max_chars)

        if compression == CompressionStrategy.LLM_SUMMARY:
            if llm_cfg is not None:
                return await LLMCompressor(llm_cfg).compress(text, max_chars=max_chars)
            logger.warning(
                "LLM_SUMMARY requested for %s/%s but no llm config found; falling back to truncate",
                server,
                tool,
            )
            return TruncateCompressor().compress(text, max_chars=max_chars)

        return get_compressor(compression).compress(text, max_chars=max_chars)

    async def _apply_surfacing(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        text: str,
    ) -> str:
        """Apply proactive memory surfacing if eligible."""
        if self._surfacing_engine is None:
            return text
        try:
            return await self._surfacing_engine.surface(
                server=server,
                tool=tool,
                arguments=arguments,
                response_text=text,
            )
        except Exception:
            logger.warning(
                "Surfacing failed for %s/%s, using compressed response",
                server,
                tool,
                exc_info=True,
            )
            return text

    async def _apply_hybrid(
        self,
        text: str,
        max_chars: int,
        hybrid_cfg: HybridConfig | None,
        sel_cfg: SelectiveConfig | None,
    ) -> str:
        cfg = hybrid_cfg or HybridConfig()
        async with self._selective_lock:
            if self._selective_compressor is None:
                kw: dict[str, Any] = {}
                if sel_cfg is not None:
                    kw = {
                        "max_pending": sel_cfg.max_pending,
                        "pending_ttl_seconds": sel_cfg.pending_ttl_seconds,
                        "json_depth": sel_cfg.json_depth,
                        "min_section_chars": sel_cfg.min_section_chars,
                    }
                self._selective_compressor = SelectiveCompressor(**kw)

        compressor = HybridCompressor(
            head_chars=cfg.head_chars,
            tail_mode=cfg.tail_mode,
            min_toc_budget=cfg.min_toc_budget,
            min_head_chars=cfg.min_head_chars,
            head_ratio=cfg.head_ratio,
            selective_compressor=self._selective_compressor,
        )
        return compressor.compress(text, max_chars=max_chars)

    async def _auto_index_response(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        text: str,
        agent_summary: str,
        compression_strategy: str | None = None,
        original_chars: int | None = None,
        compressed_chars: int | None = None,
        context_query: str | None = None,
    ) -> str:
        if self._index_engine is None:
            raise RuntimeError("index_engine not available")

        ai_cfg = self._config.auto_index
        memory_dir = ai_cfg.memory_dir.expanduser().resolve()
        memory_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        safe_tool = tool.replace("/", "_")
        fname = f"{server}__{safe_tool}__{ts}.md"
        file_path = memory_dir / fname

        args_str = ", ".join(f"{k}={v!r}" for k, v in arguments.items()) if arguments else "(none)"

        frontmatter_lines = [
            "---",
            f"source: proxy/{server}/{tool}",
            f"timestamp: {datetime.now(timezone.utc).isoformat()}",
        ]
        if compression_strategy is not None:
            frontmatter_lines.append(f"compression: {compression_strategy}")
        if original_chars is not None:
            frontmatter_lines.append(f"original_chars: {original_chars}")
        if compressed_chars is not None:
            frontmatter_lines.append(f"compressed_chars: {compressed_chars}")
        frontmatter_lines.append("---")

        intent_section = ""
        if context_query:
            intent_section = f"## Agent Intent\n\n> {context_query}\n\n"

        md_content = (
            f"{chr(10).join(frontmatter_lines)}\n\n"
            f"# Proxy Response: {server}/{tool}\n\n"
            f"- **Source**: `{server}/{tool}({args_str})`\n"
            f"- **Original size**: {original_chars or len(text)} chars\n\n"
            f"{intent_section}"
            f"## Content\n\n{text}\n"
        )
        file_path.write_text(md_content, encoding="utf-8")

        ns = ai_cfg.namespace.format(server=server, tool=tool)

        try:
            stats = await self._index_engine.index_file(file_path, namespace=ns)
            chunks = stats.indexed_chunks
        except Exception as exc:
            logger.warning("Auto-index failed for %s/%s: %s", server, tool, exc)
            chunks = 0

        return (
            f"[Indexed] `{server}/{tool}` ({original_chars or len(text)}"
            f"→{compressed_chars or len(agent_summary)} chars) "
            f"· {chunks} chunks in `{ns}` namespace.\n\n"
            f"{agent_summary}"
        )

    def select_chunks(self, key: str, sections: list[str]) -> str:
        if self._selective_compressor is None:
            return "Selective compression not active — no pending TOC selections."
        return self._selective_compressor.select(key, sections)

    async def call_tool(self, server: str, tool: str, arguments: dict[str, Any]) -> str | list:
        """Forward a tool call to upstream, compress, surface, and return."""
        if server not in self._connections:
            raise KeyError(f"Unknown upstream server: '{server}'")
        return await self._call_tool_inner(server, tool, arguments)

    async def _call_tool_inner(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any],
    ) -> str | list:
        # Extract _context_query before forwarding
        context_query = arguments.get("_context_query") if arguments else None
        upstream_args = (
            {k: v for k, v in arguments.items() if k != "_context_query"} if arguments else {}
        )

        # ── Cache lookup ──
        if self._cache is not None:
            cached = self._cache.get(server, tool, upstream_args)
            if cached is not None:
                self.tracker.record_cache_hit()
                # Re-apply surfacing on cache hit so memories stay fresh
                cached = await self._apply_surfacing(server, tool, upstream_args, cached)
                return cached
            self.tracker.record_cache_miss()

        conn = self._connections[server]
        cfg = conn.config
        delay = cfg.reconnect_delay_seconds

        for attempt in range(cfg.max_retries + 1):
            try:
                result = await conn.session.call_tool(tool, upstream_args)
                break
            except Exception as exc:
                err_code = getattr(getattr(exc, "error", None), "code", None)
                # Only retry transport/connection errors and MCP errors.
                # Programming errors (TypeError, AttributeError, etc.)
                # propagate immediately to avoid masking bugs.
                if (
                    not isinstance(exc, (OSError, ConnectionError, asyncio.TimeoutError, EOFError))
                    and err_code is None
                ):
                    raise

                # Protocol errors (bad params, unknown method) — don't retry,
                # reconnect to keep the connection healthy for the next call.
                if err_code in _NO_RETRY_CODES:
                    logger.debug(
                        "Protocol error %s for %s/%s, skipping retry", err_code, server, tool
                    )
                    try:
                        await self._reconnect_server(server)
                    except Exception:
                        logger.warning("Post-protocol-error reconnect failed", exc_info=True)
                    raise

                if attempt >= cfg.max_retries:
                    # Reconnect before raising so the NEXT call starts fresh
                    try:
                        await self._reconnect_server(server)
                    except Exception:
                        logger.warning("Post-failure reconnect failed", exc_info=True)
                    raise
                logger.warning(
                    "Tool call %s/%s failed (attempt %d/%d): %s",
                    server,
                    tool,
                    attempt + 1,
                    cfg.max_retries,
                    exc,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2 if delay > 0 else 0, cfg.max_reconnect_delay_seconds)
                self.tracker.record_reconnect()
                try:
                    await self._reconnect_server(server)
                    conn = self._connections[server]
                except Exception as reconnect_exc:
                    logger.error("Reconnect to '%s' failed: %s", server, reconnect_exc)
                    raise

        # Separate text and non-text content
        text_parts: list[str] = []
        non_text_content: list = []
        for content in result.content:
            if content.type == "text":
                text_parts.append(content.text)
            else:
                non_text_content.append(content)

        # Non-text only → pass through without compression
        if not text_parts:
            if non_text_content:
                return non_text_content
            return "[empty response]"

        original_text = "\n".join(text_parts)

        if result.isError:
            return original_text

        # Resolve effective settings
        tc = self._resolve_tool_config(server, tool)

        # ── Stage 1: CLEAN ──
        cleaned = self._clean_content(original_text, tc.cleaning)

        # ── Stage 2: COMPRESS ──
        compressed = await self._apply_compression(
            cleaned,
            tc.compression,
            tc.max_chars,
            tc.selective,
            tc.llm,
            tc.hybrid,
            server,
            tool,
        )

        # Record metrics BEFORE surfacing (surfacing adds content, not compresses)
        compressed_chars_for_metrics = len(compressed)

        # ── Stage 3: SURFACE (proactive memory injection) ──
        surfaced = await self._apply_surfacing(server, tool, upstream_args, compressed)

        # ── Stage 4: INDEX (optional) ──
        ai_cfg = self._config.auto_index
        if (
            tc.auto_index_enabled
            and self._index_engine is not None
            and len(cleaned) >= ai_cfg.min_chars
        ):
            final_result = await self._auto_index_response(
                server,
                tool,
                upstream_args,
                cleaned,
                agent_summary=surfaced,
                compression_strategy=tc.compression.value,
                original_chars=len(original_text),
                compressed_chars=len(surfaced),
                context_query=context_query,
            )
        else:
            final_result = surfaced

        # Record metrics (using pre-surfacing compressed size)
        self.tracker.record(
            CallMetrics(
                server=server,
                tool=tool,
                original_chars=len(original_text),
                compressed_chars=compressed_chars_for_metrics,
                cleaned_chars=len(cleaned),
            )
        )

        # ── Cache store (pre-surfacing content so memories stay fresh on hit) ──
        if self._cache is not None and not non_text_content:
            self._cache.set(
                server,
                tool,
                upstream_args,
                compressed,
                ttl_seconds=self._config.cache.default_ttl_seconds,
            )

        # Combine compressed text with preserved non-text content
        if non_text_content:
            from mcp.types import TextContent

            return [TextContent(type="text", text=final_result), *non_text_content]

        return final_result
