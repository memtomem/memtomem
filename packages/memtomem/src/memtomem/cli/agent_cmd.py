"""CLI: mm agent — multi-agent namespace management."""

from __future__ import annotations

import asyncio
import json as _json
from typing import TYPE_CHECKING

import click

from memtomem.constants import (
    AGENT_NAMESPACE_PREFIX,
    SHARED_NAMESPACE,
    InvalidNameError,
    validate_agent_id,
)

if TYPE_CHECKING:
    from memtomem.storage.sqlite_backend import SqliteBackend

_LEGACY_PREFIX = "agent/"
# Local alias paired with ``_LEGACY_PREFIX`` so the migration mapping reads
# as a (old, new) pair. The value derives from ``AGENT_NAMESPACE_PREFIX``;
# don't redefine the literal here.
_CURRENT_PREFIX = AGENT_NAMESPACE_PREFIX


@click.group()
def agent() -> None:
    """Multi-agent memory management commands."""


@agent.command("migrate")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print the planned renames without making changes.",
)
def migrate(dry_run: bool) -> None:
    """Rename legacy ``agent/{id}`` namespaces to ``agent-runtime:{id}``.

    Moves multi-agent namespaces from the pre-#318 format (``agent/{id}``)
    to the current ``agent-runtime:{id}`` format. Safe to re-run — rows that
    are already in the new format are left untouched.
    """
    asyncio.run(_run_migrate(dry_run=dry_run))


async def _run_migrate(dry_run: bool) -> None:
    from memtomem.cli._bootstrap import cli_components

    async with cli_components() as comp:
        mapping = await _collect_legacy_mapping(comp.storage)
        if not mapping:
            click.echo("No legacy `agent/` namespaces found. Nothing to migrate.")
            return

        click.echo(f"Legacy namespaces to migrate: {len(mapping)}")
        for old, new in mapping:
            click.echo(f"  {old}  ->  {new}")

        if dry_run:
            click.echo("\n(dry-run — no changes made. Re-run without --dry-run to apply.)")
            return

        total = 0
        for old, new in mapping:
            renamed = await comp.storage.rename_namespace(old, new)
            total += renamed
            click.echo(f"Renamed: {old}  ->  {new}  ({renamed} chunk(s))")

        click.echo(f"\nMigration complete. {len(mapping)} namespace(s), {total} chunk(s) updated.")


async def _collect_legacy_mapping(storage: SqliteBackend) -> list[tuple[str, str]]:
    """Return ``[(old, new), ...]`` pairs for namespaces needing migration."""
    pairs = await storage.list_namespaces()
    out: list[tuple[str, str]] = []
    for ns, _count in pairs:
        if not ns.startswith(_LEGACY_PREFIX):
            continue
        suffix = ns[len(_LEGACY_PREFIX) :]
        out.append((ns, f"{_CURRENT_PREFIX}{suffix}"))
    return out


# ── register ────────────────────────────────────────────────────────────


@agent.command("register")
@click.argument("agent_id")
@click.option("--description", default=None, help="Human-readable description of the agent's role.")
@click.option(
    "--color",
    default=None,
    help="Optional hex color code for UI display (e.g. ``#ff8800``).",
)
def register(agent_id: str, description: str | None, color: str | None) -> None:
    """Register an agent and create its ``agent-runtime:<id>`` namespace.

    Mirrors the ``mem_agent_register`` MCP tool so operators don't have to
    spin up an MCP client for one-off agent setup. Also ensures the
    cross-agent ``shared`` namespace exists.

    ``agent_id`` is validated against the canonical ``[A-Za-z0-9._-]``
    charset (same gate as ``mm session start``); hostile shapes like
    ``foo:bar`` or ``../x`` are rejected loudly rather than silently
    sanitised.
    """
    try:
        validate_agent_id(agent_id)
    except InvalidNameError as e:
        raise click.ClickException(str(e)) from e
    asyncio.run(_run_register(agent_id, description, color))


async def _run_register(agent_id: str, description: str | None, color: str | None) -> None:
    from memtomem.cli._bootstrap import cli_components

    namespace = f"{_CURRENT_PREFIX}{agent_id}"
    async with cli_components() as comp:
        await comp.storage.set_namespace_meta(namespace, description=description, color=color)
        if await comp.storage.get_namespace_meta(SHARED_NAMESPACE) is None:
            await comp.storage.set_namespace_meta(
                SHARED_NAMESPACE, description="Shared knowledge base for all agents"
            )
    click.echo(f"Agent registered: {agent_id}")
    click.echo(f"- Namespace: {namespace}")
    click.echo(f"- Shared namespace: {SHARED_NAMESPACE}")


# ── list ────────────────────────────────────────────────────────────────


@agent.command("list")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit machine-readable JSON instead of the default table.",
)
def list_agents(as_json: bool) -> None:
    """List registered agents (``agent-runtime:`` namespaces) and ``shared``.

    Default output is a table grouped by ``agents`` and ``shared`` —
    machine-readable form via ``--json`` for use in scripts.
    """
    asyncio.run(_run_list(as_json=as_json))


async def _run_list(as_json: bool) -> None:
    from memtomem.cli._bootstrap import cli_components

    async with cli_components() as comp:
        ns_counts = dict(await comp.storage.list_namespaces())
        all_meta = await comp.storage.list_namespace_meta()

        agents: list[dict] = []
        for meta in all_meta:
            ns = meta.get("namespace", "")
            if not ns.startswith(_CURRENT_PREFIX):
                continue
            agents.append(
                {
                    "agent_id": ns[len(_CURRENT_PREFIX) :],
                    "namespace": ns,
                    "description": meta.get("description"),
                    "color": meta.get("color"),
                    "chunks": ns_counts.get(ns, 0),
                }
            )

        shared_meta = await comp.storage.get_namespace_meta(SHARED_NAMESPACE)
        shared = (
            {
                "namespace": SHARED_NAMESPACE,
                "description": (shared_meta or {}).get("description"),
                "chunks": ns_counts.get(SHARED_NAMESPACE, 0),
            }
            if shared_meta is not None or SHARED_NAMESPACE in ns_counts
            else None
        )

    if as_json:
        click.echo(_json.dumps({"agents": agents, "shared": shared}, indent=2))
        return

    if not agents and shared is None:
        click.echo("No agents registered. Use `mm agent register <id>` to create one.")
        return

    click.echo(f"Agents: {len(agents)}")
    for a in agents:
        desc = f" — {a['description']}" if a.get("description") else ""
        click.echo(f"  {a['agent_id']:<20} ({a['chunks']} chunk(s)) {a['namespace']}{desc}")

    if shared is not None:
        click.echo("")
        desc = f" — {shared['description']}" if shared.get("description") else ""
        click.echo(f"Shared: {shared['namespace']} ({shared['chunks']} chunk(s)){desc}")


# ── share ───────────────────────────────────────────────────────────────


@agent.command("share")
@click.argument("chunk_id")
@click.option(
    "--target",
    default=SHARED_NAMESPACE,
    show_default=True,
    help=("Target namespace — ``shared`` or ``agent-runtime:<agent_id>``."),
)
def share(chunk_id: str, target: str) -> None:
    """Copy a chunk's content into another namespace.

    Mirrors the ``mem_agent_share`` MCP tool. The new chunk gets a fresh
    UUID; this command does *not* create a true cross-reference link.
    See the multi-agent guide for the exact semantics and the ongoing
    RFC for true link support.
    """
    asyncio.run(_run_share(chunk_id, target))


async def _run_share(chunk_id: str, target: str) -> None:
    from uuid import UUID

    from memtomem.cli._bootstrap import cli_components

    try:
        uid = UUID(chunk_id)
    except (ValueError, TypeError) as exc:
        raise click.ClickException(f"invalid chunk ID format: {chunk_id}") from exc

    async with cli_components() as comp:
        chunk = await comp.storage.get_chunk(uid)
        if chunk is None:
            raise click.ClickException(f"Chunk {chunk_id} not found.")

        # Build the copy's tags using the same dedup contract as the MCP
        # tool — keep them in lock-step via the helper so future tweaks
        # stay in one place.
        try:
            from memtomem.server.tools.multi_agent import _build_shared_tags

            tags = _build_shared_tags(chunk.metadata.tags, chunk_id)
        except ImportError:
            # Fallback for branches that pre-date PR-3 — append the bare
            # audit tag without dedup. The CLI must keep working even
            # before the helper lands.
            tags = list(chunk.metadata.tags) + [f"shared-from={chunk_id}"]

        from memtomem.tools.memory_writer import append_entry

        title = (
            "Shared: " + " > ".join(chunk.metadata.heading_hierarchy)
            if chunk.metadata.heading_hierarchy
            else "Shared: memory"
        )

        if not comp.config.indexing.memory_dirs:
            raise click.ClickException("No memory directories configured. Run `mm init` first.")
        from datetime import datetime, timezone
        from pathlib import Path

        base = Path(comp.config.indexing.memory_dirs[0]).expanduser().resolve()
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = base / f"{date_str}.md"
        append_entry(path, chunk.content, title=title, tags=tags)
        stats = await comp.index_engine.index_file(path, namespace=target)

    click.echo(f"Shared to namespace '{target}'.")
    click.echo(f"- File: {path}")
    click.echo(f"- Indexed chunks: {stats.indexed_chunks}")


# ── debug-resolve (hidden) ──────────────────────────────────────────────


@agent.command("debug-resolve", hidden=True)
@click.option("--agent-id", default=None, help="Explicit agent_id (mem_agent_search arg).")
@click.option(
    "--current-agent-id",
    default=None,
    help="Simulated AppContext.current_agent_id (set by mem_session_start).",
)
@click.option(
    "--current-namespace",
    default=None,
    help="Simulated AppContext.current_namespace (legacy fallback).",
)
@click.option(
    "--include-shared/--no-include-shared",
    default=True,
    show_default=True,
    help="Whether mem_agent_search would also search the shared namespace.",
)
def debug_resolve(
    agent_id: str | None,
    current_agent_id: str | None,
    current_namespace: str | None,
    include_shared: bool,
) -> None:
    """Dump the namespace ``mem_agent_search`` would resolve, as JSON.

    Hidden e2e helper — does not require a running MCP server. Lets the
    multi-agent integration scripts assert namespace resolution without
    spinning up an MCP client.
    """
    from types import SimpleNamespace

    from memtomem.server.tools.multi_agent import _resolve_agent_namespace

    fake_app = SimpleNamespace(
        current_agent_id=current_agent_id,
        current_namespace=current_namespace,
    )
    agent_ns = _resolve_agent_namespace(fake_app, agent_id)

    if include_shared and agent_ns:
        ns_filter: str | None = f"{agent_ns},{SHARED_NAMESPACE}"
    elif agent_ns:
        ns_filter = agent_ns
    else:
        ns_filter = None

    click.echo(
        _json.dumps(
            {
                "inputs": {
                    "agent_id": agent_id,
                    "current_agent_id": current_agent_id,
                    "current_namespace": current_namespace,
                    "include_shared": include_shared,
                },
                "agent_namespace": agent_ns,
                "resolved_namespace_filter": ns_filter,
            },
            indent=2,
        )
    )
