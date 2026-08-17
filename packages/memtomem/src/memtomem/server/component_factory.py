"""Backward-compatible alias for :mod:`memtomem.runtime.components`.

The factory moved to ``memtomem.runtime`` so in-process embedders can build
components without importing the MCP server. Server, web, and CLI callers
still reach it through this module, which keeps existing
``patch("memtomem.server.component_factory.create_components")`` targets
valid. Patching a factory *internal* (``create_embedder``,
``create_storage``) must target ``memtomem.runtime.components`` — those
names resolve in the defining module, not here.
"""

from __future__ import annotations

from memtomem.runtime.components import (
    Components as Components,
    TeardownResult as TeardownResult,
    _close_resource as _close_resource,
    close_components as close_components,
    create_components as create_components,
)

__all__ = [
    "Components",
    "TeardownResult",
    "close_components",
    "create_components",
]
