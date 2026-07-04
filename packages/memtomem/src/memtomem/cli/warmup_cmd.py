"""``mm warmup`` — pre-download and load the local models (#1621)."""

from __future__ import annotations

import asyncio

import click

from memtomem.cli._errors import raise_cli_error


@click.command()
def warmup() -> None:
    """Pre-download and load the local embedding/reranker models.

    The models otherwise download and load lazily on the first query.
    Run this after `mm init` (or in a container build) to front-load
    that cost. Remote providers (ollama, openai, cohere) are skipped —
    they have no local model to preload. Long-running MCP servers can
    opt into the same warmup at startup via MEMTOMEM_WARMUP__ENABLED.
    """
    try:
        asyncio.run(_warmup())
    except click.ClickException:
        raise
    except Exception as e:
        raise_cli_error(e)


async def _warmup() -> None:
    from memtomem.cli._bootstrap import cli_components
    from memtomem.server.warmup import warm_models

    async with cli_components() as comp:
        outcomes = await warm_models(comp)

    for o in outcomes:
        click.echo(f"{o.component}: {o.status} (provider={o.provider}, model={o.model})")
