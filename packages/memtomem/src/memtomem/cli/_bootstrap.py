"""Shared bootstrap for CLI commands that need core components."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from memtomem.server.component_factory import Components

_CONFIG_PATH = Path.home() / ".memtomem" / "config.json"


@asynccontextmanager
async def cli_components() -> AsyncIterator[Components]:
    """Async context manager that creates and tears down core components."""
    import click

    if not _CONFIG_PATH.exists():
        raise click.ClickException("memtomem is not configured. Run 'mm init' to set up.")

    from memtomem.server.component_factory import close_components, create_components

    comp = await create_components()
    try:
        yield comp
    finally:
        await close_components(comp)
