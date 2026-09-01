"""CLI: memtomem add / memtomem recall."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import get_args

import click

from memtomem.cli._errors import raise_cli_error
from memtomem.cli._prompts import confirm as _confirm
from memtomem.config import TargetScope
from memtomem.memory_scope import (
    NS_RETARGET_ATTEMPTS,
    NS_RETARGET_EXHAUSTED_ERROR,
    MemoryScopeError,
    resolve_memory_scope_dir as _resolve_memory_scope_dir_core,
)


_MEMORY_SCOPE_CHOICES = list(get_args(TargetScope))


def _resolve_memory_scope_dir(
    scope: TargetScope,
    project_root: Path | None,
    user_base: Path | None = None,
) -> Path:
    """ADR-0011 scope → directory, surfaced as ``ClickException`` for the CLI.

    ``user_base`` is only meaningful for ``scope="user"``; project tiers
    resolve from ``project_root`` alone (#1768).
    """
    try:
        if user_base is None:
            return _resolve_memory_scope_dir_core(scope, project_root)
        return _resolve_memory_scope_dir_core(scope, project_root, user_base)
    except MemoryScopeError as exc:
        raise click.ClickException(str(exc)) from exc


def _prompt_project_shared_confirm(target: Path, *, err: bool = False) -> bool:
    """Prompt before writing to the git-tracked project_shared tier.

    ``err=True`` routes the prompt chrome to stderr so ``--json`` runs
    keep stdout as a single machine-readable ack (via ``_prompts.confirm``,
    which bypasses click's Windows stdout leak — #1640).
    """
    click.secho(
        "This will write to the git-tracked project memory directory:", fg="yellow", err=err
    )
    click.echo(f"  {target}", err=err)
    return _confirm("Continue?", default=False, err=err)


def _render_validity_window(valid_from_unix: int | None, valid_to_unix: int | None) -> str:
    """Render a chunk's temporal-validity window as a compact ``[from → to]`` label.

    Per temporal-validity RFC §CLI surfacing. Both bounds are unix-seconds;
    ``None`` is rendered as ``∞`` (no bound on that side). Date-only display —
    the original frontmatter shape (``YYYY-MM-DD`` vs ``YYYY-QN``) is not
    preserved through unix-second storage, so a quarter that ended ``2026-Q1``
    surfaces as ``2026-03-31``. The user can inspect the source file for the
    original spelling.
    """

    def _fmt(unix: int | None) -> str:
        if unix is None:
            return "∞"
        return datetime.fromtimestamp(unix, tz=timezone.utc).strftime("%Y-%m-%d")

    return f"[{_fmt(valid_from_unix)} → {_fmt(valid_to_unix)}]"


# See the note on ``SEARCH_EPILOG`` in ``cli/search.py`` for why ``\b`` is
# required here.
ADD_EPILOG = """\
Examples:

\b
  mm add "Canary deploy froze at 14:02Z." --tags incident,postmortem
  mm add "Standardize on uv" --scope project_shared --confirm-project-shared
  mm add "Quick note to self" --json
  mm add "Cross-agent handoff" --namespace shared
"""


@click.command(epilog=ADD_EPILOG)
@click.argument("content")
@click.option("--title", "-t", default=None, help="Entry title")
@click.option("--tags", default=None, help="Comma-separated tags")
@click.option(
    "--file",
    "file_name",
    default=None,
    help="Target file relative to the selected scope's memory directory.",
)
@click.option(
    "--force-unsafe",
    is_flag=True,
    default=False,
    help="Bypass the redaction guard for this call (audit-logged).",
)
@click.option(
    "--scope",
    type=click.Choice(_MEMORY_SCOPE_CHOICES),
    default="user",
    show_default=True,
    help="Memory scope tier: user, project_shared, or project_local.",
)
@click.option(
    "--confirm-project-shared",
    is_flag=True,
    default=False,
    help="Confirm writing to the git-tracked project_shared memory tier.",
)
@click.option(
    "--namespace",
    "-n",
    default=None,
    help="Write namespace; defaults to the active session's agent scope.",
)
@click.option(
    "--allow-namespace-mix",
    is_flag=True,
    default=False,
    help=(
        "Append even though --file already holds a different namespace "
        "(re-indexing may restamp the existing entries)."
    ),
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    default=False,
    help="Skip confirmation prompts.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit a machine-readable JSON ack instead of text output.",
)
def add(
    content: str,
    title: str | None,
    tags: str | None,
    file_name: str | None,
    force_unsafe: bool,
    scope: TargetScope,
    confirm_project_shared: bool,
    namespace: str | None,
    allow_namespace_mix: bool,
    yes: bool,
    as_json: bool,
) -> None:
    """Add a memory entry and index it."""
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    try:
        asyncio.run(
            _add(
                content,
                title,
                tag_list,
                file_name,
                force_unsafe,
                scope,
                confirm_project_shared,
                yes,
                namespace=namespace,
                as_json=as_json,
                allow_namespace_mix=allow_namespace_mix,
            )
        )
    except click.ClickException as e:
        if as_json:
            click.echo(json.dumps({"ok": False, "reason": e.format_message()}))
            raise click.exceptions.Exit(1)
        raise
    except Exception as e:
        raise_cli_error(e)


async def _add(
    content: str,
    title: str | None,
    tags: list[str],
    file_name: str | None,
    force_unsafe: bool = False,
    scope: TargetScope = "user",
    confirm_project_shared: bool = False,
    yes: bool = False,
    *,
    namespace: str | None = None,
    as_json: bool = False,
    allow_namespace_mix: bool = False,
) -> None:
    from memtomem import privacy
    from memtomem.cli._bootstrap import cli_components
    from memtomem.cli._session_state import resolve_session_write_namespace
    from memtomem.constants import InvalidNameError, validate_namespace
    from memtomem.server.tools.search import _resolve_project_context_root
    from memtomem.tools.memory_writer import append_entry

    if namespace is not None:
        # Raised as a ClickException so ``--json`` runs get the ``{"ok": false}``
        # ack from ``add``'s handler instead of a UsageError on stderr.
        try:
            validate_namespace(namespace)
        except InvalidNameError as exc:
            raise click.ClickException(str(exc)) from exc

    guard = privacy.enforce_write_guard(
        content,
        surface="cli_mm_add",
        force_unsafe=force_unsafe,
        scope=scope,
        audit_context={"file": file_name, "scope": scope},
    )
    if guard.decision == "blocked":
        raise click.ClickException(
            f"Content matches {len(guard.hits)} privacy pattern(s); write rejected. "
            "Retry with --force-unsafe to bypass (audit-logged)."
        )
    if guard.decision == "blocked_project_shared":
        # ADR-0011 §5: ``force_unsafe=True`` is hard-refused on
        # ``project_shared`` writes — git history is forever, so
        # the bypass valve does not exist on this tier. The MCP
        # surface enforces the same refusal; the CLI must mirror
        # it or ``mm mem add --scope project_shared --force-unsafe``
        # would still land flagged content in the git-tracked tier.
        raise click.ClickException(
            f"Content matches {len(guard.hits)} privacy pattern(s) and "
            "--force-unsafe is not permitted on --scope project_shared "
            "(git history is forever). Retry with --scope project_local "
            "or --scope user to bypass; manually edit the canonical file "
            "if a project_shared write is required."
        )

    async with cli_components() as comp:
        project_root = _resolve_project_context_root(comp)
        # ADR-0011 PR-D review round 7: ``mm mem add user_base`` must
        # read from ``indexing.memory_dirs[0]`` so the CLI agrees with
        # MCP ``_mem_add_core`` and ``mm context memory-migrate`` —
        # users who remap ``memory_dirs`` would otherwise see split
        # writes between CLI and MCP. An empty list refuses instead of
        # falling back to the historical literal: the "index nothing"
        # state must not write into a directory the active config
        # disabled (#1768). Project tiers resolve independently.
        if scope == "user":
            from memtomem.memory_scope import require_user_base

            base = _resolve_memory_scope_dir(
                scope, project_root, require_user_base(comp.config.indexing.memory_dirs)
            )
        else:
            base = _resolve_memory_scope_dir(scope, project_root)
        if scope != "user":
            from memtomem.memory_scope import (
                is_project_tier_registered,
                project_tier_registration_error,
            )

            pmdirs = comp.config.indexing.project_memory_dirs
            if not is_project_tier_registered(base, pmdirs):
                raise click.ClickException(project_tier_registration_error(base, scope))
        from memtomem.memory_scope import day_file_name, namespace_mix_refusal

        # Only set for the default day-file target: the base the day file is
        # rebuilt under when the in-lock namespace disagrees with the pre-lock
        # guess its name was built from (issue #2005).
        day_base: Path | None = None
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        default_ns = comp.config.namespace.default_namespace
        if file_name:
            if file_name.startswith("/") or file_name.startswith("\\") or ".." in file_name:
                raise click.ClickException("File path must be relative and must not contain '..'")
            target = (base / file_name).resolve()
            try:
                target.relative_to(base)
            except ValueError:
                raise click.ClickException("File path escapes memory directory")
        else:
            # Issue #2005: one day file per namespace. A single day file
            # shared by two namespaces loses one of them as soon as chunk
            # merging joins their entries, because ``index_file`` stamps one
            # namespace across everything it re-chunks.
            day_base = base
            candidate_ns = namespace or await resolve_session_write_namespace(comp.storage)
            target = (base / day_file_name(candidate_ns, default_ns, date_str=date_str)).resolve()

        # ADR-0011 PR-D review round 7: Gate B for project_shared must
        # require an explicit ``--confirm-project-shared`` regardless of
        # ``--yes``. ``--yes`` is a generic "skip prompts" flag users
        # alias for unrelated reasons; treating it as Gate B satisfaction
        # would let ``mm mem add --scope project_shared --yes`` silently
        # write to git-tracked tier without an explicit project-shared
        # opt-in (MCP ``mem_add`` requires ``confirm_project_shared=True``
        # regardless — CLI parity).
        if scope == "project_shared" and not confirm_project_shared:
            if yes:
                raise click.ClickException(
                    "--scope project_shared requires --confirm-project-shared. "
                    "--yes alone is not sufficient: project_shared writes go to "
                    "the git-tracked memory tier and require explicit opt-in."
                )
            if not _prompt_project_shared_confirm(target, err=as_json):
                if as_json:
                    # Declining the prompt is a handled outcome — surface
                    # it as the write-command JSON error shape (exit 0)
                    # instead of the text path's click.Abort.
                    raise click.ClickException("cancelled at project_shared confirmation prompt")
                raise click.Abort()

        from memtomem.context._atomic import (
            _CRUD_SIDECAR_LOCK_BUDGET_S,
            memory_lock_path,
            async_file_lock,
        )

        # #1587: hold the target file's cross-process sidecar (L2) across append
        # + reindex + tag-merge so a concurrent MCP mem_edit/mem_delete rollback
        # (this CLI runs in a separate process from the MCP server) cannot erase
        # this appended entry. ``lock_held=True`` skips the nested engine acquire.
        # Bounded re-target loop (issue #2005): the day file's name depends
        # on the namespace, and the namespace is only authoritative once
        # resolved inside the lock. Nothing durable happens before the
        # append, so an aborted attempt leaves no trace.
        for _attempt in range(NS_RETARGET_ATTEMPTS):
            try:
                async with async_file_lock(
                    memory_lock_path(target), timeout=_CRUD_SIDECAR_LOCK_BUDGET_S
                ):
                    # Resolved inside the lock, like ``_mem_add_core`` — the write is
                    # attributed to whichever session is live at write time, not at
                    # command-parse time (#1991). ``None`` leaves the engine's
                    # namespace rules in charge, which is the pre-#1991 behavior.
                    effective_ns = namespace or await resolve_session_write_namespace(comp.storage)
                    if day_base is not None:
                        desired = (
                            day_base / day_file_name(effective_ns, default_ns, date_str=date_str)
                        ).resolve()
                        if desired != target:
                            # The session changed namespaces while we waited;
                            # re-acquire on the day file that namespace owns
                            # rather than appending to one named for another.
                            target = desired
                            continue
                    mix_err = (
                        None
                        if allow_namespace_mix
                        else await namespace_mix_refusal(
                            index_engine=comp.index_engine,
                            storage=comp.storage,
                            default_namespace=default_ns,
                            target=target,
                            effective_ns=effective_ns,
                            override_hint="--allow-namespace-mix",
                        )
                    )
                    if mix_err is not None:
                        raise click.ClickException(mix_err)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    await asyncio.to_thread(append_entry, target, content, title=title, tags=tags)
                    # Guarded above (``enforce_write_guard``); skip the engine gate (ADR-0006 PR-A).
                    stats = await comp.index_engine.index_file(
                        target, namespace=effective_ns, already_scanned=True, lock_held=True
                    )

                    # Apply tags to indexed chunks (chunker doesn't parse tag text
                    # from content). Inside the lock — keyed to this file's chunks.
                    if tags and stats.indexed_chunks > 0:
                        chunks = await comp.storage.list_chunks_by_source(target)
                        updated = []
                        for c in chunks:
                            merged = set(c.metadata.tags) | set(tags)
                            if merged != set(c.metadata.tags):
                                c.metadata = c.metadata.__class__(
                                    **{
                                        **{
                                            f: getattr(c.metadata, f)
                                            for f in c.metadata.__dataclass_fields__
                                        },
                                        "tags": tuple(sorted(merged)),
                                    }
                                )
                                updated.append(c)
                        if updated:
                            await comp.storage.upsert_chunks(updated)
            except TimeoutError as exc:
                raise click.ClickException(
                    f"{target} is locked by another process (another server or "
                    "migrate in flight); retry."
                ) from exc
            break
        else:
            raise click.ClickException(NS_RETARGET_EXHAUSTED_ERROR)

        if as_json:
            click.echo(
                json.dumps(
                    {
                        "ok": True,
                        "target": str(target),
                        "chunks": stats.indexed_chunks,
                        # ``None`` when the write was un-pinned and the engine's
                        # namespace rules decided — the ack reports what this
                        # command pinned, not what the engine resolved.
                        "namespace": effective_ns,
                    }
                )
            )
        else:
            click.echo(f"Added to {target} ({stats.indexed_chunks} chunks indexed)")
            if effective_ns is not None:
                # Surfaced so a session-inherited redirect is never silent: a
                # plain ``mm search`` hides system namespaces.
                click.echo(f"  Namespace: {effective_ns}")


@click.command()
@click.option(
    "--since", default=None, help="Start date (YYYY, YYYY-MM, YYYY-MM-DD, or ISO datetime)"
)
@click.option("--until", default=None, help="End date (exclusive, same formats)")
@click.option("--limit", "-l", default=20, help="Number of recent chunks")
@click.option("--source-filter", "-s", default=None, help="Filter by source")
@click.option(
    "--tag-filter",
    "-t",
    default=None,
    help="Comma-separated tags, matching ANY (applied before --limit)",
)
@click.option("--namespace", "-n", default=None, help="Namespace filter")
@click.option(
    "--scope",
    default=None,
    help=(
        "Scope filter (ADR-0011): single, comma list, or glob. "
        "Default: in-project = user + this project's tiers; "
        "out-of-project = user only."
    ),
)
@click.option("--format", "fmt", type=click.Choice(["table", "json", "plain"]), default="table")
def recall(
    since: str | None,
    until: str | None,
    limit: int,
    source_filter: str | None,
    tag_filter: str | None,
    namespace: str | None,
    scope: str | None,
    fmt: str,
) -> None:
    """Recall recent memory chunks."""
    try:
        asyncio.run(_recall(since, until, limit, source_filter, tag_filter, namespace, scope, fmt))
    except click.ClickException:
        raise
    except Exception as e:
        raise_cli_error(e)


async def _recall(
    since: str | None,
    until: str | None,
    limit: int,
    source_filter: str | None,
    tag_filter: str | None,
    namespace: str | None,
    scope: str | None,
    fmt: str,
) -> None:
    from memtomem.cli._bootstrap import cli_components
    from memtomem.cli._empty_results import explain_empty_result
    from memtomem.models import NamespaceFilter, ScopeFilter
    from memtomem.server.helpers import _parse_recall_date
    from memtomem.server.tools.search import _resolve_project_context_root

    since_dt = _parse_recall_date(since) if since else None
    until_dt = _parse_recall_date(until, end_of_period=True) if until else None

    async with cli_components() as comp:
        ns_filter = NamespaceFilter.parse(
            namespace,
            system_prefixes=tuple(comp.config.search.system_namespace_prefixes),
        )
        scope_filter = ScopeFilter.parse(scope)
        project_context_root = _resolve_project_context_root(comp)
        chunks = await comp.storage.recall_chunks(
            since=since_dt,
            until=until_dt,
            limit=limit,
            source_filter=source_filter,
            tag_filter=tag_filter,
            namespace_filter=ns_filter,
            scope_filter=scope_filter,
            project_context_root=project_context_root,
        )
        # Inside the block: the explainer reads the store, which
        # ``cli_components`` closes on exit (#2255). Gated on the format that
        # prints it, so ``--format json`` neither pays for the read nor can
        # fail on it.
        empty_message = (
            await explain_empty_result(
                comp.storage,
                namespace=namespace,
                filters=[
                    (flag, value)
                    for flag, value in (
                        ("--since", since),
                        ("--until", until),
                        ("--source-filter", source_filter),
                        ("--tag-filter", tag_filter),
                        ("--namespace", namespace),
                        ("--scope", scope),
                    )
                    # Empty strings are dropped, not just ``None``: the
                    # query normalizes them away (``namespace or
                    # current_namespace``), so listing one as a filter that
                    # might be responsible contradicts what actually ran.
                    if value
                ],
                count_flag="-l",
            )
            if not chunks and fmt in ("table", "plain")
            else ""
        )

    if not chunks and fmt in ("table", "plain"):
        click.secho(empty_message, fg="yellow", err=True)

    if fmt == "json":
        out = [
            {
                "id": str(c.id),
                "source": str(c.metadata.source_file),
                "content": c.content[:200],
                "created_at": c.created_at.isoformat(),
            }
            for c in chunks
        ]
        click.echo(json.dumps(out, indent=2, ensure_ascii=False))
    elif fmt == "plain":
        for c in chunks:
            click.echo(f"{c.metadata.source_file} ({c.created_at.isoformat()})")
            click.echo(c.content[:200])
            click.echo()
    else:
        # Validity column appears only when at least one chunk has a
        # window — keeps the default table compact for the common case
        # where no chunks opted into temporal-validity frontmatter.
        # Per temporal-validity RFC §CLI surfacing.
        show_validity = any(
            c.metadata.valid_from_unix is not None or c.metadata.valid_to_unix is not None
            for c in chunks
        )
        if show_validity:
            click.echo(f"{'Source':<40}{'Created':<18}{'Validity':<26}{'Content'}")
            click.echo("-" * 100)
            for c in chunks:
                src = str(c.metadata.source_file)
                if len(src) > 38:
                    src = "..." + src[-35:]
                snippet = c.content[:40].replace("\n", " ")
                vw = _render_validity_window(c.metadata.valid_from_unix, c.metadata.valid_to_unix)
                click.echo(
                    f"{src:<40}{c.created_at.strftime('%Y-%m-%d %H:%M'):<18}{vw:<26}{snippet}"
                )
        else:
            click.echo(f"{'Source':<40}{'Created':<25}{'Content'}")
            click.echo("-" * 80)
            for c in chunks:
                src = str(c.metadata.source_file)
                if len(src) > 38:
                    src = "..." + src[-35:]
                snippet = c.content[:40].replace("\n", " ")
                click.echo(f"{src:<40}{c.created_at.strftime('%Y-%m-%d %H:%M'):<25}{snippet}")
        click.echo(f"\n{len(chunks)} chunk(s)")
