"""CLI: mm status — terminal mirror of the MCP ``mem_status`` tool (#382)."""

from __future__ import annotations

import asyncio

import click


@click.command("status")
def status() -> None:
    """Show indexing statistics and current configuration summary.

    Mirrors the MCP ``mem_status`` tool — same output, callable from a
    terminal without an MCP client. Useful as a post-install sanity
    check that the binary works, the config is readable, and the DB is
    reachable, without having to run a search.
    """
    try:
        asyncio.run(_status())
    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(str(e)) from e


async def _status() -> None:
    from memtomem.cli._bootstrap import cli_components
    from memtomem.server.context import AppContext
    from memtomem.server.tools.status_config import format_status_report

    async with cli_components() as comp:
        ctx = AppContext.from_components(comp)
        output = await format_status_report(ctx)

    click.echo(output)
