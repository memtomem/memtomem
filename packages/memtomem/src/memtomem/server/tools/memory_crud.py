"""Tools: mem_add, mem_edit, mem_delete, mem_batch_add."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register

if TYPE_CHECKING:
    from memtomem.models import IndexingStats

logger = logging.getLogger(__name__)


def _webhook_error_cb(task: "asyncio.Task") -> None:  # noqa: F821
    """Log errors from fire-and-forget webhook tasks."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        logger.warning("Webhook fire failed: %s", exc)


def _validate_path(path_str: str, memory_dirs: list) -> tuple[Path | None, str | None]:
    """Validate and resolve a user-supplied path.

    Relative paths are resolved against the first memory_dir.
    Returns (resolved_path, None) on success, or (None, error_message) on failure.
    """
    raw = Path(path_str).expanduser()
    bases = [Path(d).expanduser().resolve() for d in (memory_dirs or [Path(".")])]

    if raw.is_absolute():
        target = raw.resolve()
    else:
        # Resolve relative paths against the first memory_dir
        target = (bases[0] / raw).resolve()

    if not any(target.is_relative_to(b) for b in bases):
        return None, "Error: path is outside configured memory directories."

    return target, None


async def _mem_add_core(
    content: str,
    title: str | None,
    tags: list[str] | None,
    file: str | None,
    namespace: str | None,
    template: str | None,
    ctx: CtxType,
) -> tuple[str, "IndexingStats | None"]:
    """Core logic for ``mem_add`` — also usable from internal callers that
    need the ``IndexingStats`` (e.g. ``mem_consolidate_apply`` linking new
    summary chunks by id without the old ``recall_chunks(limit=1)`` race).

    Returns:
        Tuple of ``(user_facing_message, stats)``. ``stats`` is ``None``
        for early error returns (empty content, oversized content, template
        failure, invalid path) so callers must tolerate ``None``.
    """
    if not content.strip():
        return ("Error: content cannot be empty.", None)
    if len(content) > 100_000:
        return ("Error: content too large (max 100,000 characters).", None)

    from datetime import datetime, timezone

    from memtomem.tools.memory_writer import append_entry

    app = _get_app(ctx)

    # Apply template if specified
    if template:
        from memtomem.templates import list_templates, render_template

        try:
            content = render_template(template, content, title=title)
            # Template already includes its own heading — don't duplicate
            title = None
        except ValueError as exc:
            return (f"Error: {exc}\n\nAvailable templates:\n{list_templates()}", None)

    mdirs = app.config.indexing.memory_dirs

    if file:
        target, err = _validate_path(file, mdirs)
        if err:
            return (err, None)
    else:
        base = app.config.indexing.memory_dirs[0] if app.config.indexing.memory_dirs else Path(".")
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        target = Path(base).expanduser().resolve() / f"{date_str}.md"

    append_entry(target, content, title=title, tags=tags)

    effective_ns = namespace or app.current_namespace

    # Re-index the whole file via the standard pipeline so the watcher
    # (which also calls index_file) produces identical hashes → no duplicates.
    stats = await app.index_engine.index_file(target, namespace=effective_ns)
    app.search_pipeline.invalidate_cache()

    result = f"Memory added to {target}\n- Chunks indexed: {stats.indexed_chunks}\n- File: {target}"

    # Semantic duplicate check: warn if very similar content already exists
    try:
        if len(content) > 20:
            similar, _ = await app.search_pipeline.search(content, top_k=5)
            dupes = [
                s
                for s in similar
                if s.score >= 0.90 and s.score < 0.9999  # exclude exact self-match
            ]
            if dupes:
                result += "\n\n⚠ Similar memories found:"
                for d in dupes[:3]:
                    preview = d.chunk.content[:80].replace("\n", " ")
                    result += f"\n  - ({d.score:.0%}) {preview}..."
    except Exception:
        logger.warning("Duplicate check after mem_add failed", exc_info=True)

    # Fire webhook
    if app.webhook_manager:
        import asyncio

        task = asyncio.create_task(
            app.webhook_manager.fire("add", {"file": str(target), "chunks_indexed": 1})
        )
        task.add_done_callback(_webhook_error_cb)

    return (result, stats)


@mcp.tool()
@tool_handler
async def mem_add(
    content: str,
    title: str | None = None,
    tags: list[str] | None = None,
    file: str | None = None,
    namespace: str | None = None,
    template: str | None = None,
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Add a new memory entry to a markdown file and immediately index it.

    The entry is appended to the target file (or a new timestamped file is
    created in the first configured memory directory). The file is then
    re-indexed so the entry is immediately searchable.

    Args:
        content: The memory content to store
        title: Optional heading title for the entry
        tags: Optional tags for categorisation
        file: Target .md filename (relative or absolute). If omitted, a
              timestamped file is created in the first memory_dir.
        namespace: Assign indexed chunks to this namespace (default: config default)
        template: Use a built-in template (adr, meeting, debug, decision).
                  Content can be JSON with field values or plain text.
    """
    message, _stats = await _mem_add_core(
        content=content,
        title=title,
        tags=tags,
        file=file,
        namespace=namespace,
        template=template,
        ctx=ctx,
    )
    return message


@mcp.tool()
@tool_handler
@register("crud")
async def mem_edit(
    chunk_id: str,
    new_content: str,
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Edit an existing memory entry in its source markdown file.

    Replaces the chunk's original line range in the file with new_content,
    then re-indexes the file so the change is immediately searchable.

    Args:
        chunk_id: The UUID of the chunk to edit (shown in mem_search results)
        new_content: The replacement content
    """
    if not new_content.strip():
        return "Error: new_content cannot be empty."

    from memtomem.tools.memory_writer import replace_lines

    app = _get_app(ctx)

    try:
        uid = UUID(chunk_id)
    except (ValueError, TypeError):
        return f"Error: invalid chunk ID format: {chunk_id}"

    chunk = await app.storage.get_chunk(uid)
    if chunk is None:
        return f"Error: chunk {chunk_id} not found."

    meta = chunk.metadata
    # Backup for rollback on indexing failure
    original = meta.source_file.read_text(encoding="utf-8")
    try:
        replace_lines(meta.source_file, meta.start_line, meta.end_line, new_content)
        stats = await app.index_engine.index_file(meta.source_file, force=True)
        app.search_pipeline.invalidate_cache()
    except Exception as exc:
        meta.source_file.write_text(original, encoding="utf-8")
        try:
            await app.index_engine.index_file(meta.source_file, force=True)
        except Exception:
            logger.warning("Rollback re-index also failed", exc_info=True)
        app.search_pipeline.invalidate_cache()
        logger.error("mem_edit rollback after indexing failure: %s", exc, exc_info=True)
        return f"Error: edit failed and rolled back: {exc}"

    return (
        f"Memory updated in {meta.source_file}\n"
        f"- Lines {meta.start_line}-{meta.end_line} replaced\n"
        f"- Re-indexed: {stats.indexed_chunks} chunks"
    )


@mcp.tool()
@tool_handler
@register("crud")
async def mem_delete(
    chunk_id: str | None = None,
    source_file: str | None = None,
    namespace: str | None = None,
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Delete memory entries from the index (and optionally from the source file).

    When chunk_id is given, the specific chunk's line range is removed from
    the markdown file and the file is re-indexed.
    When source_file is given, all chunks from that file are removed from the
    index (the file itself is NOT deleted).
    When namespace is given, all chunks in that namespace are removed from the index.

    Args:
        chunk_id: UUID of a specific chunk to delete
        source_file: Path to remove all indexed chunks from
        namespace: Namespace to delete all chunks from
    """
    from memtomem.tools.memory_writer import remove_lines

    app = _get_app(ctx)

    if chunk_id:
        try:
            uid = UUID(chunk_id)
        except (ValueError, TypeError):
            return f"Error: invalid chunk ID format: {chunk_id}"

        chunk = await app.storage.get_chunk(uid)
        if chunk is None:
            return f"Error: chunk {chunk_id} not found."

        meta = chunk.metadata
        # Backup for rollback on indexing failure
        original = meta.source_file.read_text(encoding="utf-8")
        try:
            remove_lines(meta.source_file, meta.start_line, meta.end_line)
            stats = await app.index_engine.index_file(meta.source_file, force=True)
            app.search_pipeline.invalidate_cache()
        except Exception as exc:
            meta.source_file.write_text(original, encoding="utf-8")
            try:
                await app.index_engine.index_file(meta.source_file, force=True)
            except Exception:
                logger.warning("Rollback re-index also failed", exc_info=True)
            app.search_pipeline.invalidate_cache()
            logger.error("mem_delete rollback after indexing failure: %s", exc, exc_info=True)
            return f"Error: delete failed and rolled back: {exc}"
        return (
            f"Memory deleted from {meta.source_file}\n"
            f"- Lines {meta.start_line}-{meta.end_line} removed\n"
            f"- Re-indexed: {stats.indexed_chunks} chunks"
        )

    if source_file:
        sf_path, sf_err = _validate_path(source_file, app.config.indexing.memory_dirs)
        if sf_err:
            return sf_err
        deleted = await app.storage.delete_by_source(sf_path)
        app.search_pipeline.invalidate_cache()
        return f"Removed {deleted} chunks from index for {source_file}"

    if namespace:
        deleted = await app.storage.delete_by_namespace(namespace)
        app.search_pipeline.invalidate_cache()
        return f"Removed {deleted} chunks from namespace '{namespace}'"

    return "Provide chunk_id, source_file, or namespace."


@mcp.tool()
@tool_handler
@register("crud")
async def mem_batch_add(
    entries: list[dict],
    namespace: str | None = None,
    file: str | None = None,
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Add multiple memory entries in one call (KV batch).

    Each entry dict should have "key" (title) and "value" (content), and
    optionally "tags" (list[str]).  All entries are appended to the same file
    and indexed once.

    Args:
        entries: List of {"key": "title", "value": "content", "tags": [...]}
        namespace: Namespace for all entries (default: config default)
        file: Target .md file.  If omitted, a timestamped file is created.
    """
    if len(entries) > 500:
        return "Error: batch too large (max 500 entries)."

    from datetime import datetime, timezone

    from memtomem.tools.memory_writer import append_entry

    app = _get_app(ctx)
    mdirs = app.config.indexing.memory_dirs

    if file:
        target, err = _validate_path(file, mdirs)
        if err:
            return err
    else:
        base = app.config.indexing.memory_dirs[0] if app.config.indexing.memory_dirs else Path(".")
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        target = Path(base).expanduser().resolve() / f"{date_str}.md"

    skipped = 0
    for entry in entries:
        key = entry.get("key") or entry.get("title", "")
        value = entry.get("value") or entry.get("content", "")
        entry_tags = entry.get("tags")
        if not value:
            skipped += 1
            continue
        append_entry(target, value, title=key or None, tags=entry_tags)

    # Collect all tags from entries for post-index application
    all_tags: set[str] = set()
    for entry in entries:
        entry_tags = entry.get("tags")
        if entry_tags:
            all_tags.update(entry_tags)

    effective_ns = namespace or app.current_namespace
    stats = await app.index_engine.index_file(target, namespace=effective_ns)
    app.search_pipeline.invalidate_cache()

    # Apply collected tags to indexed chunks
    if all_tags and stats.indexed_chunks > 0:
        chunks = await app.storage.list_chunks_by_source(target)
        updated = []
        for c in chunks:
            merged = set(c.metadata.tags) | all_tags
            if merged != set(c.metadata.tags):
                c.metadata = c.metadata.__class__(
                    **{
                        **{f: getattr(c.metadata, f) for f in c.metadata.__dataclass_fields__},
                        "tags": tuple(sorted(merged)),
                    }
                )
                updated.append(c)
        if updated:
            await app.storage.upsert_chunks(updated)

    display_ns = effective_ns or app.config.namespace.default_namespace
    result = (
        f"Batch add complete ({len(entries)} entries) → {target}\n"
        f"- Namespace: {display_ns}\n"
        f"- Chunks indexed: {stats.indexed_chunks}"
    )
    if skipped:
        result += f"\n- Skipped: {skipped} entries (empty content)"
    return result
