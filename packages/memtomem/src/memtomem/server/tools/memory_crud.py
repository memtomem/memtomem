"""Tools: mem_add, mem_edit, mem_delete, mem_batch_add."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app_initialized
from memtomem.server.error_handler import tool_handler
from memtomem.server.helpers import _announce_dim_mismatch_once, _check_embedding_mismatch
from memtomem.server.tool_registry import register
from memtomem.server.tools.multi_agent import _resolve_agent_namespace
from memtomem.server.validation import MAX_CONTENT_LENGTH
from memtomem.server.webhooks import webhook_error_cb

if TYPE_CHECKING:
    from memtomem.models import IndexingStats

logger = logging.getLogger(__name__)


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
    force_unsafe: bool = False,
) -> tuple[str, "IndexingStats | None"]:
    """Core logic for ``mem_add`` — also usable from internal callers that
    need the ``IndexingStats`` (e.g. ``mem_consolidate_apply`` linking new
    summary chunks by id without the old ``recall_chunks(limit=1)`` race).

    Returns:
        Tuple of ``(user_facing_message, stats)``. ``stats`` is ``None``
        for early error returns (empty content, oversized content,
        redaction-guard hit without ``force_unsafe``, template failure,
        invalid path) so callers must tolerate ``None``.
    """
    if not content.strip():
        return ("Error: content cannot be empty.", None)
    if len(content) > MAX_CONTENT_LENGTH:
        return ("Error: content too large (max 100,000 characters).", None)

    # Trust-boundary redaction guard — see ``memtomem.privacy`` module
    # docstring for the rationale and the cross-repo sync rule. Runs
    # before any filesystem write so a flagged write leaves no on-disk
    # trace to clean up.
    from memtomem import privacy

    hits = privacy.scan(content)
    if hits:
        if force_unsafe:
            privacy.record("bypassed", "mem_add")
            # Audit trail for forensic correlation. The matched bytes are
            # never logged — only the request shape (counters answer "is
            # bypass happening?"; this line answers "what specifically got
            # through?"). The full chunk content stays in the on-disk
            # markdown file the call is about to write.
            logger.warning(
                "redaction bypass via force_unsafe=True "
                "(tool=mem_add, namespace=%r, file=%r, content_chars=%d, hits=%d)",
                namespace,
                file,
                len(content),
                len(hits),
            )
        else:
            privacy.record("blocked", "mem_add")
            return (
                f"Error: content matches {len(hits)} privacy pattern(s); "
                "write rejected. Retry with force_unsafe=True to bypass "
                "(audit-logged).",
                None,
            )
    else:
        privacy.record("pass", "mem_add")

    from datetime import datetime, timezone

    from memtomem.tools.memory_writer import append_entry

    app = await _get_app_initialized(ctx)

    # Block vector-dependent writes when the server is in degraded mode
    # (see issue #349). Without this gate the subsequent ``index_file``
    # call hits ``upsert_chunks`` and crashes on a missing ``chunks_vec``.
    mismatch_msg = _check_embedding_mismatch(app)
    if mismatch_msg:
        return (mismatch_msg, None)

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

    assert target is not None
    await asyncio.to_thread(append_entry, target, content, title=title, tags=tags)

    effective_ns = namespace or _resolve_agent_namespace(app, None)

    # Re-index the whole file via the standard pipeline so the watcher
    # (which also calls index_file) produces identical hashes → no duplicates.
    stats = await app.index_engine.index_file(target, namespace=effective_ns)
    app.search_pipeline.invalidate_cache()

    display_ns = effective_ns or app.config.namespace.default_namespace
    result = (
        f"Memory added to {target}\n"
        f"- Namespace: {display_ns}\n"
        f"- Chunks indexed: {stats.indexed_chunks}\n"
        f"- File: {target}"
    )

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
        task = asyncio.create_task(
            app.webhook_manager.fire("add", {"file": str(target), "chunks_indexed": 1})
        )
        task.add_done_callback(webhook_error_cb)

    # One-shot dim-mismatch hint — only emitted the first time per MCP session.
    dim_notice = await _announce_dim_mismatch_once(app)
    if dim_notice:
        result += f"\n\n{dim_notice}"

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
    force_unsafe: bool = False,
    ctx: CtxType = None,
) -> str:
    """Add a new memory entry to a markdown file and immediately index it.

    The entry is appended to the target file (or a new timestamped file is
    created in the first configured memory directory). The file is then
    re-indexed so the entry is immediately searchable.

    Content passes through a trust-boundary redaction guard before any
    filesystem write. If the content matches a known secret pattern
    (provider tokens, API keys, PEM headers, etc.) the write is rejected.
    Set ``force_unsafe=True`` to bypass after manual review; bypass events
    are recorded with a ``bypassed`` outcome label so guard effectiveness
    and bypass usage stay observable. See ``mem_add_redaction_stats``.

    The redaction scan covers the first 10,000 characters of ``content``;
    matches beyond that window are not seen by the guard. This is parity
    with the STM compression-side scanner — split very long content into
    multiple calls if every region must be inspected.

    Args:
        content: The memory content to store
        title: Optional heading title for the entry
        tags: Optional tags for categorisation
        file: Target .md filename (relative or absolute). If omitted, a
              timestamped file is created in the first memory_dir.
        namespace: Assign indexed chunks to this namespace (default: config default)
        template: Use a built-in template (adr, meeting, debug, decision,
                  procedure). Content can be JSON with field values or plain text.
        force_unsafe: When True, bypass the redaction guard for this call
                      even when content matches a secret pattern. Use only
                      when matches are known false positives (e.g.,
                      documenting an example credential schema).

    Returns a confirmation message. If highly similar memories already exist
    (≥90% match), a duplicate warning is appended to the output.
    """
    message, _stats = await _mem_add_core(
        content=content,
        title=title,
        tags=tags,
        file=file,
        namespace=namespace,
        template=template,
        force_unsafe=force_unsafe,
        ctx=ctx,
    )
    return message


@mcp.tool()
@tool_handler
@register("crud")
async def mem_edit(
    chunk_id: str,
    new_content: str,
    ctx: CtxType = None,
) -> str:
    """Edit an existing memory entry in its source markdown file.

    ``new_content`` is treated as body-only: the heading line and the
    section-leading ``> created:`` / ``> tags:`` blockquote header are
    preserved automatically. To override the heading explicitly,
    prefix ``new_content`` with ``## `` and the call reverts to a
    full replacement of the chunk's line range.

    Args:
        chunk_id: The UUID of the chunk to edit (shown in mem_search results)
        new_content: The replacement body. Heading + per-entry metadata
            blockquote are preserved unless the value starts with ``## ``.
    """
    if not new_content.strip():
        return "Error: new_content cannot be empty."

    from memtomem.tools.memory_writer import replace_chunk_body

    app = await _get_app_initialized(ctx)
    mismatch_msg = _check_embedding_mismatch(app)
    if mismatch_msg:
        return mismatch_msg

    try:
        uid = UUID(chunk_id)
    except (ValueError, TypeError):
        return f"Error: invalid chunk ID format: {chunk_id}"

    chunk = await app.storage.get_chunk(uid)
    if chunk is None:
        return f"Error: chunk {chunk_id} not found."

    meta = chunk.metadata
    # Backup for rollback on indexing failure
    original = await asyncio.to_thread(meta.source_file.read_text, encoding="utf-8")
    try:
        # ``replace_chunk_body`` preserves the heading + section-leading
        # blockquote header (``> created:`` / ``> tags:``) so that callers
        # supplying body-only ``new_content`` don't accidentally erase the
        # metadata. Pass a content prefixed with ``## `` to override the
        # heading explicitly and bypass preservation.
        await asyncio.to_thread(
            replace_chunk_body, meta.source_file, meta.start_line, meta.end_line, new_content
        )
        stats = await app.index_engine.index_file(meta.source_file, force=True)
        app.search_pipeline.invalidate_cache()
    except Exception as exc:
        await asyncio.to_thread(meta.source_file.write_text, original, encoding="utf-8")
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
    ctx: CtxType = None,
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

    app = await _get_app_initialized(ctx)

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
        original = await asyncio.to_thread(meta.source_file.read_text, encoding="utf-8")
        try:
            await asyncio.to_thread(remove_lines, meta.source_file, meta.start_line, meta.end_line)
            stats = await app.index_engine.index_file(meta.source_file, force=True)
            app.search_pipeline.invalidate_cache()
        except Exception as exc:
            await asyncio.to_thread(meta.source_file.write_text, original, encoding="utf-8")
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
        assert sf_path is not None
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
    force_unsafe: bool = False,
    ctx: CtxType = None,
) -> str:
    """Add multiple memory entries in one call (KV batch).

    Each entry dict should have "key" (title) and "value" (content), and
    optionally "tags" (list[str]).  All entries are appended to the same file
    and indexed once.

    Each entry's content passes through the same trust-boundary redaction
    guard as ``mem_add``. If any entry matches a secret pattern, the whole
    batch is rejected — partial-success on a flagged batch would leak the
    transactional contract callers rely on. Pass ``force_unsafe=True`` to
    bypass for the whole batch (each hit item is recorded with a
    ``bypassed`` outcome label per audit).

    The scan covers only the first 10,000 characters of each entry's
    value; matches beyond that per-entry window are not seen by the
    guard.

    Args:
        entries: List of {"key": "title", "value": "content", "tags": [...]}
        namespace: Namespace for all entries (default: config default)
        file: Target .md file.  If omitted, a timestamped file is created.
        force_unsafe: When True, bypass the redaction guard for any flagged
                      entries. Bypass events are recorded per item.
    """
    if len(entries) > 500:
        return f"Error: batch too large (max 500 entries, got {len(entries)})."

    # Trust-boundary redaction guard. Pre-scan every entry before any
    # filesystem write so a flagged batch leaves no on-disk residue
    # regardless of which entry tripped the pattern. See
    # ``memtomem.privacy`` for the cross-repo sync rule.
    from memtomem import privacy

    hit_indices: list[int] = []
    for idx, entry in enumerate(entries):
        value = entry.get("value") or entry.get("content", "")
        if not value:
            continue
        if privacy.scan(value):
            hit_indices.append(idx)

    if hit_indices and not force_unsafe:
        for _ in hit_indices:
            privacy.record("blocked", "mem_batch_add")
        return (
            f"Error: items at indices {hit_indices} match privacy patterns; "
            "whole batch rejected. Resubmit with hit items removed, or pass "
            "force_unsafe=True to bypass (audit-logged)."
        )

    hit_set = set(hit_indices)
    for idx, entry in enumerate(entries):
        value = entry.get("value") or entry.get("content", "")
        if not value:
            continue
        if idx in hit_set:
            privacy.record("bypassed", "mem_batch_add")
            logger.warning(
                "redaction bypass via force_unsafe=True "
                "(tool=mem_batch_add, namespace=%r, file=%r, item_idx=%d, content_chars=%d)",
                namespace,
                file,
                idx,
                len(value),
            )
        else:
            privacy.record("pass", "mem_batch_add")

    from datetime import datetime, timezone

    from memtomem.tools.memory_writer import append_entry

    app = await _get_app_initialized(ctx)
    mismatch_msg = _check_embedding_mismatch(app)
    if mismatch_msg:
        return mismatch_msg
    mdirs = app.config.indexing.memory_dirs

    if file:
        target, err = _validate_path(file, mdirs)
        if err:
            return err
    else:
        base = app.config.indexing.memory_dirs[0] if app.config.indexing.memory_dirs else Path(".")
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        target = Path(base).expanduser().resolve() / f"{date_str}.md"

    assert target is not None
    skipped = 0
    for entry in entries:
        key = entry.get("key") or entry.get("title", "")
        value = entry.get("value") or entry.get("content", "")
        entry_tags = entry.get("tags")
        if not value:
            skipped += 1
            continue
        append_entry(target, value, title=key or None, tags=entry_tags)

    effective_ns = namespace or _resolve_agent_namespace(app, None)
    stats = await app.index_engine.index_file(target, namespace=effective_ns)
    app.search_pipeline.invalidate_cache()

    display_ns = effective_ns or app.config.namespace.default_namespace
    result = (
        f"Batch add complete ({len(entries)} entries) → {target}\n"
        f"- Namespace: {display_ns}\n"
        f"- Chunks indexed: {stats.indexed_chunks}"
    )
    if skipped:
        result += f"\n- Skipped: {skipped} entries (empty content)"
    return result


@mcp.tool()
@tool_handler
@register("crud")
async def mem_add_redaction_stats(
    ctx: CtxType = None,
) -> str:
    """Return a JSON snapshot of redaction-guard outcomes since process start.

    Outcome labels:
        blocked  — write rejected because content matched a privacy pattern.
        pass     — write proceeded; content matched no patterns.
        bypassed — write proceeded with ``force_unsafe=True`` despite a match.

    The ``by_tool`` map breaks the same outcomes down by ingress tool
    (``mem_add``, ``mem_batch_add``).

    Counts reflect attempted *write outcomes*, not raw scans. A rejected
    ``mem_batch_add`` records ``blocked`` once per hit item but does not
    record ``pass`` for the clean siblings in the same rejected batch
    (no write occurred for them). Summing
    ``blocked + pass + bypassed`` therefore equals the count of actual
    or attempted writes that reached the guard, not the total number
    of entries inspected.
    """
    import json

    from memtomem import privacy

    return json.dumps(privacy.snapshot(), indent=2)
