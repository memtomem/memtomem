"""Compatibility layer for registering proxy tools with correct schema in FastMCP.

FastMCP infers tool parameter schemas from the handler's function signature.
Proxy handlers use **kwargs, which produces an incorrect schema (single "kwargs"
param). This module overrides both the schema AND the validation model so that:
  - Claude sees the upstream tool's actual parameter names
  - FastMCP validation passes any arguments through to the handler
  - Tool annotations (readOnlyHint, destructiveHint) are preserved
"""

from __future__ import annotations

from typing import Any

from pydantic import ConfigDict

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.func_metadata import ArgModelBase, FuncMetadata


class _ProxyPassthroughArgs(ArgModelBase):
    """Pydantic model that accepts and forwards any fields."""

    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)

    def model_dump_one_level(self) -> dict[str, Any]:
        result = super().model_dump_one_level()
        if self.__pydantic_extra__:
            result.update(self.__pydantic_extra__)
        return result


_PASSTHROUGH_METADATA = FuncMetadata(
    arg_model=_ProxyPassthroughArgs,
    output_schema=None,
    output_model=None,
    wrap_output=False,
)


def register_proxy_tool(
    server: FastMCP,
    handler: Any,
    info: Any,  # ProxyToolInfo
) -> None:
    """Register a proxy tool with the upstream's actual schema and annotations."""
    server.add_tool(
        handler,
        name=info.prefixed_name,
        description=f"[proxied] {info.description}",
        annotations=info.annotations,
    )
    registered = server._tool_manager._tools.get(info.prefixed_name)
    if registered is not None:
        if info.input_schema:
            registered.parameters = info.input_schema
        registered.fn_metadata = _PASSTHROUGH_METADATA
