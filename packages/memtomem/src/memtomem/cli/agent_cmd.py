"""CLI: mm agent — multi-agent namespace management."""

from __future__ import annotations

import asyncio
import json as _json
from typing import cast

import click

from memtomem.cli._prompts import confirm
from memtomem.constants import (
    AGENT_NAMESPACE_PREFIX,
    SHARED_NAMESPACE,
    InvalidNameError,
    validate_agent_id,
    validate_namespace,
)
from memtomem.errors import NamespaceConflictError, NamespaceMutationBusyError
from memtomem.services import namespace_management

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
@click.option(
    "--yes",
    "assume_yes",
    is_flag=True,
    help="Skip the confirmation asked when a target namespace already exists.",
)
def migrate(dry_run: bool, assume_yes: bool) -> None:
    """Rename legacy ``agent/{id}`` namespaces to ``agent-runtime:{id}``.

    Moves multi-agent namespaces from the pre-#318 format (``agent/{id}``)
    to the current ``agent-runtime:{id}`` format. Safe to re-run — rows that
    are already in the new format are left untouched.
    """
    asyncio.run(_run_migrate(dry_run=dry_run, assume_yes=assume_yes))


async def _run_migrate(dry_run: bool, assume_yes: bool = False) -> None:
    from memtomem.cli._bootstrap import cli_components

    async with cli_components() as comp:
        # One listing serves both questions: which legacy namespaces exist,
        # and which of their targets are already taken.
        rows = await comp.storage.list_namespace_meta()
        mapping = _collect_legacy_mapping(rows)
        if not mapping:
            click.echo("No legacy `agent/` namespaces found. Nothing to migrate.")
            return

        # Targets that already exist are consolidated into (rename passes
        # merge=True), so say so up front rather than letting the listing
        # imply an empty destination.
        existing = {row["namespace"] for row in rows}

        click.echo(f"Legacy namespaces to migrate: {len(mapping)}")
        for old, new in mapping:
            suffix = "  (merges into existing namespace)" if new in existing else ""
            click.echo(f"  {old}  ->  {new}{suffix}")

        if dry_run:
            click.echo("\n(dry-run — no changes made. Re-run without --dry-run to apply.)")
            return

        # Consolidation is destructive — the source's metadata row goes, and
        # chunks the destination already holds are deleted. Every other
        # surface makes the caller opt in by name (StrictBool on the tool, an
        # explicit `merge` on the API); the CLI must not opt in on their
        # behalf. Only asked when there is actually something to merge into,
        # so the ordinary "no collisions" migration stays non-interactive.
        merging = [new for _old, new in mapping if new in existing]
        if merging and not assume_yes:
            if not confirm(
                f"{len(merging)} namespace(s) already exist and will be merged into "
                f"(their description/color are kept; duplicate chunks are dropped). "
                f"Continue? (pass --yes to skip this prompt when scripting)"
            ):
                click.echo("Aborted — nothing was changed.")
                return

        total = 0
        dropped = 0
        completed = 0
        skipped: list[str] = []
        for old, new in mapping:
            # merge only for the targets the user was shown and agreed to.
            # A target that appeared *since* the listing was never part of
            # that consent, so it takes the default refusal instead — the
            # pair is skipped and named, and re-running picks it up with a
            # fresh prompt.
            try:
                result = await namespace_management.rename_namespace(
                    comp.storage,
                    old,
                    new,
                    merge=new in existing,
                )
            except NamespaceMutationBusyError as exc:
                raise click.ClickException(
                    f"Migration stopped after {completed} namespace(s). "
                    f"The current pair {old} -> {new} was not changed. {exc} "
                    "Earlier reported renames remain applied; re-run to continue."
                ) from exc
            except NamespaceConflictError:
                skipped.append(old)
                click.echo(f"Skipped: {old}  ->  {new}  (target appeared after the listing)")
                continue
            total += result.chunks_moved
            dropped += result.duplicates_dropped
            completed += 1
            suffix = "  (merged)" if result.merged else ""
            if result.duplicates_dropped:
                # Chunks the destination already held are deleted, not moved —
                # never let that happen without saying so.
                suffix += f"  ({result.duplicates_dropped} duplicate(s) dropped, already in {new})"
            click.echo(f"Renamed: {old}  ->  {new}  ({result.chunks_moved} chunk(s)){suffix}")

        tail = f", {dropped} duplicate(s) dropped" if dropped else ""
        click.echo(
            f"\nMigration complete. {len(mapping) - len(skipped)} namespace(s), "
            f"{total} chunk(s) updated{tail}."
        )
        if skipped:
            click.echo(f"{len(skipped)} skipped — re-run to migrate them.")


def _collect_legacy_mapping(rows: list[dict]) -> list[tuple[str, str]]:
    """Return ``[(old, new), ...]`` pairs for namespaces needing migration.

    *rows* come from ``list_namespace_meta`` (chunks ∪ namespace_metadata),
    not ``list_namespaces`` (chunks only): an agent registered under the
    legacy ``agent/{id}`` name but never written to exists purely as a
    metadata row, and a chunks-only listing would leave it stranded on the
    old prefix forever.
    """
    out: list[tuple[str, str]] = []
    for ns in sorted(row["namespace"] for row in rows):
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
@click.option(
    "--force-unsafe",
    is_flag=True,
    default=False,
    help="Bypass the redaction guard when copying the chunk (audit-logged).",
)
def share(chunk_id: str, target: str, force_unsafe: bool) -> None:
    """Copy a chunk's content into another namespace.

    Mirrors the ``mem_agent_share`` MCP tool. The new chunk gets a fresh
    UUID; this command does *not* create a true cross-reference link.
    See the multi-agent guide for the exact semantics and the ongoing
    RFC for true link support.

    ``target`` is run through :func:`validate_namespace` before any
    storage write — same gate the MCP path closes on issue #496 / PR
    #499. Hostile shapes like ``agent-runtime:foo:bar`` are rejected
    loudly so the CLI cannot smuggle a row past the contract that the
    direct ``agent_id`` and ``mem_agent_share`` MCP surfaces enforce.

    The chunk's content is re-scanned by the trust-boundary redaction
    guard before the copy is written. A chunk that originally landed
    via ``force_unsafe`` will block again on share unless ``--force-unsafe``
    is repeated here, so secret content does not silently propagate
    into a wider namespace.
    """
    try:
        validate_namespace(target)
    except InvalidNameError as e:
        raise click.ClickException(str(e)) from e
    asyncio.run(_run_share(chunk_id, target, force_unsafe))


async def _run_share(chunk_id: str, target: str, force_unsafe: bool = False) -> None:
    from uuid import UUID

    from memtomem.cli._bootstrap import cli_components

    try:
        uid = UUID(chunk_id)
    except (ValueError, TypeError) as exc:
        raise click.ClickException(f"invalid chunk ID format: {chunk_id}") from exc

    async with cli_components() as comp:
        # CLI twin of ``mem_agent_share``, screened the same way: this copies
        # a chunk's content into a shared namespace, so a foreign-project id
        # would republish it past the boundary (ADR-0036). Out-of-boundary
        # reports the same "not found" as a missing id.
        from memtomem.runtime.project_context import _resolve_project_context_root
        from memtomem.search.visibility import resolve_visible_chunk

        chunk = await resolve_visible_chunk(
            comp.storage,
            uid,
            project_context_root=_resolve_project_context_root(comp),
        )
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

        from memtomem import privacy
        from memtomem.tools.memory_writer import append_entry

        guard = privacy.enforce_write_guard(
            chunk.content,
            surface="cli_agent_share",
            force_unsafe=force_unsafe,
            audit_context={"chunk_id": chunk_id, "target": target},
        )
        if guard.decision == "blocked":
            raise click.ClickException(
                f"Chunk content matches {len(guard.hits)} privacy pattern(s); share rejected. "
                "Retry with --force-unsafe to bypass (audit-logged)."
            )

        title = (
            "Shared: " + " > ".join(chunk.metadata.heading_hierarchy)
            if chunk.metadata.heading_hierarchy
            else "Shared: memory"
        )

        from datetime import datetime, timezone

        from memtomem.cli._errors import raise_cli_error
        from memtomem.errors import ConfigError
        from memtomem.memory_scope import day_file_name, namespace_mix_refusal, require_user_base

        try:
            base = require_user_base(comp.config.indexing.memory_dirs)
        except ConfigError as exc:
            raise_cli_error(exc)
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        # Issue #2005: this write indexes with ``namespace=target``, so it has
        # the same restamping hazard as ``mm add`` — the shared entry landing
        # in the plain day file would move that day's other entries into
        # ``target`` as soon as chunk merging joined them.
        default_ns = comp.config.namespace.default_namespace
        path = base / day_file_name(target, default_ns, date_str=date_str)

        from memtomem.context._atomic import (
            _CRUD_SIDECAR_LOCK_BUDGET_S,
            memory_lock_path,
            async_file_lock,
        )

        # The guard is only worth anything if the file it inspected is the
        # file that gets appended to, so it shares one lock span with the
        # append and the re-index — the same L2 sidecar ``mm add`` holds
        # (#1587). Checking outside the lock would let another writer change
        # the file's namespace in between and turn the guard into decoration.
        # ``lock_held=True`` skips the nested engine acquire.
        try:
            async with async_file_lock(memory_lock_path(path), timeout=_CRUD_SIDECAR_LOCK_BUDGET_S):
                mix_err = await namespace_mix_refusal(
                    index_engine=comp.index_engine,
                    storage=comp.storage,
                    default_namespace=default_ns,
                    target=path,
                    effective_ns=target,
                    override_hint="--allow-namespace-mix on `mm add` for a hand-picked file",
                )
                if mix_err is not None:
                    raise click.ClickException(mix_err)
                append_entry(path, chunk.content, title=title, tags=tags)
                # Guarded above (``enforce_write_guard``); skip the engine gate
                # (ADR-0006 PR-A).
                stats = await comp.index_engine.index_file(
                    path, namespace=target, already_scanned=True, lock_held=True
                )
        except TimeoutError as exc:
            raise click.ClickException(
                f"{path} is locked by another process (another server or migrate in flight); retry."
            ) from exc

    click.echo(f"Shared to namespace '{target}'.")
    click.echo(f"- File: {path}")
    click.echo(f"- Indexed chunks: {stats.indexed_chunks}")


# ── search ──────────────────────────────────────────────────────────────


@agent.command("search")
@click.argument("query")
@click.option(
    "--agent-id",
    "-a",
    default=None,
    help="Agent whose scope to search. Defaults to the active session's agent.",
)
@click.option(
    "--include-shared/--no-include-shared",
    default=True,
    show_default=True,
    help="Also search the shared namespace.",
)
@click.option("--top-k", "-k", default=10, show_default=True, help="Number of results.")
@click.option(
    "--shared-namespace",
    default=None,
    help=(
        "Shared bucket to merge in, for per-project agent teams "
        "(e.g. shared:myproject). Defaults to the global 'shared'. Only the "
        "shared leg is re-pointed, so this has no effect with "
        "--no-include-shared, or when no agent resolves and the search runs "
        "unpinned."
    ),
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["table", "json", "plain", "context", "smart"]),
    default="table",
    show_default=True,
    help="Output format.",
)
def agent_search(
    query: str,
    agent_id: str | None,
    include_shared: bool,
    top_k: int,
    shared_namespace: str | None,
    fmt: str,
) -> None:
    """Search an agent's memories, merged with the shared namespace.

    The shell twin of the ``mem_agent_search`` MCP tool, resolving the same
    two buckets by the same rule: the agent's own ``agent-runtime:<id>``
    scope plus, unless ``--no-include-shared``, the shared one. Without
    ``--agent-id`` the agent comes from the active session's binding, the
    way the MCP tool takes it from the session that started it; with no
    session and no flag there is no agent to scope to, and the search runs
    unpinned at default visibility — which is not "every namespace", since
    the usual system-namespace hiding still applies. That case prints a note
    saying so, because a session whose binding could not be read resolves the
    same way, and a silently widened search is the one outcome the caller
    would not think to check for.

    Output formats are ``mm search``'s, not the MCP tool's — this is a CLI
    command, and a shell pipeline expects ``--format json`` to mean what it
    means everywhere else in this CLI.
    """
    try:
        if agent_id is not None:
            validate_agent_id(agent_id)
        if shared_namespace is not None:
            validate_namespace(shared_namespace)
    except InvalidNameError as e:
        raise click.ClickException(str(e)) from e
    from memtomem.cli._errors import raise_cli_error

    try:
        asyncio.run(
            _run_agent_search(
                query=query,
                agent_id=agent_id,
                include_shared=include_shared,
                top_k=top_k,
                shared_namespace=shared_namespace,
                fmt=fmt,
            )
        )
    except click.ClickException:
        raise
    except Exception as e:
        raise_cli_error(e)


def _agent_search_typed_filters(
    *,
    agent_id: str | None,
    include_shared: bool,
    shared_namespace: str | None,
) -> list[tuple[str, str | None]]:
    """The options ``mm agent search`` was actually given, for the diagnostic.

    Only what the command line carried, in this verb's own spelling. The
    agent is omitted when it came from the session rather than from
    ``--agent-id``, because the empty-result message reports the invocation
    and a session binding is not part of it — the "no agent resolved" note
    covers that axis separately. ``--no-include-shared`` takes no argument,
    so it is reported bare.
    """

    typed: list[tuple[str, str | None]] = []
    if agent_id is not None:
        typed.append(("--agent-id", agent_id))
    if not include_shared:
        typed.append(("--no-include-shared", None))
    if shared_namespace is not None:
        typed.append(("--shared-namespace", shared_namespace))
    return typed


def _agent_search_scope_note(agent_ns: str | None, ns_filter: str | None) -> str | None:
    """What narrowed the search, for a reader who did not narrow it.

    Dropping the wrong ``--namespace`` label must not drop the fact it
    carried. Without ``--agent-id`` this verb scopes to the active session's
    agent, so the invocation is bare and the empty-result inventory would
    otherwise report a healthy index and no options at all — true of the
    command line, and silent about the one thing that emptied the result.

    ``None`` when there is nothing to disclose: an unresolved agent did not
    narrow anything, and the "no agent resolved" note already owns that case.
    """

    if ns_filter is None or agent_ns is None:
        return None
    return (
        f"This search was scoped to namespace '{ns_filter}', resolved from the "
        "active session; pass --agent-id to scope it to a different agent."
    )


async def _run_agent_search(
    *,
    query: str,
    agent_id: str | None,
    include_shared: bool,
    top_k: int,
    shared_namespace: str | None,
    fmt: str,
) -> None:
    from memtomem.cli._bootstrap import cli_components
    from memtomem.cli._session_state import resolve_session_write_namespace
    from memtomem.cli.search import _search_with_components, render_search_results
    from memtomem.server.tools.multi_agent import merge_agent_namespace_filter

    async with cli_components() as comp:
        # Priority mirrors ``_resolve_agent_namespace``: an explicit flag
        # overrides the session, and the MCP chain's third step (the ambient
        # ``current_namespace``) has no CLI equivalent, so it is simply absent.
        if agent_id:
            agent_ns: str | None = f"{_CURRENT_PREFIX}{agent_id}"
        else:
            agent_ns = await resolve_session_write_namespace(comp.storage)
        ns_filter = merge_agent_namespace_filter(agent_ns, include_shared, shared_namespace)

        payload = await _search_with_components(
            comp,
            query=query,
            top_k=top_k,
            source_filter=None,
            tag_filter=None,
            namespace=ns_filter,
            scope=None,
            as_of=None,
            fmt=fmt,
            # This verb has no ``--namespace`` and no ``-n``: the namespace it
            # searches is merged here out of the three options below. Reporting
            # it as ``--namespace`` — the shared helper's default, right for
            # ``mm search`` — would answer an empty result by naming a flag
            # ``mm agent search`` rejects, and a remediation the reader cannot
            # carry out is worse than none. So the diagnostic gets this
            # command's own vocabulary, and ``count_flag=None`` drops the
            # ``-n`` / ``-k`` mix-up hint along with it.
            namespace_label="the resolved agent namespace",
            count_flag=None,
            # An explicit --agent-id is already in the filters below; only a
            # session-derived scope is invisible on the command line.
            scope_note=(
                _agent_search_scope_note(agent_ns, ns_filter) if agent_id is None else None
            ),
            typed_filters=_agent_search_typed_filters(
                agent_id=agent_id,
                include_shared=include_shared,
                shared_namespace=shared_namespace,
            ),
        )

    # An unresolved agent widens the query from one agent's scope to the whole
    # store, and the two ways to get here are indistinguishable from the
    # caller's side: no session at all, or a session whose binding could not be
    # read (an unreadable state file, a stale row, a malformed agent id — the
    # resolver treats all of them as "unbound" by design). Both are legitimate,
    # neither is worth failing on, and both produce results from namespaces the
    # caller asked to be scoped away from. Say it on stderr so the widening is
    # visible without changing what the resolver returns or what the MCP tool
    # would have done with the same inputs.
    if ns_filter is None:
        click.secho(
            "(no agent resolved — searching unpinned, not just this agent's scope. "
            "Pass --agent-id, or start a session bound to an agent.)",
            fg="yellow",
            err=True,
        )

    # ``--shared-namespace`` re-points the shared leg of the merge, so it is
    # validated and then has nothing to act on in the two cases where there is
    # no such leg. Both are accepted rather than refused — neither is a
    # mistake worth failing a search over — but a flag that parses, validates
    # and then does nothing is exactly the kind of silence that gets read as
    # "it worked". Say which case swallowed it.
    if shared_namespace is not None and (ns_filter is None or not include_shared):
        reason = (
            "there is no agent scope to merge it with"
            if ns_filter is None
            else "--no-include-shared drops the shared leg it re-points"
        )
        click.secho(
            f"(--shared-namespace {shared_namespace!r} was ignored: {reason}.)",
            fg="yellow",
            err=True,
        )

    render_search_results(query, fmt, payload)


# ── debug-resolve (hidden) ──────────────────────────────────────────────


@agent.command("debug-resolve", hidden=True)
@click.option("--agent-id", "-a", default=None, help="Explicit agent_id (mem_agent_search arg).")
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

    from memtomem.server.context import AppContext
    from memtomem.server.tools.multi_agent import (
        _resolve_agent_namespace,
        merge_agent_namespace_filter,
    )

    fake_app = SimpleNamespace(
        current_agent_id=current_agent_id,
        current_namespace=current_namespace,
    )
    agent_ns = _resolve_agent_namespace(cast(AppContext, fake_app), agent_id)

    ns_filter = merge_agent_namespace_filter(agent_ns, include_shared)

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
