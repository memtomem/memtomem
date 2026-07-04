"""CLI: ``mm tags`` — list / rename / delete / merge tags across chunks (#688 PR3).

Thin CLI surface over :mod:`memtomem.services.tag_management` — the same
shared service the Web ``/api/tags/{...}`` routes and the MCP ``mem_tag_*``
tools call, so all three surfaces share one write lock, one ``updated_at``
policy, and one search-cache invalidation. Mutations are **global**: they
touch every chunk carrying the tag regardless of scope tier, matching the
``count``/sample the dry-run preview shows.

UX mirrors ``mm gc orphan-projects``: the default is a dry-run preview
(count + sample); ``--apply`` performs the write behind a confirmation
prompt; ``--apply --yes`` is the non-interactive form.
"""

from __future__ import annotations

import asyncio

import click

from memtomem.services import tag_management as tag_svc


@click.group("tags")
def tags() -> None:
    """Manage tags: list, rename, delete, or merge them across all chunks."""


def _print_samples(result: tag_svc.TagOpResult) -> None:
    """Render the dry-run sample chunks (same shape the Web modal shows)."""
    if not result.samples:
        return
    click.echo("\nSample affected chunks:")
    for s in result.samples:
        click.echo(f"  {s.chunk_id}  ({s.source_file})")
        click.echo(f"    tags: [{', '.join(s.current_tags)}]")
        click.echo(f"    {s.content_preview}")


# --------------------------------------------------------------------------- #
# list
# --------------------------------------------------------------------------- #
@tags.command("list")
def list_tags() -> None:
    """List every tag and its chunk count, most frequent first."""
    asyncio.run(_run_list())


async def _run_list() -> None:
    from memtomem.cli._bootstrap import cli_components

    async with cli_components() as comp:
        tag_counts = await comp.storage.get_tag_counts()
        if not tag_counts:
            click.secho("No tags found.", fg="yellow")
            return
        for tag, count in tag_counts:
            click.echo(f"  {tag}  — {count} chunks")
        total = sum(c for _, c in tag_counts)
        click.echo(f"\n{len(tag_counts)} tags across {total} chunk-tag assignments.")


# --------------------------------------------------------------------------- #
# rename
# --------------------------------------------------------------------------- #
@tags.command("rename")
@click.argument("old_tag")
@click.argument("new_tag")
@click.option(
    "--apply",
    "apply_",
    is_flag=True,
    help="Actually write. Without this flag, prints what would change (dry-run).",
)
@click.option(
    "--yes",
    "assume_yes",
    is_flag=True,
    help="Skip the confirmation prompt. Requires --apply.",
)
def rename(old_tag: str, new_tag: str, apply_: bool, assume_yes: bool) -> None:
    """Rename OLD_TAG to NEW_TAG across every chunk that carries it."""
    if assume_yes and not apply_:
        raise click.UsageError("--yes requires --apply.")
    asyncio.run(_run_rename(old_tag, new_tag, apply_=apply_, assume_yes=assume_yes))


async def _run_rename(old_tag: str, new_tag: str, *, apply_: bool, assume_yes: bool) -> None:
    from memtomem.cli._bootstrap import cli_components

    async with cli_components() as comp:
        try:
            preview = await tag_svc.rename_tag(comp.storage, old_tag, new_tag, dry_run=True)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc

        old_s, new_s = old_tag.strip(), new_tag.strip()
        if preview.affected_chunks == 0:
            click.secho(f"No chunks carry tag '{old_s}'. Nothing to do.", fg="yellow")
            return

        click.echo(f"Rename '{old_s}' → '{new_s}' would affect {preview.affected_chunks} chunks.")
        _print_samples(preview)
        if not apply_:
            click.echo("\nRun with --apply to perform the rename.")
            return
        if not assume_yes and not click.confirm(
            f"\nRename '{old_s}' → '{new_s}' across {preview.affected_chunks} chunks?",
            default=False,
        ):
            click.echo("Aborted.")
            return

        result = await tag_svc.rename_tag(
            comp.storage, old_tag, new_tag, dry_run=False, search_pipeline=comp.search_pipeline
        )
        click.secho(
            f"Renamed '{old_s}' → '{new_s}' in {result.affected_chunks} chunks.", fg="green"
        )


# --------------------------------------------------------------------------- #
# delete
# --------------------------------------------------------------------------- #
@tags.command("delete")
@click.argument("name")
@click.option(
    "--apply",
    "apply_",
    is_flag=True,
    help="Actually write. Without this flag, prints what would change (dry-run).",
)
@click.option(
    "--yes",
    "assume_yes",
    is_flag=True,
    help="Skip the confirmation prompt. Requires --apply.",
)
def delete(name: str, apply_: bool, assume_yes: bool) -> None:
    """Remove NAME from every chunk that carries it (the chunks stay indexed)."""
    if assume_yes and not apply_:
        raise click.UsageError("--yes requires --apply.")
    asyncio.run(_run_delete(name, apply_=apply_, assume_yes=assume_yes))


async def _run_delete(name: str, *, apply_: bool, assume_yes: bool) -> None:
    from memtomem.cli._bootstrap import cli_components

    async with cli_components() as comp:
        try:
            preview = await tag_svc.delete_tag(comp.storage, name, dry_run=True)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc

        name_s = name.strip()
        if preview.affected_chunks == 0:
            click.secho(f"No chunks carry tag '{name_s}'. Nothing to do.", fg="yellow")
            return

        click.echo(f"Delete '{name_s}' would affect {preview.affected_chunks} chunks.")
        _print_samples(preview)
        if not apply_:
            click.echo("\nRun with --apply to perform the delete.")
            return
        if not assume_yes and not click.confirm(
            f"\nDelete '{name_s}' from {preview.affected_chunks} chunks?",
            default=False,
        ):
            click.echo("Aborted.")
            return

        result = await tag_svc.delete_tag(
            comp.storage, name, dry_run=False, search_pipeline=comp.search_pipeline
        )
        click.secho(f"Removed '{name_s}' from {result.affected_chunks} chunks.", fg="green")


# --------------------------------------------------------------------------- #
# merge
# --------------------------------------------------------------------------- #
@tags.command("merge")
@click.argument("sources", nargs=-1, required=True)
@click.option("--into", "target", required=True, help="The tag every source is folded into.")
@click.option(
    "--apply",
    "apply_",
    is_flag=True,
    help="Actually write. Without this flag, prints what would change (dry-run).",
)
@click.option(
    "--yes",
    "assume_yes",
    is_flag=True,
    help="Skip the confirmation prompt. Requires --apply.",
)
def merge(sources: tuple[str, ...], target: str, apply_: bool, assume_yes: bool) -> None:
    """Fold one or more SOURCES tags into --into TARGET across all chunks."""
    if assume_yes and not apply_:
        raise click.UsageError("--yes requires --apply.")
    asyncio.run(_run_merge(sources, target, apply_=apply_, assume_yes=assume_yes))


async def _run_merge(
    sources: tuple[str, ...], target: str, *, apply_: bool, assume_yes: bool
) -> None:
    from memtomem.cli._bootstrap import cli_components

    async with cli_components() as comp:
        try:
            preview = await tag_svc.merge_tags(comp.storage, list(sources), target, dry_run=True)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc

        target_s = target.strip()
        src_display = ", ".join(f"'{s.strip()}'" for s in sources if s.strip())
        if preview.affected_chunks == 0:
            # Covers both "no chunk carries a source tag" and "every source
            # collapsed to the target" (e.g. `merge python --into python`) —
            # in the latter the chunks exist, so a membership claim would lie.
            click.secho(
                f"Nothing to merge into '{target_s}' — no chunks would change.", fg="yellow"
            )
            return

        click.echo(
            f"Merge {src_display} → '{target_s}' would affect {preview.affected_chunks} chunks."
        )
        _print_samples(preview)
        if not apply_:
            click.echo("\nRun with --apply to perform the merge.")
            return
        if not assume_yes and not click.confirm(
            f"\nMerge {src_display} → '{target_s}' across {preview.affected_chunks} chunks?",
            default=False,
        ):
            click.echo("Aborted.")
            return

        result = await tag_svc.merge_tags(
            comp.storage, list(sources), target, dry_run=False, search_pipeline=comp.search_pipeline
        )
        click.secho(
            f"Merged {src_display} → '{target_s}' across {result.affected_chunks} chunks.",
            fg="green",
        )
