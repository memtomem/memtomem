"""MCP Client adapter for surfacing — connects to a remote memtomem server."""

from __future__ import annotations

import logging
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from memtomem_stm.surfacing.config import SurfacingConfig

logger = logging.getLogger(__name__)


@dataclass
class RemoteSearchResult:
    """Lightweight search result parsed from mem_search text output."""

    class _FakeMeta:
        def __init__(self, source: str, namespace: str):
            self.source_file = Path(source)
            self.namespace = namespace

    class _FakeChunk:
        def __init__(self, content: str, source: str, namespace: str):
            self.content = content
            self.metadata = RemoteSearchResult._FakeMeta(source, namespace)
            self.id = ""

    def __init__(self, content: str, score: float, source: str = "", namespace: str = "default"):
        self.chunk = self._FakeChunk(content, source, namespace)
        self.score = score


class McpClientSearchAdapter:
    """Connects to a memtomem MCP server via stdio and calls mem_search.

    Implements enough of the SearchPipeline interface for SurfacingEngine.
    """

    def __init__(self, config: SurfacingConfig) -> None:
        self._config = config
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    async def start(self) -> None:
        """Connect to the memtomem MCP server."""
        self._stack = AsyncExitStack()
        params = StdioServerParameters(
            command=self._config.ltm_mcp_command,
            args=self._config.ltm_mcp_args,
        )
        transport = stdio_client(params)
        streams = await self._stack.enter_async_context(transport)
        self._session = await self._stack.enter_async_context(ClientSession(streams[0], streams[1]))
        await self._session.initialize()
        logger.info("MCP client connected to memtomem server: %s", self._config.ltm_mcp_command)

    async def stop(self) -> None:
        """Disconnect from the memtomem MCP server."""
        if self._stack:
            await self._stack.aclose()
            self._stack = None
            self._session = None

    async def search(
        self,
        query: str,
        top_k: int | None = None,
        namespace: str | list[str] | None = None,
        **kwargs: Any,
    ) -> tuple[list[RemoteSearchResult], object]:
        """Call mem_search on the remote server and parse results."""
        if self._session is None:
            return [], None

        args: dict[str, Any] = {"query": query}
        if top_k is not None:
            args["top_k"] = top_k
        if namespace is not None:
            args["namespace"] = namespace

        try:
            result = await self._session.call_tool("mem_search", args)
        except Exception as exc:
            logger.debug("MCP mem_search failed: %s", exc)
            return [], None

        # Parse text response into results
        text_parts = [c.text for c in result.content if c.type == "text"]
        if not text_parts:
            return [], None

        text = "\n".join(text_parts)
        return self._parse_results(text), None

    @staticmethod
    def _parse_results(text: str) -> list[RemoteSearchResult]:
        """Parse mem_search formatted output into RemoteSearchResult objects.

        Expected format per result:
        --- [score] source_file ---
        content...
        """
        results: list[RemoteSearchResult] = []
        # Split by result separators
        blocks = re.split(r"^---\s*", text, flags=re.MULTILINE)

        for block in blocks:
            block = block.strip()
            if not block:
                continue

            # Try to extract score from first line
            first_line, _, rest = block.partition("\n")
            score_match = re.search(r"\[(\d+\.?\d*)\]", first_line)
            score = float(score_match.group(1)) if score_match else 0.5

            # Extract source file
            source_match = re.search(r"(\S+\.md)", first_line)
            source = source_match.group(1) if source_match else "unknown"

            content = rest.strip() if rest else first_line
            if content:
                results.append(
                    RemoteSearchResult(
                        content=content[:500],
                        score=score,
                        source=source,
                    )
                )

        return results
