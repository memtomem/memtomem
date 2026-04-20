"""CLI: mm ingest claude-memory — read-only Claude auto-memory snapshot."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import click

from memtomem.storage.sqlite_namespace import sanitize_namespace_segment

if TYPE_CHECKING:
    from collections.abc import Callable

    from memtomem.indexing.engine import IndexEngine
    from memtomem.models import Chunk
    from memtomem.search.pipeline import SearchPipeline
    from memtomem.storage.base import StorageBackend
    from memtomem.storage.sqlite_backend import SqliteBackend


class IngestComponents(Protocol):
    """Structural contract consumed by :func:`_ingest_files_with_components`.

    Both :class:`memtomem.server.component_factory.Components` and
    :class:`memtomem.server.context.AppContext` conform: the helper only
    reads ``index_engine``, ``storage``, and ``search_pipeline``, all of
    which are present on both dataclasses.
    """

    @property
    def index_engine(self) -> IndexEngine: ...
    @property
    def storage(self) -> SqliteBackend: ...
    @property
    def search_pipeline(self) -> SearchPipeline: ...


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

_NAMESPACE_PREFIX = "claude-memory:"


def _discover_claude_slug_dirs(parent: Path) -> list[Path]:
    """Find all ``<slug>/memory/`` subdirectories under *parent*.

    Used for multi-slug ingest when *parent* is e.g.
    ``~/.claude/projects/``.  Returns the ``memory/`` paths sorted by
    slug name for deterministic output.
    """
    dirs: list[Path] = []
    if not parent.is_dir():
        return dirs
    for child in sorted(parent.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        mem_dir = child / "memory"
        if mem_dir.is_dir():
            dirs.append(mem_dir)
    return dirs


# ── Gemini memory constants ─────────────────────────────────────────

_GEMINI_NAMESPACE_PREFIX = "gemini-memory:"
_GEMINI_BASE_TAG = "gemini-memory"

# ── Codex memory constants ──────────────────────────────────────────

_CODEX_NAMESPACE_PREFIX = "codex-memory:"
_CODEX_BASE_TAG = "codex-memory"
_CODEX_EXCLUDE_FILENAMES = frozenset({"README.md"})


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

    # Single-slug fast path: source is a memory/ directory with .md files.
    files = _discover_files(resolved)
    if files:
        await _run_claude_single_slug(resolved, files, dry_run)
        return

    # Multi-slug: source is a parent (e.g. ~/.claude/projects/) containing
    # multiple <slug>/memory/ subdirectories.
    slug_dirs = _discover_claude_slug_dirs(resolved)
    if slug_dirs:
        await _run_claude_multi_slug(slug_dirs, dry_run)
        return

    click.echo(
        click.style(
            f"No indexable markdown files found in {resolved}",
            fg="yellow",
        )
    )


async def _run_claude_single_slug(mem_dir: Path, files: list[Path], dry_run: bool) -> None:
    """Ingest a single Claude memory directory."""
    slug = _derive_slug(mem_dir)
    namespace = _build_namespace(slug)

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


async def _run_claude_multi_slug(slug_dirs: list[Path], dry_run: bool) -> None:
    """Ingest all discovered Claude memory slug directories."""
    total_indexed = 0
    total_skipped = 0
    total_deleted = 0
    total_errors: list[str] = []

    if dry_run:
        click.echo(f"Discovered {len(slug_dirs)} slug(s) (dry-run):")
        for mem_dir in slug_dirs:
            slug = _derive_slug(mem_dir)
            namespace = _build_namespace(slug)
            files = _discover_files(mem_dir)
            click.echo(f"\n  {namespace} ({len(files)} file(s)):")
            for f in files:
                tags = sorted(_tags_for_file(f))
                click.echo(f"    {f.name}  tags=[{', '.join(tags)}]")
        return

    from memtomem.cli._bootstrap import cli_components

    async with cli_components() as comp:
        for mem_dir in slug_dirs:
            slug = _derive_slug(mem_dir)
            namespace = _build_namespace(slug)
            files = _discover_files(mem_dir)
            if not files:
                continue
            summary = await _ingest_files_with_components(comp, files, namespace)
            total_indexed += summary.indexed
            total_skipped += summary.skipped
            total_deleted += summary.deleted
            total_errors.extend(summary.errors)
            click.echo(
                f"  {namespace}: {summary.indexed} new, "
                f"{summary.skipped} unchanged, {summary.deleted} deleted"
            )

    click.echo(
        f"\nTotal across {len(slug_dirs)} slug(s): "
        f"{total_indexed} new, {total_skipped} unchanged, "
        f"{total_deleted} deleted."
    )
    for err in total_errors:
        click.echo(click.style(f"  ERROR: {err}", fg="red"))


@dataclass(frozen=True)
class IngestSummary:
    """Aggregate result of ingesting a batch of files."""

    indexed: int
    skipped: int
    deleted: int
    errors: tuple[str, ...]


async def _ingest_files_with_components(
    comp: IngestComponents,
    files: list[Path],
    namespace: str,
    *,
    tag_fn: Callable[[Path], set[str]] | None = None,
) -> IngestSummary:
    """Index *files* via *comp.index_engine* and tag each freshly-indexed file.

    Split out from the per-source ``_run_*_ingest`` helpers so tests can
    drive the ingestion loop with a real ``components`` fixture instead of
    going through ``cli_components()`` (which requires a global config).

    *tag_fn* defaults to ``_tags_for_file`` (Claude tags) when ``None``.
    """
    effective_tag_fn = tag_fn if tag_fn is not None else _tags_for_file
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
            await _apply_tags(comp.storage, f, effective_tag_fn(f))

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


def _build_namespace(slug: str, prefix: str = _NAMESPACE_PREFIX) -> str:
    """Return ``<prefix><slug>`` with *slug* sanitized for storage.

    Characters outside the SQLite namespace allowlist (``_NS_NAME_RE``) are
    replaced with ``_`` so downstream storage never rejects the namespace.
    Default *prefix* is ``claude-memory:`` for backward compatibility.
    """
    return f"{prefix}{sanitize_namespace_segment(slug)}"


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


# =====================================================================
# mm ingest gemini-memory
# =====================================================================


@ingest.command("gemini-memory")
@click.option(
    "--source",
    "source_path",
    required=True,
    type=click.Path(exists=True, file_okay=True, dir_okay=True, path_type=Path),
    help=(
        "Path to a GEMINI.md file or a directory containing one. "
        "Global memories live at ~/.gemini/GEMINI.md; per-project "
        "memories sit in the project root."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be indexed without writing to storage.",
)
def gemini_memory(source_path: Path, dry_run: bool) -> None:
    """Index a Gemini CLI GEMINI.md memory file into memtomem.

    Read-only snapshot: the source file stays where it is — memtomem
    records the absolute path as ``source_file`` and indexes the content
    under namespace ``gemini-memory:<slug>``. Re-run to pick up changes;
    unchanged content is skipped via content hash.
    """
    try:
        asyncio.run(_run_gemini_ingest(source_path, dry_run))
    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(str(e)) from e


async def _run_gemini_ingest(source_path: Path, dry_run: bool) -> None:
    resolved = source_path.expanduser().resolve()
    files = _gemini_discover_files(resolved)
    if not files:
        click.echo(
            click.style(
                f"No indexable GEMINI.md file found at {resolved}",
                fg="yellow",
            )
        )
        return

    slug = _gemini_derive_slug(files[0])
    namespace = _build_namespace(slug, prefix=_GEMINI_NAMESPACE_PREFIX)

    if dry_run:
        click.echo(f"Would ingest {len(files)} file(s) into namespace '{namespace}' (dry-run):")
        for f in files:
            tags = sorted(_gemini_tags_for_file(f))
            click.echo(f"  {f.name}  tags=[{', '.join(tags)}]")
        return

    from memtomem.cli._bootstrap import cli_components

    async with cli_components() as comp:
        summary = await _ingest_files_with_components(
            comp, files, namespace, tag_fn=_gemini_tags_for_file
        )

    click.echo(
        f"Ingested {len(files)} file(s) into '{namespace}': "
        f"{summary.indexed} new, {summary.skipped} unchanged, "
        f"{summary.deleted} deleted."
    )
    for err in summary.errors:
        click.echo(click.style(f"  ERROR: {err}", fg="red"))


def _gemini_discover_files(source: Path) -> list[Path]:
    """Return the GEMINI.md file to index.

    *source* may be a file (the GEMINI.md itself) or a directory
    containing one. Returns a single-element list when found, empty
    list otherwise.
    """
    if source.is_file():
        return [source] if source.suffix == ".md" else []
    # Directory — look for GEMINI.md directly inside.
    candidate = source / "GEMINI.md"
    if candidate.is_file():
        return [candidate]
    return []


def _gemini_derive_slug(source_file: Path) -> str:
    """Extract a namespace slug from the parent directory of *source_file*.

    ``~/.gemini/GEMINI.md`` → ``global``.
    ``/path/to/my-project/GEMINI.md`` → ``my-project``.
    """
    parent_name = source_file.parent.name
    if parent_name in (".gemini", ""):
        return "global"
    return parent_name


def _gemini_tags_for_file(file_path: Path) -> set[str]:
    """Return the tag set for a Gemini memory file.

    Every chunk gets the ``gemini-memory`` source marker.
    """
    return {_GEMINI_BASE_TAG}


# =====================================================================
# mm ingest codex-memory
# =====================================================================


@ingest.command("codex-memory")
@click.option(
    "--source",
    "source_path",
    required=True,
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    help="Path to a Codex memories directory (typically ~/.codex/memories/).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be indexed without writing to storage.",
)
def codex_memory(source_path: Path, dry_run: bool) -> None:
    """Index a Codex CLI memories directory into memtomem.

    Read-only snapshot: the source files stay where they are — memtomem
    records the absolute path as ``source_file`` and indexes the content
    under namespace ``codex-memory:<slug>``. Re-run to pick up new or
    changed files; unchanged files are skipped via content hash.
    """
    try:
        asyncio.run(_run_codex_ingest(source_path, dry_run))
    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(str(e)) from e


async def _run_codex_ingest(source_path: Path, dry_run: bool) -> None:
    resolved = source_path.expanduser().resolve()
    slug = _codex_derive_slug(resolved)
    namespace = _build_namespace(slug, prefix=_CODEX_NAMESPACE_PREFIX)

    files = _codex_discover_files(resolved)
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
            tags = sorted(_codex_tags_for_file(f))
            click.echo(f"  {f.name}  tags=[{', '.join(tags)}]")
        return

    from memtomem.cli._bootstrap import cli_components

    async with cli_components() as comp:
        summary = await _ingest_files_with_components(
            comp, files, namespace, tag_fn=_codex_tags_for_file
        )

    click.echo(
        f"Ingested {len(files)} file(s) into '{namespace}': "
        f"{summary.indexed} new, {summary.skipped} unchanged, "
        f"{summary.deleted} deleted."
    )
    for err in summary.errors:
        click.echo(click.style(f"  ERROR: {err}", fg="red"))


def _codex_discover_files(source_root: Path) -> list[Path]:
    """Return indexable ``.md`` files directly under *source_root*.

    Flat (non-recursive) — mirrors the Claude discovery pattern.
    Sorted for deterministic output. Skips hidden files and README.md.
    """
    files: list[Path] = []
    for f in sorted(source_root.iterdir()):
        if not f.is_file():
            continue
        if f.suffix != ".md":
            continue
        if f.name.startswith("."):
            continue
        if f.name in _CODEX_EXCLUDE_FILENAMES:
            continue
        files.append(f)
    return files


def _codex_derive_slug(source_dir: Path) -> str:
    """Extract a namespace slug from a Codex memories directory path.

    ``~/.codex/memories/`` → ``global``.
    ``/path/to/custom-dir/`` → ``custom-dir``.
    """
    name = source_dir.name
    if name in ("memories", ""):
        parent = source_dir.parent.name
        if parent in (".codex", ""):
            return "global"
        return parent
    return name


def _codex_tags_for_file(file_path: Path) -> set[str]:
    """Return the tag set for a Codex memory file.

    Every chunk gets the ``codex-memory`` source marker.
    """
    return {_CODEX_BASE_TAG}
