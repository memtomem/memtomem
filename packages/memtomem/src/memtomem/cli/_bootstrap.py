"""Shared bootstrap for CLI commands that need core components."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from memtomem.server.component_factory import Components


@asynccontextmanager
async def cli_components() -> AsyncIterator[Components]:
    """Async context manager that creates and tears down core components."""
    from memtomem.server.component_factory import close_components, create_components

    comp = await create_components()
    try:
        yield comp
    finally:
        await close_components(comp)
