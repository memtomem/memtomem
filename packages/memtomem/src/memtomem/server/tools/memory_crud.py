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


def _validate_path(
    path_str: str,
    memory_dirs: list,
    project_memory_dirs: list | None = None,
) -> tuple[Path | None, str | None]:
    """Validate and resolve a user-supplied path.

    Relative paths are resolved against the first memory_dir.
    Absolute paths must live under one of ``memory_dirs`` or
    ``project_memory_dirs`` (ADR-0011: project-tier writes need the
    project's ``.memtomem/memories[.local]`` entries to count as
    valid bases).
    Returns (resolved_path, None) on success, or (None, error_message) on failure.
    """
    raw = Path(path_str).expanduser()
    user_bases = [Path(d).expanduser().resolve() for d in (memory_dirs or [Path(".")])]
    project_bases = [Path(d).expanduser().resolve() for d in (project_memory_dirs or [])]
    bases = user_bases + project_bases

    if raw.is_absolute():
        target = raw.resolve()
    else:
        # Resolve relative paths against the first user-tier memory_dir.
        # Project-tier roots are absolute-only; mixing them in here would
        # surprise existing callers that pass plain filenames.
        target = (user_bases[0] / raw).resolve()

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
    scope: str = "user",
    confirm_project_shared: bool = False,
) -> tuple[str, "IndexingStats | None"]:
    """Core logic for ``mem_add`` — also usable from internal callers that
    need the ``IndexingStats`` (e.g. ``mem_consolidate_apply`` linking new
    summary chunks by id without the old ``recall_chunks(limit=1)`` race).

    ``scope`` is ADR-0011 Gate B: passing anything other than ``"user"``
    requires explicit caller intent. ``project_shared`` additionally
    requires ``confirm_project_shared=True`` (the gate-B confirm
    surrogate for MCP callers; the CLI uses an interactive prompt).

    Returns:
        Tuple of ``(user_facing_message, stats)``. ``stats`` is ``None``
        for early error returns (empty content, oversized content,
        redaction-guard hit without ``force_unsafe``, template failure,
        invalid path, missing project_shared confirm) so callers must
        tolerate ``None``.
    """
    if not content.strip():
        return ("Error: content cannot be empty.", None)
    if len(content) > MAX_CONTENT_LENGTH:
        return ("Error: content too large (max 100,000 characters).", None)

    from datetime import datetime, timezone

    from memtomem import privacy
    from memtomem.config import classify_scope
    from memtomem.tools.memory_writer import append_entry

    app = await _get_app_initialized(ctx)

    # Block vector-dependent writes when the server is in degraded mode
    # (see issue #349). Without this gate the subsequent ``index_file``
    # call hits ``upsert_chunks`` and crashes on a missing ``chunks_vec``.
    mismatch_msg = _check_embedding_mismatch(app)
    if mismatch_msg:
        return (mismatch_msg, None)

    mdirs = app.config.indexing.memory_dirs
    pmdirs = app.config.indexing.project_memory_dirs

    # ADR-0011: derive the *effective* scope before running the gates.
    # When the caller passes a ``file=`` path and that path lands in a
    # registered project tier directory, the indexer will tag the
    # resulting chunks with that project tier — so Gate A/B must see
    # the same tier the chunks will end up in. A caller leaving scope
    # at its default ``user`` while pointing ``file=`` at a project-
    # shared path would otherwise bypass Gate B (no confirm required)
    # and Gate A (force_unsafe=True still allowed). Mirrors the
    # ``mem_edit`` / ``mem_delete`` inferred-scope contract.
    target: Path | None = None
    if file:
        target, err = _validate_path(file, mdirs, pmdirs)
        if err:
            return (err, None)
        assert target is not None
        inferred_scope, inferred_root = classify_scope(target, pmdirs)
        effective_scope = inferred_scope
        effective_project_root: Path | None = inferred_root
    else:
        effective_scope = scope
        effective_project_root = None

    # Gate B (surface layer). project_shared writes go to git; require
    # explicit confirm so MCP callers cannot silently commit PII to a
    # tracked tier through a default-bool oversight.
    if effective_scope == "project_shared" and not confirm_project_shared:
        hint = (
            ""
            if effective_scope == scope
            else " (scope inferred from file= path; the target directory is "
            f"a registered project_shared tier under {effective_project_root})"
        )
        return (
            "Error: scope='project_shared' writes to a git-tracked "
            f"directory. Pass confirm_project_shared=True to proceed.{hint}",
            None,
        )

    # Gate A (chokepoint). enforce_write_guard hard-refuses
    # ``force_unsafe=True`` when scope=='project_shared'.
    guard = privacy.enforce_write_guard(
        content,
        surface="mem_add",
        force_unsafe=force_unsafe,
        scope=effective_scope,
        audit_context={
            "namespace": namespace,
            "file": file,
            "scope": effective_scope,
            "scope_inferred_from_path": effective_scope != scope,
        },
    )
    if guard.decision == "blocked":
        return (
            f"Error: content matches {len(guard.hits)} privacy pattern(s); "
            "write rejected. Retry with force_unsafe=True to bypass "
            "(audit-logged).",
            None,
        )
    if guard.decision == "blocked_project_shared":
        return (
            f"Error: content matches {len(guard.hits)} privacy pattern(s) "
            "and force_unsafe=True is not permitted on scope='project_shared' "
            "(git history is forever). Retry with scope='project_local' "
            "or scope='user' to bypass; manually edit the canonical file "
            "if a project_shared write is required.",
            None,
        )

    # Apply template if specified
    if template:
        from memtomem.templates import list_templates, render_template

        try:
            content = render_template(template, content, title=title)
            # Template already includes its own heading — don't duplicate
            title = None
        except ValueError as exc:
            return (f"Error: {exc}\n\nAvailable templates:\n{list_templates()}", None)

    if target is None:
        # ADR-0011: route the default-dated file to the canonical
        # directory for the requested scope. Without this branch, MCP
        # ``mem_add(scope='project_shared')`` would still write to the
        # user-tier path even though the gate accepted the call — the
        # CLI/MCP divergence flagged in PR-D review.
        from memtomem.memory_scope import (
            MemoryScopeError,
            is_project_tier_registered,
            project_tier_registration_error,
            resolve_memory_scope_dir,
        )
        from memtomem.server.tools.search import _resolve_project_context_root

        if effective_scope == "user":
            base = mdirs[0] if mdirs else Path(".")
            base = Path(base).expanduser().resolve()
        else:
            project_root = _resolve_project_context_root(app)
            try:
                base = resolve_memory_scope_dir(
                    effective_scope, project_root, user_base=Path(mdirs[0])
                )
            except MemoryScopeError as exc:
                return (f"Error: {exc}", None)
            # ADR-0011: refuse if the resolved tier directory is not
            # registered — otherwise the row's scope flips to project
            # but the read surface / watcher cannot see it. Mirrors
            # the ``mm context memory-migrate`` registration guard.
            if not is_project_tier_registered(base, pmdirs):
                return (
                    f"Error: {project_tier_registration_error(base, effective_scope)}",
                    None,
                )
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        target = base / f"{date_str}.md"

    assert target is not None
    target.parent.mkdir(parents=True, exist_ok=True)
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
    scope: str = "user",
    confirm_project_shared: bool = False,
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

    The redaction scan covers the entire ``content`` regardless of length —
    a secret pasted past any byte offset still triggers the guard. The
    asymmetry with STM's compression-side scanner is intentional: STM's
    window is a routing signal, while the LTM scan is the write-rejection
    gate at the trust boundary.

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
        scope=scope,
        confirm_project_shared=confirm_project_shared,
        ctx=ctx,
    )
    return message


@mcp.tool()
@tool_handler
@register("crud")
async def mem_edit(
    chunk_id: str,
    new_content: str,
    force_unsafe: bool = False,
    ctx: CtxType = None,
) -> str:
    """Edit an existing memory entry in its source markdown file.

    ``new_content`` is treated as body-only: the heading line and the
    section-leading ``> created:`` / ``> tags:`` blockquote header are
    preserved automatically. To override the heading explicitly,
    prefix ``new_content`` with ``## `` and the call reverts to a
    full replacement of the chunk's line range.

    ``new_content`` passes through the same trust-boundary redaction
    guard as ``mem_add``. A match rejects the edit unless
    ``force_unsafe=True``; bypass events are audit-logged. See
    ``mem_add_redaction_stats`` for the counter snapshot.

    ADR-0011: the gate's scope is **inferred from the loaded chunk**,
    not from a caller parameter. Editing a chunk whose persisted
    ``scope == 'project_shared'`` enforces the same hard-refusal of
    ``force_unsafe=True`` that applies to ``mem_add(scope='project_shared',
    ...)`` — a client cannot bypass Gate A by omitting an explicit
    scope kwarg on the edit path.

    Args:
        chunk_id: The UUID of the chunk to edit (shown in mem_search results)
        new_content: The replacement body. Heading + per-entry metadata
            blockquote are preserved unless the value starts with ``## ``.
        force_unsafe: When True, bypass the redaction guard for this call
            even when ``new_content`` matches a secret pattern. Use only
            when matches are known false positives.
    """
    if not new_content.strip():
        return "Error: new_content cannot be empty."

    from memtomem import privacy
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

    # ADR-0011: infer scope from the loaded chunk's persisted metadata.
    # The privacy gate sees the same scope the chunk lives under, so
    # editing a project_shared chunk gets the project_shared refusal
    # rule even when the caller did not pass an explicit scope kwarg.
    inferred_scope = meta.scope or "user"
    guard = privacy.enforce_write_guard(
        new_content,
        surface="mem_edit",
        force_unsafe=force_unsafe,
        scope=inferred_scope,
        audit_context={"chunk_id": chunk_id, "scope": inferred_scope},
    )
    if guard.decision == "blocked":
        return (
            f"Error: new_content matches {len(guard.hits)} privacy pattern(s); "
            "edit rejected. Retry with force_unsafe=True to bypass (audit-logged)."
        )
    if guard.decision == "blocked_project_shared":
        return (
            f"Error: new_content matches {len(guard.hits)} privacy pattern(s) "
            "and force_unsafe=True is not permitted on scope='project_shared' "
            "chunks (git history is forever). Move the chunk to a different "
            "scope first, or hand-edit the canonical file with explicit review."
        )
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
    confirm_project_shared: bool = False,
    ctx: CtxType = None,
) -> str:
    """Delete memory entries from the index (and optionally from the source file).

    When chunk_id is given, the specific chunk's line range is removed from
    the markdown file and the file is re-indexed.
    When source_file is given, all chunks from that file are removed from the
    index (the file itself is NOT deleted).
    When namespace is given, all chunks in that namespace are removed from the index.
    ADR-0011: deleting project_shared chunks requires
    ``confirm_project_shared=True``. Bulk source deletes are all-or-nothing:
    if any matched chunk is project_shared, the whole source delete is rejected.

    Args:
        chunk_id: UUID of a specific chunk to delete
        source_file: Path to remove all indexed chunks from
        namespace: Namespace to delete all chunks from
        confirm_project_shared: Required for project_shared chunks
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
        inferred_scope = meta.scope or "user"
        if inferred_scope == "project_shared" and not confirm_project_shared:
            logger.info(
                "mem_delete rejected project_shared chunk without confirmation",
                extra={"chunk_id": chunk_id, "scope": inferred_scope},
            )
            return (
                "Error: deleting scope='project_shared' chunks requires "
                "confirm_project_shared=True."
            )
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
        sf_path, sf_err = _validate_path(
            source_file,
            app.config.indexing.memory_dirs,
            app.config.indexing.project_memory_dirs,
        )
        if sf_err:
            return sf_err
        assert sf_path is not None
        scopes = await app.storage.list_scopes_by_source(sf_path)
        if "project_shared" in scopes and not confirm_project_shared:
            logger.info(
                "mem_delete rejected bulk project_shared source without confirmation",
                extra={"source_file": str(sf_path), "scopes": sorted(scopes)},
            )
            return (
                "Error: source_file delete would remove scope='project_shared' chunks; "
                "pass confirm_project_shared=True to proceed. Bulk source deletes are "
                "all-or-nothing; use chunk_id for per-chunk control."
            )
        deleted = await app.storage.delete_by_source(sf_path)
        app.search_pipeline.invalidate_cache()
        return f"Removed {deleted} chunks from index for {source_file}"

    if namespace:
        # ADR-0011 PR-D Gate B on bulk namespace delete. project_shared
        # memories can sit in the default namespace alongside user
        # memories, so the namespace string alone does not imply the
        # trust tier — probe the persisted scope set first.
        ns_scopes = await app.storage.list_scopes_by_namespace(namespace)
        if "project_shared" in ns_scopes and not confirm_project_shared:
            logger.info(
                "mem_delete rejected bulk project_shared namespace without confirmation",
                extra={"namespace": namespace, "scopes": sorted(ns_scopes)},
            )
            return (
                f"Error: namespace='{namespace}' delete would remove "
                "scope='project_shared' chunks; pass confirm_project_shared=True "
                "to proceed. Bulk namespace deletes are all-or-nothing; use "
                "chunk_id for per-chunk control."
            )
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
    scope: str = "user",
    confirm_project_shared: bool = False,
    ctx: CtxType = None,
) -> str:
    """Add multiple memory entries in one call (KV batch).

    Each entry dict should have "key" (title) and "value" (content), and
    optionally "tags" (list[str]).  All entries are appended to the same file
    and indexed once.

    Each entry's content passes through the same trust-boundary redaction
    guard as ``mem_add`` — routed through ``enforce_write_guard`` per
    entry (ADR-0011 PR-D refactor of the earlier inline-scan path) so
    the project_shared hard refusal is unbypassable on the batch path.
    If any entry matches a secret pattern, the whole batch is rejected
    — partial-success on a flagged batch would leak the transactional
    contract callers rely on. Pass ``force_unsafe=True`` to bypass for
    the whole batch (each hit item is recorded with a ``bypassed``
    outcome label per audit). When ``scope='project_shared'``,
    ``force_unsafe=True`` is hard-refused regardless: git history is
    forever (ADR-0011 §5).

    Each entry's full value is scanned regardless of length — the scan
    no longer truncates at a fixed window, so a secret embedded past any
    byte offset still trips the guard.

    Args:
        entries: List of {"key": "title", "value": "content", "tags": [...]}
        namespace: Namespace for all entries (default: config default)
        file: Target .md file.  If omitted, a timestamped file is created.
        force_unsafe: When True, bypass the redaction guard for any flagged
                      entries. Bypass events are recorded per item.
        scope: ADR-0011 scope axis (``user`` / ``project_shared`` /
               ``project_local``). Applies to every entry in the batch.
        confirm_project_shared: Required when ``scope='project_shared'``
                                — Gate B explicit opt-in for git-tracked writes.
    """
    if len(entries) > 500:
        return f"Error: batch too large (max 500 entries, got {len(entries)})."

    from datetime import datetime, timezone

    from memtomem import privacy
    from memtomem.config import classify_scope
    from memtomem.tools.memory_writer import append_entry

    app = await _get_app_initialized(ctx)
    mismatch_msg = _check_embedding_mismatch(app)
    if mismatch_msg:
        return mismatch_msg
    mdirs = app.config.indexing.memory_dirs
    pmdirs = app.config.indexing.project_memory_dirs

    # ADR-0011: derive the *effective* scope before running Gate A/B.
    # When the caller passes a ``file=`` path that lands in a
    # registered project tier directory, the indexer will tag the
    # resulting chunks with that project tier — so the gates must see
    # the same tier the chunks will end up in. A caller leaving scope
    # at its default ``user`` while pointing ``file=`` at a project-
    # shared path would otherwise bypass Gate B (no confirm required)
    # and Gate A (force_unsafe=True still allowed). Mirrors the
    # ``_mem_add_core`` inferred-scope contract.
    target: Path | None = None
    if file:
        target, err = _validate_path(file, mdirs, pmdirs)
        if err:
            return err
        assert target is not None
        inferred_scope, _ = classify_scope(target, pmdirs)
        effective_scope = inferred_scope
    else:
        effective_scope = scope

    # Gate B (surface layer). Mirrors mem_add — explicit confirm
    # required for any project_shared write, batch or single.
    if effective_scope == "project_shared" and not confirm_project_shared:
        hint = (
            ""
            if effective_scope == scope
            else " (scope inferred from file= path; the target directory is "
            "a registered project_shared tier)"
        )
        return (
            "Error: scope='project_shared' writes to a git-tracked "
            f"directory. Pass confirm_project_shared=True to proceed.{hint}"
        )

    # Trust-boundary redaction guard. Each entry routes through
    # ``enforce_write_guard(record_outcome=False)`` so the
    # project_shared force_unsafe hard refusal applies on the batch
    # path too — the earlier inline ``privacy.scan`` + manual
    # ``record`` pattern bypassed gate A and was the bypass route
    # ADR-0011 §5 explicitly closes. We collect decisions first and
    # only record outcomes after deciding whether to commit the
    # whole batch (transactional invariant: no pass record on a
    # rejected batch).
    decisions: list[tuple[int, str, int]] = []  # (idx, decision, hit_count)
    for idx, entry in enumerate(entries):
        value = entry.get("value") or entry.get("content", "")
        if not value:
            continue
        guard = privacy.enforce_write_guard(
            value,
            surface="mem_batch_add",
            force_unsafe=force_unsafe,
            scope=effective_scope,
            audit_context={
                "namespace": namespace,
                "file": file,
                "item_idx": idx,
                "scope": effective_scope,
                "scope_inferred_from_path": effective_scope != scope,
            },
            record_outcome=False,
        )
        decisions.append((idx, guard.decision, len(guard.hits)))

    blocked = [d for d in decisions if d[1] == "blocked"]
    blocked_shared = [d for d in decisions if d[1] == "blocked_project_shared"]
    if blocked_shared:
        # Hard-refusal on project_shared force_unsafe — record blocked_project_shared
        # for each hit item, no pass for clean items (transactional reject).
        for _ in blocked_shared:
            privacy.record("blocked_project_shared", "mem_batch_add")
        idxs = [d[0] for d in blocked_shared]
        return (
            f"Error: items at indices {sorted(idxs)} match privacy patterns "
            "and force_unsafe=True is not permitted on scope='project_shared' "
            "(git history is forever). Whole batch rejected. Move flagged "
            "items to scope='project_local' or scope='user' to bypass."
        )
    if blocked:
        for _ in blocked:
            privacy.record("blocked", "mem_batch_add")
        idxs = [d[0] for d in blocked]
        return (
            f"Error: items at indices {sorted(idxs)} match privacy patterns; "
            "whole batch rejected. Resubmit with hit items removed, or pass "
            "force_unsafe=True to bypass (audit-logged)."
        )

    # Batch will commit — record per-entry outcomes and emit bypass
    # audits for any force-unsafe hits.
    for idx, decision, hit_count in decisions:
        if decision == "bypassed":
            privacy.record("bypassed", "mem_batch_add")
            value = entries[idx].get("value") or entries[idx].get("content", "")
            privacy.emit_bypass_audit(
                surface="mem_batch_add",
                content_chars=len(value),
                hits=hit_count,
                audit_context={
                    "namespace": namespace,
                    "file": file,
                    "item_idx": idx,
                    "scope": effective_scope,
                },
            )
        else:
            privacy.record("pass", "mem_batch_add")

    if target is None:
        # ADR-0011: scope-aware default-dated file target. Mirrors
        # ``_mem_add_core`` so MCP ``mem_batch_add(scope='project_shared')``
        # lands in the project's ``.memtomem/memories/`` directory, not
        # the user-tier path.
        from memtomem.memory_scope import (
            MemoryScopeError,
            is_project_tier_registered,
            project_tier_registration_error,
            resolve_memory_scope_dir,
        )
        from memtomem.server.tools.search import _resolve_project_context_root

        if effective_scope == "user":
            base = mdirs[0] if mdirs else Path(".")
            base = Path(base).expanduser().resolve()
        else:
            project_root = _resolve_project_context_root(app)
            try:
                base = resolve_memory_scope_dir(
                    effective_scope, project_root, user_base=Path(mdirs[0])
                )
            except MemoryScopeError as exc:
                return f"Error: {exc}"
            # ADR-0011 PR-D round 6: refuse if the resolved tier dir is
            # not registered in IndexingConfig.project_memory_dirs.
            # Otherwise the row's scope flips to project but the read
            # surface / watcher cannot see it.
            if not is_project_tier_registered(base, pmdirs):
                return f"Error: {project_tier_registration_error(base, effective_scope)}"
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        target = base / f"{date_str}.md"

    assert target is not None
    target.parent.mkdir(parents=True, exist_ok=True)
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
