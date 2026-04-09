"""Tiny stdio MCP server used by integration tests.

Stands in for `memtomem-server` so that STM's McpClientSearchAdapter can be
exercised end-to-end without depending on a real memtomem installation. The
server exposes a single tool, `mem_search`, that returns canned content in the
format the adapter knows how to parse.

Run with: `python <path-to-this-file>`
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fake-memtomem")


@mcp.tool()
async def mem_search(
    query: str,
    top_k: int | None = None,
    namespace: str | list[str] | None = None,
) -> str:
    """Return canned search hits in the format McpClientSearchAdapter parses."""
    return (
        "--- [0.92] /notes/auth.md ---\n"
        "JWT authentication uses HS256 with rotating secrets every 24 hours.\n"
        "--- [0.87] /notes/api.md ---\n"
        "All API responses include rate limit headers (X-RateLimit-*).\n"
    )


if __name__ == "__main__":
    mcp.run()
