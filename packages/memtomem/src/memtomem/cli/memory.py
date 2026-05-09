"""CLI: memtomem add / memtomem recall."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import get_args

import click

from memtomem.config import TargetScope
from memtomem.memory_scope import (
    MemoryScopeError,
    resolve_memory_scope_dir as _resolve_memory_scope_dir_core,
)


_MEMORY_SCOPE_CHOICES = list(get_args(TargetScope))


def _resolve_memory_scope_dir(
    scope: TargetScope,
    project_root: Path | None,
    user_base: Path,
) -> Path:
    """ADR-0011 scope → directory, surfaced as ``ClickException`` for the CLI."""
    try:
        return _resolve_memory_scope_dir_core(scope, project_root, user_base)
    except MemoryScopeError as exc:
        raise click.ClickException(str(exc)) from exc


def _prompt_project_shared_confirm(target: Path) -> bool:
    """Prompt before writing to the git-tracked project_shared tier."""
    click.secho("This will write to the git-tracked project memory directory:", fg="yellow")
    click.echo(f"  {target}")
    return click.confirm("Continue?", default=False)


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


@click.command()
@click.argument("content")
@click.option("--title", "-t", default=None, help="Entry title")
@click.option("--tags", default=None, help="Comma-separated tags")
@click.option(
    "--file", "file_name", default=None, help="Target file (relative to ~/.memtomem/memories/)"
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
    "--yes",
    "-y",
    is_flag=True,
    default=False,
    help="Skip confirmation prompts.",
)
def add(
    content: str,
    title: str | None,
    tags: str | None,
    file_name: str | None,
    force_unsafe: bool,
    scope: TargetScope,
    confirm_project_shared: bool,
    yes: bool,
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
            )
        )
    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(str(e)) from e


async def _add(
    content: str,
    title: str | None,
    tags: list[str],
    file_name: str | None,
    force_unsafe: bool = False,
    scope: TargetScope = "user",
    confirm_project_shared: bool = False,
    yes: bool = False,
) -> None:
    from memtomem import privacy
    from memtomem.cli._bootstrap import cli_components
    from memtomem.server.tools.search import _resolve_project_context_root
    from memtomem.tools.memory_writer import append_entry

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
        user_base = Path("~/.memtomem/memories")
        base = _resolve_memory_scope_dir(scope, project_root, user_base)
        if scope != "user":
            from memtomem.memory_scope import (
                is_project_tier_registered,
                project_tier_registration_error,
            )

            pmdirs = comp.config.indexing.project_memory_dirs
            if not is_project_tier_registered(base, pmdirs):
                raise click.ClickException(project_tier_registration_error(base, scope))
        if file_name:
            if file_name.startswith("/") or file_name.startswith("\\") or ".." in file_name:
                raise click.ClickException("File path must be relative and must not contain '..'")
            target = (base / file_name).resolve()
            try:
                target.relative_to(base)
            except ValueError:
                raise click.ClickException("File path escapes memory directory")
        else:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            target = (base / f"{date_str}.md").resolve()

        if (
            scope == "project_shared"
            and not confirm_project_shared
            and not yes
            and not _prompt_project_shared_confirm(target)
        ):
            raise click.Abort()

        target.parent.mkdir(parents=True, exist_ok=True)
        append_entry(target, content, title=title, tags=tags)
        stats = await comp.index_engine.index_file(target)

        # Apply tags to indexed chunks (chunker doesn't parse tag text from content)
        if tags and stats.indexed_chunks > 0:
            chunks = await comp.storage.list_chunks_by_source(target)
            updated = []
            for c in chunks:
                merged = set(c.metadata.tags) | set(tags)
                if merged != set(c.metadata.tags):
                    c.metadata = c.metadata.__class__(
                        **{
                            **{f: getattr(c.metadata, f) for f in c.metadata.__dataclass_fields__},
                            "tags": tuple(sorted(merged)),
                        }
                    )
                    updated.append(c)
            if updated:
                await comp.storage.upsert_chunks(updated)

        click.echo(f"Added to {target} ({stats.indexed_chunks} chunks indexed)")


@click.command()
@click.option(
    "--since", default=None, help="Start date (YYYY, YYYY-MM, YYYY-MM-DD, or ISO datetime)"
)
@click.option("--until", default=None, help="End date (exclusive, same formats)")
@click.option("--limit", "-l", default=20, help="Number of recent chunks")
@click.option("--source-filter", "-s", default=None, help="Filter by source")
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
    namespace: str | None,
    scope: str | None,
    fmt: str,
) -> None:
    """Recall recent memory chunks."""
    try:
        asyncio.run(_recall(since, until, limit, source_filter, namespace, scope, fmt))
    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(str(e)) from e


async def _recall(
    since: str | None,
    until: str | None,
    limit: int,
    source_filter: str | None,
    namespace: str | None,
    scope: str | None,
    fmt: str,
) -> None:
    from memtomem.cli._bootstrap import cli_components
    from memtomem.models import NamespaceFilter, ScopeFilter
    from memtomem.server.helpers import _parse_recall_date
    from memtomem.server.tools.search import _resolve_project_context_root

    since_dt = _parse_recall_date(since) if since else None
    until_dt = _parse_recall_date(until) if until else None

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
            namespace_filter=ns_filter,
            scope_filter=scope_filter,
            project_context_root=project_context_root,
        )

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
