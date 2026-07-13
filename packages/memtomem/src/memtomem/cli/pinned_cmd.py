"""CLI for file-backed Pinned Context blocks."""

from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import get_args

import click

from memtomem.config import TargetScope

_SCOPES = list(get_args(TargetScope))


@click.group("pinned")
def pinned() -> None:
    """Manage durable context included before retrieved memories."""


@asynccontextmanager
async def _store_context():
    from memtomem.cli._bootstrap import cli_components
    from memtomem.pinned import PinnedContextStore
    from memtomem.server.tools.search import _resolve_project_context_root

    async with AsyncExitStack() as stack:
        comp = await stack.enter_async_context(cli_components())
        store = PinnedContextStore(comp.config, project_root=_resolve_project_context_root(comp))
        yield comp, store


@pinned.command("list")
@click.option("--agent", "agent_id", default=None)
@click.option("--json", "as_json", is_flag=True)
def list_blocks(agent_id: str | None, as_json: bool) -> None:
    """List effective blocks after agent and scope shadowing."""
    asyncio.run(_list_blocks(agent_id, as_json))


async def _list_blocks(agent_id: str | None, as_json: bool) -> None:
    async with _store_context() as (_, store):
        blocks = store.list(agent_id=agent_id)
        if as_json:
            click.echo(json.dumps([block.as_dict() for block in blocks], ensure_ascii=False))
            return
        if not blocks:
            click.echo("No Pinned Context blocks found.")
        for block in blocks:
            target = f" agent={block.agent_id}" if block.agent_id else ""
            click.echo(f"{block.block_id} [{block.scope}{target}] {block.description}")


@pinned.command("get")
@click.argument("block_id")
@click.option("--scope", type=click.Choice(_SCOPES), default="user")
@click.option("--agent", "agent_id", default=None)
def get_block(block_id: str, scope: TargetScope, agent_id: str | None) -> None:
    """Print one block from an exact scope and agent location."""
    asyncio.run(_get_block(block_id, scope, agent_id))


async def _get_block(block_id: str, scope: TargetScope, agent_id: str | None) -> None:
    async with _store_context() as (_, store):
        block = store.get(block_id, scope=scope, agent_id=agent_id)
        if block is None:
            raise click.ClickException("Pinned Context block not found")
        click.echo(block.content)


@pinned.command("set")
@click.argument("block_id")
@click.option("--content", default=None, help="Block text; omit when using --file.")
@click.option("--file", "content_file", type=click.Path(path_type=Path), default=None)
@click.option("--description", default="")
@click.option("--priority", type=int, default=0)
@click.option("--scope", type=click.Choice(_SCOPES), default="user")
@click.option("--agent", "agent_id", default=None)
@click.option("--confirm-project-shared", is_flag=True)
@click.option("--force-unsafe", is_flag=True)
def set_block(
    block_id: str,
    content: str | None,
    content_file: Path | None,
    description: str,
    priority: int,
    scope: TargetScope,
    agent_id: str | None,
    confirm_project_shared: bool,
    force_unsafe: bool,
) -> None:
    """Create or replace one Pinned Context block."""
    if (content is None) == (content_file is None):
        raise click.UsageError("Provide exactly one of --content or --file")
    if content is not None:
        body = content
    elif content_file is not None:
        body = content_file.read_text(encoding="utf-8")
    else:  # guarded by the exactly-one check above
        raise click.UsageError("Provide exactly one of --content or --file")
    asyncio.run(
        _set_block(
            block_id,
            body,
            description,
            priority,
            scope,
            agent_id,
            confirm_project_shared,
            force_unsafe,
        )
    )


async def _set_block(
    block_id: str,
    content: str,
    description: str,
    priority: int,
    scope: TargetScope,
    agent_id: str | None,
    confirm_project_shared: bool,
    force_unsafe: bool,
) -> None:
    async with _store_context() as (_, store):
        try:
            block = store.set(
                block_id,
                content,
                scope=scope,
                agent_id=agent_id,
                description=description,
                priority=priority,
                confirm_project_shared=confirm_project_shared,
                force_unsafe=force_unsafe,
            )
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(block.source_path)


@pinned.command("delete")
@click.argument("block_id")
@click.option("--scope", type=click.Choice(_SCOPES), default="user")
@click.option("--agent", "agent_id", default=None)
@click.option("--confirm-project-shared", is_flag=True)
def delete_block(
    block_id: str,
    scope: TargetScope,
    agent_id: str | None,
    confirm_project_shared: bool,
) -> None:
    """Delete one exact Pinned Context block."""
    asyncio.run(_delete_block(block_id, scope, agent_id, confirm_project_shared))


async def _delete_block(
    block_id: str,
    scope: TargetScope,
    agent_id: str | None,
    confirm_project_shared: bool,
) -> None:
    async with _store_context() as (_, store):
        try:
            deleted = store.delete(
                block_id,
                scope=scope,
                agent_id=agent_id,
                confirm_project_shared=confirm_project_shared,
            )
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo("Deleted." if deleted else "Not found.")


@pinned.command("compose")
@click.argument("query", required=False)
@click.option("--agent", "agent_id", default=None)
@click.option("--max-chars", type=click.IntRange(min=1), default=12_000)
@click.option("--top-k", type=click.IntRange(min=1), default=10)
def compose(query: str | None, agent_id: str | None, max_chars: int, top_k: int) -> None:
    """Emit a structured pinned-first context bundle as JSON."""
    asyncio.run(_compose(query, agent_id, max_chars, top_k))


async def _compose(query: str | None, agent_id: str | None, max_chars: int, top_k: int) -> None:
    from memtomem.pinned import ContextAssembler

    async with _store_context() as (comp, store):
        bundle = await ContextAssembler(store, comp.search_pipeline).compose(
            query, agent_id=agent_id, max_chars=max_chars, top_k=top_k
        )
        click.echo(json.dumps(bundle.as_dict(), ensure_ascii=False))
