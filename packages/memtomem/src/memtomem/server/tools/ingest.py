"""Tool: mem_ingest — ingest external agent memories via mem_do."""

from __future__ import annotations

from pathlib import Path

from memtomem.server.context import CtxType, _get_app_initialized
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register

_VALID_SOURCE_TYPES = frozenset({"claude", "gemini", "codex"})


@tool_handler
@register("ingest")
async def mem_ingest(
    source: str,
    source_type: str = "claude",
    dry_run: bool = False,
    ctx: CtxType = None,
) -> str:
    """Ingest external agent memories (Claude, Gemini, Codex) into memtomem.

    Indexes markdown files from the specified source directory under a
    namespaced scope (e.g. ``claude-memory:<slug>``).  For Claude, passing
    a parent directory (e.g. ``~/.claude/projects/``) auto-discovers all
    ``<slug>/memory/`` subdirectories.

    Args:
        source: Path to memory directory or parent for multi-slug discovery.
        source_type: Agent type — ``claude``, ``gemini``, or ``codex``.
        dry_run: If True, report what would be indexed without writing.
    """
    if source_type not in _VALID_SOURCE_TYPES:
        return f"Error: unknown source_type '{source_type}'. Valid: {sorted(_VALID_SOURCE_TYPES)}"

    resolved = Path(source).expanduser().resolve()
    if not resolved.exists():
        return f"Error: path not found: {resolved}"

    if source_type == "claude":
        return await _ingest_claude(resolved, dry_run, ctx)
    if source_type == "gemini":
        return await _ingest_gemini(resolved, dry_run, ctx)
    return await _ingest_codex(resolved, dry_run, ctx)


async def _ingest_claude(resolved: Path, dry_run: bool, ctx: CtxType) -> str:
    from memtomem.cli.ingest_cmd import (
        _build_namespace,
        _derive_slug,
        _discover_claude_slug_dirs,
        _discover_files,
        _ingest_files_with_components,
        _tags_for_file,
    )

    # Single-slug fast path.
    files = _discover_files(resolved)
    if files:
        return await _ingest_single(
            resolved,
            files,
            dry_run,
            ctx,
            derive_slug=_derive_slug,
            build_ns=_build_namespace,
            tag_fn=_tags_for_file,
            ingest_fn=_ingest_files_with_components,
        )

    # Multi-slug discovery.
    slug_dirs = _discover_claude_slug_dirs(resolved)
    if not slug_dirs:
        return f"No indexable markdown files found in {resolved}"

    if dry_run:
        lines = [f"Discovered {len(slug_dirs)} slug(s) (dry-run):"]
        for mem_dir in slug_dirs:
            slug = _derive_slug(mem_dir)
            ns = _build_namespace(slug)
            dir_files = _discover_files(mem_dir)
            lines.append(f"\n  {ns} ({len(dir_files)} file(s)):")
            for f in dir_files:
                tags = sorted(_tags_for_file(f))
                lines.append(f"    {f.name}  tags=[{', '.join(tags)}]")
        return "\n".join(lines)

    app = await _get_app_initialized(ctx)
    total_indexed = 0
    total_skipped = 0
    total_deleted = 0
    errors: list[str] = []
    slug_lines: list[str] = []

    for mem_dir in slug_dirs:
        slug = _derive_slug(mem_dir)
        ns = _build_namespace(slug)
        dir_files = _discover_files(mem_dir)
        if not dir_files:
            continue
        summary = await _ingest_files_with_components(
            app,
            dir_files,
            ns,
            tag_fn=_tags_for_file,  # type: ignore[arg-type]
        )
        total_indexed += summary.indexed
        total_skipped += summary.skipped
        total_deleted += summary.deleted
        errors.extend(summary.errors)
        slug_lines.append(
            f"  {ns}: {summary.indexed} new, {summary.skipped} unchanged, {summary.deleted} deleted"
        )

    lines = slug_lines + [
        f"\nTotal across {len(slug_dirs)} slug(s): "
        f"{total_indexed} new, {total_skipped} unchanged, "
        f"{total_deleted} deleted."
    ]
    for err in errors:
        lines.append(f"  ERROR: {err}")
    return "\n".join(lines)


async def _ingest_gemini(resolved: Path, dry_run: bool, ctx: CtxType) -> str:
    from memtomem.cli.ingest_cmd import (
        _GEMINI_NAMESPACE_PREFIX,
        _build_namespace,
        _gemini_derive_slug,
        _gemini_discover_files,
        _gemini_tags_for_file,
        _ingest_files_with_components,
    )

    files = _gemini_discover_files(resolved)
    if not files:
        return f"No indexable GEMINI.md file found at {resolved}"

    slug = _gemini_derive_slug(files[0])
    ns = _build_namespace(slug, prefix=_GEMINI_NAMESPACE_PREFIX)

    if dry_run:
        lines = [f"Would ingest {len(files)} file(s) into namespace '{ns}' (dry-run):"]
        for f in files:
            tags = sorted(_gemini_tags_for_file(f))
            lines.append(f"  {f.name}  tags=[{', '.join(tags)}]")
        return "\n".join(lines)

    app = await _get_app_initialized(ctx)
    summary = await _ingest_files_with_components(
        app,
        files,
        ns,
        tag_fn=_gemini_tags_for_file,  # type: ignore[arg-type]
    )
    result = (
        f"Ingested {len(files)} file(s) into '{ns}': "
        f"{summary.indexed} new, {summary.skipped} unchanged, "
        f"{summary.deleted} deleted."
    )
    for err in summary.errors:
        result += f"\n  ERROR: {err}"
    return result


async def _ingest_codex(resolved: Path, dry_run: bool, ctx: CtxType) -> str:
    from memtomem.cli.ingest_cmd import (
        _CODEX_NAMESPACE_PREFIX,
        _build_namespace,
        _codex_derive_slug,
        _codex_discover_files,
        _codex_tags_for_file,
        _ingest_files_with_components,
    )

    files = _codex_discover_files(resolved)
    if not files:
        return f"No indexable markdown files found in {resolved}"

    slug = _codex_derive_slug(resolved)
    ns = _build_namespace(slug, prefix=_CODEX_NAMESPACE_PREFIX)

    if dry_run:
        lines = [f"Would ingest {len(files)} file(s) into namespace '{ns}' (dry-run):"]
        for f in files:
            tags = sorted(_codex_tags_for_file(f))
            lines.append(f"  {f.name}  tags=[{', '.join(tags)}]")
        return "\n".join(lines)

    app = await _get_app_initialized(ctx)
    summary = await _ingest_files_with_components(
        app,
        files,
        ns,
        tag_fn=_codex_tags_for_file,  # type: ignore[arg-type]
    )
    result = (
        f"Ingested {len(files)} file(s) into '{ns}': "
        f"{summary.indexed} new, {summary.skipped} unchanged, "
        f"{summary.deleted} deleted."
    )
    for err in summary.errors:
        result += f"\n  ERROR: {err}"
    return result


async def _ingest_single(
    mem_dir: Path,
    files: list[Path],
    dry_run: bool,
    ctx: CtxType,
    *,
    derive_slug,
    build_ns,
    tag_fn,
    ingest_fn,
) -> str:
    """Shared single-directory ingest for any source type."""
    slug = derive_slug(mem_dir)
    ns = build_ns(slug)

    if dry_run:
        lines = [f"Would ingest {len(files)} file(s) into namespace '{ns}' (dry-run):"]
        for f in files:
            tags = sorted(tag_fn(f))
            lines.append(f"  {f.name}  tags=[{', '.join(tags)}]")
        return "\n".join(lines)

    app = await _get_app_initialized(ctx)
    summary = await ingest_fn(app, files, ns, tag_fn=tag_fn)  # type: ignore[arg-type]
    result = (
        f"Ingested {len(files)} file(s) into '{ns}': "
        f"{summary.indexed} new, {summary.skipped} unchanged, "
        f"{summary.deleted} deleted."
    )
    for err in summary.errors:
        result += f"\n  ERROR: {err}"
    return result
