"""In-process runtime layer — components without a transport.

Everything needed to stand up memtomem's engine (storage, embedder, index
engine, search pipeline) inside a host process. Importing this package must
never pull in the MCP server, its SDK, or the web stack: embedders such as
the LangGraph adapter in :mod:`memtomem.integrations` depend on that
separation, and ``tests/test_runtime_import_hygiene.py`` enforces it against
all three roots (``mcp``, ``memtomem.server``, ``memtomem.web``).

``memtomem.server.component_factory`` remains as a backward-compatible
alias for the names re-exported here.
"""

from __future__ import annotations

from memtomem.runtime.components import (
    Components as Components,
    TeardownResult as TeardownResult,
    close_components as close_components,
    create_components as create_components,
)

__all__ = [
    "Components",
    "TeardownResult",
    "close_components",
    "create_components",
]
