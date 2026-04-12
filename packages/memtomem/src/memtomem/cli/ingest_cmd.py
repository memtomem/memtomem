"""CLI: mm ingest claude-memory — read-only Claude auto-memory snapshot."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from memtomem.models import Chunk
    from memtomem.server.component_factory import Components
    from memtomem.storage.base import StorageBackend


# Files that sit inside a Claude memory directory but should never be
# indexed as memory content. MEMORY.md is an index (table of contents) whose
# text is just pointers to the other files — indexing it would surface a
# high-score duplicate on every query. README.md is usually meta/how-to-read.
_EXCLUDE_FILENAMES = frozenset({"MEMORY.md", "README.md"})

# Filename prefix → tag. Trailing underscore is required so we only match
# the prefix component, not any substring (``feedbackXYZ.md`` is not a
# feedback note).
_TAG_PREFIXES: tuple[tuple[str, str], ...] = (
    ("feedback_", "feedback"),
    ("project_", "project"),
    ("user_", "user"),
    ("reference_", "reference"),
)

# Mirrors _NS_NAME_RE in storage/sqlite_namespace.py. Any character outside
# this set in the derived slug is replaced with ``_`` before we hand the
# namespace to the index engine.
_NS_SAFE_RE = re.compile(r"[^\w\-.:@ ]")

_NAMESPACE_PREFIX = "claude-memory:"


@click.group()
def ingest() -> None:
    """Ingest memories from external sources."""


@ingest.command("claude-memory")
@click.option(
    "--source",
    "source_path",
    required=True,
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    help=("Path to a Claude auto-memory directory, typically ~/.claude/projects/<slug>/memory/"),
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be indexed without writing to storage.",
)
def claude_memory(source_path: Path, dry_run: bool) -> None:
    """Index a Claude Code auto-memory directory into memtomem.

    Read-only snapshot: the source files stay where they are — memtomem
    records the absolute path as ``source_file`` and indexes the content
    under namespace ``claude-memory:<slug>``. Re-run to pick up new or
    changed files; unchanged files are skipped via content hash.
    """
    try:
        asyncio.run(_run_claude_ingest(source_path, dry_run))
    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(str(e)) from e


async def _run_claude_ingest(source_path: Path, dry_run: bool) -> None:
    resolved = source_path.expanduser().resolve()
    slug = _derive_slug(resolved)
    namespace = _build_namespace(slug)

    files = _discover_files(resolved)
    if not files:
        click.echo(
            click.style(
                f"No indexable markdown files found in {resolved}",
                fg="yellow",
            )
        )
        return

    if dry_run:
        click.echo(f"Would ingest {len(files)} file(s) into namespace '{namespace}' (dry-run):")
        for f in files:
            tags = sorted(_tags_for_file(f))
            click.echo(f"  {f.name}  tags=[{', '.join(tags)}]")
        return

    from memtomem.cli._bootstrap import cli_components

    async with cli_components() as comp:
        summary = await _ingest_files_with_components(comp, files, namespace)

    click.echo(
        f"Ingested {len(files)} file(s) into '{namespace}': "
        f"{summary.indexed} new, {summary.skipped} unchanged, "
        f"{summary.deleted} deleted."
    )
    for err in summary.errors:
        click.echo(click.style(f"  ERROR: {err}", fg="red"))


@dataclass(frozen=True)
class IngestSummary:
    """Aggregate result of ingesting a batch of files."""

    indexed: int
    skipped: int
    deleted: int
    errors: tuple[str, ...]


async def _ingest_files_with_components(
    comp: Components,
    files: list[Path],
    namespace: str,
) -> IngestSummary:
    """Index *files* via *comp.index_engine* and tag each freshly-indexed file.

    Split out from ``_run_claude_ingest`` so tests can drive the ingestion
    loop with a real ``components`` fixture instead of going through
    ``cli_components()`` (which requires a global ~/.memtomem/config.json).
    """
    total_indexed = 0
    total_skipped = 0
    total_deleted = 0
    errors: list[str] = []
    for f in files:
        stats = await comp.index_engine.index_file(f, namespace=namespace)
        total_indexed += stats.indexed_chunks
        total_skipped += stats.skipped_chunks
        total_deleted += stats.deleted_chunks
        if stats.errors:
            errors.extend(stats.errors)

        if stats.indexed_chunks > 0:
            await _apply_tags(comp.storage, f, _tags_for_file(f))

    comp.search_pipeline.invalidate_cache()
    return IngestSummary(
        indexed=total_indexed,
        skipped=total_skipped,
        deleted=total_deleted,
        errors=tuple(errors),
    )


def _discover_files(source_root: Path) -> list[Path]:
    """Return indexable ``.md`` files directly under *source_root*.

    Flat (non-recursive) — Claude memory directories are a single level by
    convention. Sorted for deterministic output. Skips hidden files and the
    known index/readme exclusion list.
    """
    files: list[Path] = []
    for f in sorted(source_root.iterdir()):
        if not f.is_file():
            continue
        if f.suffix != ".md":
            continue
        if f.name.startswith("."):
            continue
        if f.name in _EXCLUDE_FILENAMES:
            continue
        files.append(f)
    return files


def _derive_slug(source_path: Path) -> str:
    """Extract the project slug from a Claude memory path.

    Expected layout is ``.../projects/<slug>/memory/``; in that case the
    slug is the parent directory's name. For any other layout we fall back
    to the source directory's own name so the caller still gets a
    stable namespace.
    """
    if source_path.name == "memory":
        return source_path.parent.name or "default"
    return source_path.name or "default"


def _build_namespace(slug: str) -> str:
    """Return ``claude-memory:<slug>`` with *slug* sanitized for storage.

    Characters outside the SQLite namespace allowlist (``_NS_NAME_RE``) are
    replaced with ``_`` so downstream storage never rejects the namespace.
    """
    safe = _NS_SAFE_RE.sub("_", slug)
    return f"{_NAMESPACE_PREFIX}{safe}"


def _tags_for_file(file_path: Path) -> set[str]:
    """Return the tag set to apply to every chunk from *file_path*.

    Always includes ``claude-memory`` (source marker). Files whose name
    starts with a known prefix (``feedback_``, ``project_``, ``user_``,
    ``reference_``) also get a type tag derived from the prefix.
    """
    tags = {"claude-memory"}
    for prefix, tag in _TAG_PREFIXES:
        if file_path.name.startswith(prefix):
            tags.add(tag)
            break
    return tags


async def _apply_tags(
    storage: StorageBackend,
    source_file: Path,
    new_tags: set[str],
) -> None:
    """Merge *new_tags* into every chunk for *source_file* and upsert.

    No-op when the chunk already has the full tag set. Mirrors the tag
    application pattern in ``server/tools/importers.py`` so behavior is
    consistent across Notion/Obsidian/Claude ingestion paths.
    """
    chunks = await storage.list_chunks_by_source(source_file)
    if not chunks:
        return

    dirty: list[Chunk] = []
    for c in chunks:
        existing = set(c.metadata.tags)
        merged = existing | new_tags
        if merged == existing:
            continue
        c.metadata = c.metadata.__class__(
            **{
                **{field: getattr(c.metadata, field) for field in c.metadata.__dataclass_fields__},
                "tags": tuple(sorted(merged)),
            }
        )
        dirty.append(c)
    if dirty:
        await storage.upsert_chunks(dirty)
