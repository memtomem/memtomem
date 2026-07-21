"""Tool: mem_index."""

from __future__ import annotations

from pathlib import Path

from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app_initialized
from memtomem.server.error_handler import tool_handler
from memtomem.server.helpers import _check_embedding_mismatch
from memtomem.server.tools._provenance import (
    capture_session_and_namespace,
    record_write_provenance,
)


@mcp.tool()
@tool_handler
async def mem_index(
    path: str = ".",
    recursive: bool = True,
    force: bool = False,
    namespace: str | None = None,
    auto_tag: bool = False,
    ctx: CtxType = None,
) -> str:
    """Index or re-index markdown files for hybrid search.

    Args:
        path: File or directory path to index
        recursive: Whether to recurse into subdirectories (default True)
        force: If True, re-index all files even if unchanged (default False)
        namespace: Assign all indexed chunks to this namespace
        auto_tag: If True, run keyword-based auto-tagging on newly indexed chunks
    """
    app = await _get_app_initialized(ctx)

    # Block indexing if embedding config mismatches DB
    mismatch_msg = _check_embedding_mismatch(app)
    if mismatch_msg:
        return mismatch_msg

    target = Path(path).expanduser().resolve()

    # The gauge spans capture -> index -> provenance event. Indexing a
    # large tree can outlast the session-teardown drain budget, in which
    # case ``mem_session_end`` reports that writes were still in flight
    # rather than presenting a short event count as complete.
    async with app.write_in_flight():
        # Session id and namespace in one ``_session_lock`` acquisition:
        # split, a transition between them files the chunks and their
        # provenance under different sessions.
        provenance_session_id, effective_ns = await capture_session_and_namespace(app, namespace)

        stats = await app.index_engine.index_path(
            target,
            recursive=recursive,
            force=force,
            namespace=effective_ns,
            path_scope="explicit",
        )

        # No-ops on its own when nothing new was written, which covers the
        # zero-file and unchanged-re-index paths below.
        await record_write_provenance(
            app,
            session_id=provenance_session_id,
            event_type="index",
            stats=stats,
        )

    if stats.errors and stats.total_files == 0:
        return "Error: " + "; ".join(stats.errors)

    if stats.total_files == 0:
        return (
            "Indexing complete: no indexable files found\n"
            f"- Path: {target}\n"
            "- Root registration: unchanged (one-shot index)"
        )

    result = (
        f"Indexing complete:\n"
        f"- Files scanned: {stats.total_files}\n"
        f"- Total chunks: {stats.total_chunks}\n"
        f"- Indexed: {stats.indexed_chunks}\n"
        f"- Skipped (unchanged): {stats.skipped_chunks}\n"
        f"- Deleted (stale): {stats.deleted_chunks}\n"
        f"- Blocked (redaction): {stats.blocked_files}\n"
        f"- Duration: {stats.duration_ms:.0f}ms"
    )
    if not app.index_engine._is_within_memory_dirs(target):
        result += "\n- Root registration: unchanged (one-shot index)"
    if stats.errors:
        result += "\n- Errors:\n" + "\n".join(f"    {e}" for e in stats.errors)
    if stats.blocked_files:
        # ADR-0006 PR-A: name the skipped files so an operator can review them.
        result += "\n- Blocked files:\n" + "\n".join(f"    {p}" for p in stats.blocked_paths)
        if stats.blocked_project_shared_files:
            result += (
                f"\n- {stats.blocked_project_shared_files} of these are project_shared"
                " (hard-refused; force_unsafe does not apply)."
            )

    if auto_tag and stats.indexed_chunks > 0:
        from memtomem.tools.auto_tag import auto_tag_storage

        tagged = await auto_tag_storage(
            app.storage,
            source_filter=str(target) if target.is_file() else None,
            max_tags=5,
        )
        result += f"\n- Auto-tagged: {tagged} chunks"

    return result
