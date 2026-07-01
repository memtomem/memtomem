"""CLI: mm shell — interactive REPL for memtomem."""

from __future__ import annotations

import asyncio
import shlex

import click


@click.command()
def shell() -> None:
    """Interactive memory shell — search, add, recall, and browse."""
    try:
        asyncio.run(_shell_loop())
    except (KeyboardInterrupt, EOFError):
        click.echo("\nBye!")


async def _shell_loop() -> None:
    from memtomem.cli._bootstrap import cli_components

    click.secho("memtomem shell", fg="cyan", bold=True)
    click.secho("Commands: search, ask, add, recall, tags, stats, help, quit", fg="bright_black")
    click.echo()

    async with cli_components() as comp:
        while True:
            try:
                raw = click.prompt(
                    click.style("mm", fg="green"),
                    prompt_suffix="> ",
                    default="",
                    show_default=False,
                )
            except (KeyboardInterrupt, EOFError):
                click.echo("\nBye!")
                return

            line = raw.strip()
            if not line:
                continue

            try:
                parts = shlex.split(line)
            except ValueError:
                parts = line.split()

            cmd = parts[0].lower()
            args = parts[1:]

            if cmd in ("quit", "exit", "q"):
                click.echo("Bye!")
                return
            elif cmd == "help":
                _show_help()
                continue

            try:
                if cmd in ("search", "s"):
                    await _cmd_search(comp, args)
                elif cmd == "ask":
                    await _cmd_ask(comp, args)
                elif cmd == "add":
                    await _cmd_add(comp, args)
                elif cmd in ("recall", "r"):
                    await _cmd_recall(comp, args)
                elif cmd == "tags":
                    await _cmd_tags(comp)
                elif cmd in ("stats", "status"):
                    await _cmd_stats(comp)
                elif cmd in ("index", "idx"):
                    await _cmd_index(comp, args)
                else:
                    # Treat as implicit search
                    await _cmd_search(comp, parts)
            except Exception as exc:
                click.secho(f"Error: {exc}", fg="red")


def _show_help() -> None:
    click.secho("Commands:", fg="cyan")
    click.echo("  search <query>        Search memories (alias: s)")
    click.echo("  ask <question>        Q&A grounded in memories")
    click.echo("  add <content>         Add a memory")
    click.echo("  recall [--days N]     Recall recent memories")
    click.echo("  tags                  List tags with counts")
    click.echo("  stats                 Show index statistics")
    click.echo("  index [path]          Index a directory")
    click.echo("  help                  Show this help")
    click.echo("  quit                  Exit shell (alias: q, exit)")
    click.echo()
    click.secho("Tip: type anything without a command to search.", fg="bright_black")


async def _cmd_search(comp, args: list[str]) -> None:
    if not args:
        click.secho("Usage: search <query>", fg="yellow")
        return

    query = " ".join(args)
    # ADR-0011 PR-D round 9: thread project context so the always-on
    # scope filter sees the same boundary the rest of the read surface
    # does. Without this, an interactive shell session inside a
    # registered project silently loses project_shared / project_local
    # rows on every search.
    from memtomem.server.tools.search import _resolve_project_context_root

    project_context_root = _resolve_project_context_root(comp)
    results, stats = await comp.search_pipeline.search(
        query, top_k=10, project_context_root=project_context_root
    )

    if not results:
        click.secho("No results found.", fg="yellow")
        return

    for r in results:
        src = str(r.chunk.metadata.source_file)
        if len(src) > 35:
            src = "..." + src[-32:]
        heading = (
            " > ".join(r.chunk.metadata.heading_hierarchy)
            if r.chunk.metadata.heading_hierarchy
            else ""
        )
        label = heading or src
        snippet = r.chunk.content[:100].replace("\n", " ")

        click.echo(
            f"  {click.style(f'[{r.rank}]', fg='cyan')} {click.style(f'{r.score:.3f}', fg='bright_black')} {label}"
        )
        click.echo(f"      {snippet}")

    click.echo(
        f"\n  {stats.final_total} results ({stats.bm25_candidates} BM25 + {stats.dense_candidates} dense)"
    )


async def _cmd_ask(comp, args: list[str]) -> None:
    if not args:
        click.secho("Usage: ask <question>", fg="yellow")
        return

    question = " ".join(args)
    # ADR-0011 PR-D round 9: same project-context threading as
    # ``_cmd_search`` — interactive ``ask`` must see project tier rows
    # when run inside a registered project.
    from memtomem.server.tools.search import _resolve_project_context_root

    project_context_root = _resolve_project_context_root(comp)
    results, _ = await comp.search_pipeline.search(
        question, top_k=5, project_context_root=project_context_root
    )

    if not results:
        click.secho("No relevant memories found.", fg="yellow")
        return

    click.secho(f"\nQuestion: {question}", fg="cyan", bold=True)
    click.echo()

    for r in results:
        heading = (
            " > ".join(r.chunk.metadata.heading_hierarchy)
            if r.chunk.metadata.heading_hierarchy
            else ""
        )
        source = str(r.chunk.metadata.source_file).split("/")[-1]
        label = heading or source

        click.echo(f"  {click.style(f'[{r.rank}]', fg='green')} {label} ({r.score:.2f})")
        # Show full content for top 3, truncated for rest
        content = r.chunk.content.strip()
        if r.rank > 3:
            content = content[:200] + "..." if len(content) > 200 else content
        for line in content.split("\n")[:8]:
            click.echo(f"      {line}")
        click.echo()

    click.secho("Answer based on the context above.", fg="bright_black")


async def _cmd_add(comp, args: list[str]) -> None:
    if not args:
        click.secho("Usage: add <content>", fg="yellow")
        click.echo("  Or type 'add' and enter multi-line content (Ctrl+D to finish)")
        return

    content = " ".join(args)

    from datetime import datetime, timezone
    from pathlib import Path

    from memtomem import privacy
    from memtomem.tools.memory_writer import append_entry

    # Interactive shell has no inline ``--force-unsafe`` syntax; on a
    # hit the user is told to retry via ``mm add --force-unsafe`` so
    # the bypass remains an explicit, audit-logged action.
    guard = privacy.enforce_write_guard(content, surface="cli_shell_add")
    if guard.decision == "blocked":
        click.secho(
            f"Content matches {len(guard.hits)} privacy pattern(s); write rejected. "
            "Retry via `mm add --force-unsafe '<content>'` to bypass (audit-logged).",
            fg="red",
        )
        return

    if not comp.config.indexing.memory_dirs:
        click.secho("No memory directories configured. Run 'mm init' first.", fg="red")
        return
    base = Path(comp.config.indexing.memory_dirs[0]).expanduser().resolve()
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    target = base / f"{date_str}.md"
    target.parent.mkdir(parents=True, exist_ok=True)

    append_entry(target, content)
    # Guarded above (``enforce_write_guard``); skip the engine gate (ADR-0006 PR-A).
    stats = await comp.index_engine.index_file(target, already_scanned=True)

    click.secho(f"Added to {target.name} ({stats.indexed_chunks} chunks indexed)", fg="green")


async def _cmd_recall(comp, args: list[str]) -> None:
    from datetime import datetime, timedelta, timezone

    days = 7
    for i, a in enumerate(args):
        if a == "--days" and i + 1 < len(args):
            try:
                days = int(args[i + 1])
            except ValueError:
                pass

    since = datetime.now(timezone.utc) - timedelta(days=days)
    # ADR-0011 PR-D round 9: thread project context onto the always-on
    # scope filter — interactive ``recall`` in a registered project
    # cwd should surface project_shared / project_local rows alongside
    # user-tier ones, matching the contract ``mm mem recall`` honours.
    from memtomem.server.tools.search import _resolve_project_context_root

    project_context_root = _resolve_project_context_root(comp)
    chunks = await comp.storage.recall_chunks(
        since=since, limit=20, project_context_root=project_context_root
    )

    if not chunks:
        click.secho(f"No memories in the last {days} days.", fg="yellow")
        return

    click.secho(f"Recent memories (last {days} days):", fg="cyan")
    for c in chunks:
        src = str(c.metadata.source_file).split("/")[-1]
        snippet = c.content[:80].replace("\n", " ")
        click.echo(f"  {click.style(src, fg='bright_black')} {snippet}")


async def _cmd_tags(comp) -> None:
    tag_counts = await comp.storage.get_tag_counts()
    if not tag_counts:
        click.secho("No tags found.", fg="yellow")
        return

    click.secho("Tags:", fg="cyan")
    for tag, count in tag_counts[:30]:
        bar = "#" * min(count, 20)
        click.echo(f"  {tag:<25} {count:>4}  {click.style(bar, fg='green')}")


async def _cmd_stats(comp) -> None:
    stats = await comp.storage.get_stats()
    click.secho("Index Statistics:", fg="cyan")
    click.echo(f"  Chunks:    {stats['total_chunks']}")
    click.echo(f"  Sources:   {stats['total_sources']}")

    tag_counts = await comp.storage.get_tag_counts()
    click.echo(f"  Tags:      {len(tag_counts)}")

    namespaces = await comp.storage.list_namespaces()
    click.echo(f"  Namespaces: {len(namespaces)}")


async def _cmd_index(comp, args: list[str]) -> None:
    from pathlib import Path

    if args:
        path = Path(args[0]).expanduser().resolve()
    else:
        if not comp.config.indexing.memory_dirs:
            click.secho("No memory directories configured. Run 'mm init' first.", fg="red")
            return
        path = Path(comp.config.indexing.memory_dirs[0]).expanduser().resolve()

    if not path.exists():
        click.secho(f"Path not found: {path}", fg="red")
        return

    click.echo(f"Indexing {path}...")
    stats = await comp.index_engine.index_path(path, recursive=True)
    click.secho(
        f"Done: {stats.total_files} files, {stats.indexed_chunks} chunks ({stats.duration_ms}ms)",
        fg="green",
    )
    if stats.blocked_files:
        # ADR-0006 PR-A: secret-bearing files skipped by the redaction gate.
        click.secho(f"  {stats.blocked_files} file(s) blocked by redaction guard", fg="yellow")
