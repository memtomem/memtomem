"""Tools: mem_consolidate, mem_consolidate_apply."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app_initialized
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register

logger = logging.getLogger(__name__)


@mcp.tool()
@tool_handler
@register("maintenance")
async def mem_consolidate(
    namespace: str | None = None,
    source_filter: str | None = None,
    max_groups: int = 5,
    min_group_size: int = 3,
    ctx: CtxType = None,
) -> str:
    """Find groups of related chunks that could be consolidated into summaries.

    Analyzes chunks by source file and semantic similarity to identify
    groups that an agent can summarize. This is a dry-run — no mutations.

    Args:
        namespace: Scope to this namespace
        source_filter: Only analyze chunks from matching sources
        max_groups: Maximum number of groups to return
        min_group_size: Minimum chunks per group (default 3)
    """
    if not 1 <= max_groups <= 50:
        return f"Error: max_groups must be between 1 and 50, got {max_groups}."
    if min_group_size < 2:
        return f"Error: min_group_size must be at least 2, got {min_group_size}."

    app = await _get_app_initialized(ctx)
    effective_ns = namespace or app.current_namespace

    # Group chunks by source file
    sources = await app.storage.get_source_files_with_counts()
    if source_filter:
        from memtomem.search.pipeline import match_source_filter

        # Same substring + glob contract as ``mem_search(source_filter=...)``,
        # including separator-fold for Windows portability (#720).
        sources = [s for s in sources if match_source_filter(source_filter, str(s[0]))]

    # Filter by namespace if specified
    if effective_ns:
        sources = [s for s in sources if s[3] and effective_ns in s[3].split(",")]

    # Find groups with enough chunks
    groups: list[dict[str, Any]] = []
    group_id = 0
    for path, count, updated, ns, avg_tok, _, _ in sources:
        if count < min_group_size:
            continue
        chunks = await app.storage.list_chunks_by_source(path, limit=20)
        if len(chunks) < min_group_size:
            continue

        total_tokens = sum(len(c.content.split()) for c in chunks)
        previews = []
        chunk_ids = []
        for c in chunks[:5]:
            preview = c.content[:80].replace("\n", " ")
            previews.append(f"    - [{str(c.id)[:8]}] {preview}...")
            chunk_ids.append(str(c.id))

        groups.append(
            {
                "group_id": group_id,
                "source": str(path),
                "chunk_count": len(chunks),
                "total_tokens": total_tokens,
                "namespace": ns,
                "previews": previews,
                "chunk_ids": chunk_ids,
            }
        )
        group_id += 1
        if len(groups) >= max_groups:
            break

    if not groups:
        return (
            "No consolidation candidates found.\n"
            f"(Checked {len(sources)} source files, "
            f"min_group_size={min_group_size}, max_groups={max_groups})"
        )

    lines = [f"Consolidation candidates: {len(groups)} groups\n"]
    for g in groups:
        lines.append(f"### Group {g['group_id']}: {g['source'].split('/')[-1]}")
        lines.append(f"  Chunks: {g['chunk_count']}, ~{g['total_tokens']} tokens")
        if g["namespace"]:
            lines.append(f"  Namespace: {g['namespace']}")
        lines.extend(g["previews"])
        lines.append(f"  → Use mem_consolidate_apply(group_id={g['group_id']}, summary='...')")
        lines.append("")

    # Persist groups to scratch storage (survives restart, auto-expires in 1 hour)
    import json
    from datetime import datetime, timedelta, timezone

    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
    await app.storage.scratch_set(
        "consolidation_groups",
        json.dumps(groups, default=str),
        expires_at=expires,
    )

    return "\n".join(lines)


@mcp.tool()
@tool_handler
@register("maintenance")
async def mem_consolidate_apply(
    group_id: int,
    summary: str,
    keep_originals: bool = True,
    confirm_project_shared: bool = False,
    ctx: CtxType = None,
) -> str:
    """Apply a consolidation by creating a summary chunk for a group.

    The agent writes the summary; this tool persists it via the normal
    ``mem_add`` path — the summary becomes a real markdown entry in the
    user's first ``memory_dirs`` daily notes file, and each original chunk
    gets a ``consolidated_into`` relation pointing to the summary. This
    preserves the file-based mental model: consolidation events are visible
    in the filesystem and in git history, and the summary can be
    hand-edited like any other markdown entry.

    ADR-0011 cross-scope rejection: source chunks are loaded by
    ``chunk_ids`` (the truth source for the group, robust to source
    rename / re-index between ``mem_consolidate`` and the apply call)
    and their persisted ``metadata.scope`` is inspected. Mixed-scope
    groups are skipped — the resulting summary cannot inherit a single
    trust tier from heterogeneous sources. Project-shared sources
    require ``confirm_project_shared=True`` so the summary write does
    not silently land in a git-tracked tier.

    The policy-driven ``auto_consolidate`` flow deliberately takes a
    different path (virtual chunk in the ``archive:summary`` namespace with
    content-embedded source hash for idempotency). See
    ``project_ltm_manager_roadmap.md`` Phase A.5 for the rationale.

    Args:
        group_id: Group ID from mem_consolidate output.
        summary: The consolidated summary written by the agent.
        keep_originals: Keep original chunks (default True). If False,
            originals are soft-decayed (``importance_score *= 0.5``, floor
            0.3); never a hard delete.
        confirm_project_shared: Required when the group's source chunks
            live in scope='project_shared'. The summary inherits the
            same scope and lands in the project's git-tracked memory
            directory; the explicit confirm prevents silent commits to
            shared trust tier.
    """
    import json
    from datetime import datetime, timezone
    from uuid import UUID

    from memtomem.tools.consolidation_engine import (
        DECAY_FACTOR,
        DECAY_FLOOR,
        link_consolidation_relations,
    )

    app = await _get_app_initialized(ctx)

    entry = await app.storage.scratch_get("consolidation_groups")
    if not entry:
        return "Error: run mem_consolidate first to identify groups."

    # Check expiration (scratch_get does not filter expired entries)
    if entry.get("expires_at"):
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if entry["expires_at"] < now:
            await app.storage.scratch_delete("consolidation_groups")
            return "Error: consolidation groups are stale (>1 hour). Run mem_consolidate again."

    groups = json.loads(entry["value"])

    group = next((g for g in groups if g["group_id"] == group_id), None)
    if group is None:
        return f"Error: group_id {group_id} not found. Run mem_consolidate again."

    # ADR-0011 cross-scope check. Load source chunks by id (the
    # truth source); a re-index between ``mem_consolidate`` and
    # ``mem_consolidate_apply`` would change ``group["source"]`` chunk
    # set but leave the persisted UUIDs stable.
    raw_ids = group.get("chunk_ids") or []
    try:
        chunk_uuids = [UUID(cid) for cid in raw_ids]
    except (ValueError, TypeError):
        return f"Error: group {group_id} contains invalid chunk_ids."
    chunks_map = await app.storage.get_chunks_batch(chunk_uuids) if chunk_uuids else {}
    source_scopes = {(c.metadata.scope or "user") for c in chunks_map.values()}
    if len(source_scopes) > 1:
        # Mixed-scope group — refuse because the summary would otherwise
        # silently inherit one tier and lose the others. Surfaced in
        # the MCP return string (not just logger.warning) so callers
        # see the rejection reason.
        logger.warning(
            "mem_consolidate_apply: skipping group %s — mixed memory scopes %s",
            group_id,
            sorted(source_scopes),
        )
        return (
            f"Error: skipped group {group_id}: mixed memory scopes "
            f"({sorted(source_scopes)}). Re-run mem_consolidate with a "
            "narrower filter so each group spans a single scope tier."
        )
    derived_scope = next(iter(source_scopes), "user") if source_scopes else "user"
    # ADR-0011 PR-D review round 7: enumerate the source chunks'
    # persisted ``project_root`` values too. ``mem_consolidate`` walks
    # source files globally, so a project-tier group can come from a
    # project that is NOT the MCP server's current cwd. Without the
    # override below, ``_mem_add_core`` would resolve the write target
    # via ``_resolve_project_context_root(app)`` (server cwd) and the
    # summary would land in the wrong project's ``.memtomem`` tier (or
    # fail when the server is not inside any project). Pin the override
    # to the source chunks' shared project_root; refuse mixed-project
    # groups so the summary cannot silently span project boundaries.
    source_project_roots = {
        c.metadata.project_root for c in chunks_map.values() if c.metadata.project_root is not None
    }
    if derived_scope in ("project_shared", "project_local") and len(source_project_roots) > 1:
        sorted_roots = sorted(str(p) for p in source_project_roots)
        logger.warning(
            "mem_consolidate_apply: skipping group %s — mixed project_root values %s",
            group_id,
            sorted_roots,
        )
        # Join paths with ", " (not Python list repr) so Windows backslash
        # paths surface as literal ``C:\Users\...`` rather than the doubly-
        # escaped ``C:\\\\Users\\\\...`` produced by ``str(list)``. Keeps
        # the user-facing message readable AND lets the pin test's
        # ``str(proj_a) in out`` substring check work cross-platform.
        return (
            f"Error: skipped group {group_id}: source chunks span multiple "
            f"projects ({', '.join(sorted_roots)}). A consolidated summary "
            "cannot pick one project_root without discarding the others; "
            "re-run mem_consolidate with a source_filter that pins a single "
            "project."
        )
    # ADR-0011 PR-D review round 10 (B1): a project-tier ``derived_scope``
    # with EVERY source chunk carrying ``project_root=None`` (legacy rows
    # written before the PR-B migration backfill, or any decode that
    # leaves the column NULL) used to fall through ``project_root_override =
    # None`` → ``_mem_add_core`` then resolved the target via
    # ``_resolve_project_context_root(app)`` (server cwd) and the summary
    # silently leaked into whatever project the server happened to be in.
    # The mixed-project rejection above only fires for ``> 1`` distinct
    # roots; the zero-root case slipped past. Refuse explicitly so the
    # write target cannot be inferred from ambient context.
    if derived_scope in ("project_shared", "project_local") and not source_project_roots:
        logger.warning(
            "mem_consolidate_apply: skipping group %s — derived_scope=%s but no source "
            "chunk carries project_root",
            group_id,
            derived_scope,
        )
        return (
            f"Error: skipped group {group_id}: derived_scope='{derived_scope}' "
            "but no source chunk carries a persisted project_root. The "
            "summary's destination cannot be resolved without falling "
            "back to the MCP server's current cwd, which would risk a "
            "cross-project leak. This typically affects pre-ADR-0011 rows "
            "that were not project-classified by the schema migration; "
            "re-index the source files (mm reindex) so chunks carry "
            "project_root, or re-run mem_consolidate with a source_filter "
            "that pins a single registered project."
        )
    project_root_override: "Path | None" = (
        next(iter(source_project_roots)) if source_project_roots else None
    )
    if derived_scope == "project_shared" and not confirm_project_shared:
        return (
            f"Error: group {group_id} sources live in scope='project_shared'. "
            "The consolidated summary would land in the project's "
            "git-tracked memory tier. Pass confirm_project_shared=True to "
            "proceed."
        )

    # Agent path is file-first: append to a daily notes file + index. We use
    # ``_mem_add_core`` (not the MCP ``mem_add`` tool) so we can grab the
    # IndexingStats and recover the new chunk id without the old
    # ``recall_chunks(limit=1)`` trick, which raced with any concurrent
    # write between mem_add and the lookup — silent data corruption
    # territory.
    from memtomem.server.tools.memory_crud import _mem_add_core

    source_name = group["source"].split("/")[-1]
    add_result, stats = await _mem_add_core(
        content=summary,
        title=f"Consolidated: {source_name}",
        tags=["consolidated", "summary"],
        file=None,
        namespace=group.get("namespace"),
        template=None,
        ctx=ctx,
        scope=derived_scope,
        confirm_project_shared=confirm_project_shared,
        project_root_override=project_root_override,
    )

    if stats is None or not stats.new_chunk_ids:
        logger.warning(
            "mem_consolidate_apply: mem_add produced no new chunk ids — "
            "cannot link originals for group %s",
            group_id,
        )
        await app.storage.scratch_delete("consolidation_groups")
        return (
            f"Consolidation applied for group {group_id} (unlinked).\n"
            f"{add_result}\n"
            f"- Original chunks: {group['chunk_count']}\n"
            f"- Originals kept: {keep_originals}\n"
            "- Warning: could not recover summary chunk id; relations not created."
        )

    if len(stats.new_chunk_ids) > 1:
        # Canary for chunker behavior drift. Today the Markdown chunker
        # keeps a single ``Consolidated: ...`` H1 section together, so we
        # expect exactly 1. If this warning ever fires, revisit the
        # summary → chunk matching strategy (see Phase A.5 docs-review
        # thread) — the current "take the first" rule is intentionally
        # simple so the contract failure is loud.
        logger.warning(
            "mem_consolidate_apply: mem_add produced %d chunks, using first as summary_id",
            len(stats.new_chunk_ids),
        )

    summary_id = stats.new_chunk_ids[0]

    # Link originals → summary via the shared helper (same edge type that
    # execute_auto_consolidate uses, so queries like mem_related / mem_expand
    # work uniformly across both flows).
    linked = await link_consolidation_relations(
        app.storage,
        group["chunk_ids"],
        summary_id,
    )

    if not keep_originals and group["chunk_ids"]:
        scores = await app.storage.get_importance_scores(group["chunk_ids"])
        if scores:
            floored = {cid: max(score * DECAY_FACTOR, DECAY_FLOOR) for cid, score in scores.items()}
            await app.storage.update_importance_scores(floored)

    app.search_pipeline.invalidate_cache()
    await app.storage.scratch_delete("consolidation_groups")

    return (
        f"Consolidation applied for group {group_id}.\n"
        f"{add_result}\n"
        f"- Summary chunk id: {summary_id}\n"
        f"- Originals linked: {linked}/{group['chunk_count']}\n"
        f"- Originals kept: {keep_originals}"
    )
