"""Tool: mem_index."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app_initialized
from memtomem.server.error_handler import tool_handler
from memtomem.server.helpers import _check_embedding_mismatch
from memtomem.server.tools._provenance import (
    capture_session_and_namespace_split,
    record_write_provenance,
)


def _partition_index_errors(
    errors: Sequence[str], retryable_errors: Sequence[str]
) -> tuple[list[str], list[str]]:
    """Split errors by the retryable same-string subset, preserving order.

    Depends on the producer invariant that every ``retryable_errors`` entry is
    byte-identical to one already in ``errors`` (``IndexEngine`` appends the
    same ``f"{path.name}: {exc}"`` string to both lists, deduping each with
    ``dict.fromkeys``). Partitioning ``errors`` — rather than concatenating the
    two lists — is what keeps a retryable failure reported exactly once; the
    cost is that a producer which ever emits a retryable string absent from
    ``errors`` would drop it silently here.
    """
    retryable_set = set(retryable_errors)
    return (
        [error for error in errors if error not in retryable_set],
        [error for error in errors if error in retryable_set],
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
        provenance_session_id, caller_ns, session_ns = await capture_session_and_namespace_split(
            app, namespace
        )

        # The two namespaces travel in different slots on purpose (#2104).
        # ``caller_ns`` is what this call named: explicit intent, and it wins
        # everywhere. ``session_ns`` is ambient — an agent session or a
        # ``mem_ns_set`` current namespace — so it binds only sources with no
        # stored rows. Passing it as the explicit namespace, as this tool used
        # to, made ``mem_index(force=true)`` under a session restamp every file
        # it re-embedded, including another agent's.
        #
        # ``force`` re-embeds; it does not re-resolve namespaces (#2061), so
        # a file keeps the namespace its chunks are stored under unless
        # ``caller_ns`` names one. Applying changed namespace rules to
        # already-indexed files is ``mm index --reassign-namespaces``, which
        # stays CLI-only — the core tool descriptions are at their character
        # budget (``test_core_tool_descriptions``), so a parameter here would
        # cost more than the rare operation is worth. The preservation
        # advisory below tells a caller when that command is the one they
        # want.
        stats = await app.index_engine.index_path(
            target,
            recursive=recursive,
            force=force,
            namespace=caller_ns,
            new_source_namespace=session_ns,
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

    errors, retryable_errors = _partition_index_errors(stats.errors, stats.retryable_errors)

    if stats.errors and stats.total_files == 0:
        lines: list[str] = []
        if errors:
            lines.append("Error: " + "; ".join(errors))
        if retryable_errors:
            lines.append("Error (retryable): " + "; ".join(retryable_errors))
            lines.append("Retry: Call mem_index again once the chunk store is reachable.")
        return "\n".join(lines)

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
    if stats.namespaces_preserved_against_rules:
        # The advisory has to be built by hand here: this result is a string,
        # not a serialized ``IndexingStats``, so a new field reaches this
        # surface only if it is rendered (#2061).
        result += (
            f"\n- Namespaces preserved: {stats.namespaces_preserved_against_rules} file(s) "
            "kept their stored namespace; current path rules would assign differently. "
            "Apply the rules with `mm index --reassign-namespaces <path>`."
        )
    if stats.chunks_missing_vectors:
        # Hand-built for the same reason as the advisory above. The remedy
        # names the CLI because a re-embed of a whole tree is a long,
        # interruptible job better run from a shell than from a tool call
        # that has to hold a client's turn open. Both paths preserve stored
        # namespaces now (#2104), so the choice is ergonomic, not a safety
        # caveat.
        result += (
            f"\n- No embedding: {stats.chunks_missing_vectors} unchanged chunk(s) "
            "have no vector, so dense search will not find them. Re-embed with "
            "`mm index --force <path>` (CLI)."
        )
    if errors:
        result += "\n- Errors:\n" + "\n".join(f"    {error}" for error in errors)
    if retryable_errors:
        result += "\n- Errors (retryable):\n" + "\n".join(
            f"    {error}" for error in retryable_errors
        )
        result += "\n- Retry: Call mem_index again once the chunk store is reachable."
    if stats.blocked_files:
        # ADR-0006 PR-A: name the skipped files so an operator can review them.
        result += "\n- Blocked files:\n" + "\n".join(f"    {p}" for p in stats.blocked_paths)
        if stats.blocked_project_shared_files:
            result += (
                f"\n- {stats.blocked_project_shared_files} of these are project_shared"
                " (hard-refused; force_unsafe does not apply)."
            )

    if stats.exempted_files:
        # #2076: hand-built like the advisories above — this surface renders a
        # string, so a new stat reaches an agent only by being written here.
        # ``mem_index`` has no ``force_unsafe`` parameter, so a declared
        # exemption is the only way a pattern-documenting note indexes over
        # MCP at all; naming the files is what keeps that from being silent.
        result += (
            f"\n- Declared redaction exemption: {stats.exempted_files} file(s) admitted by "
            "their own frontmatter `redaction: documents-patterns` (audit-logged):\n"
            + "\n".join(f"    {p}" for p in stats.exempted_paths)
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
