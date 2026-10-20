"""Tools: mem_consolidate, mem_consolidate_apply."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from memtomem.config import TargetScope
from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app_initialized
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register

if TYPE_CHECKING:
    from memtomem.models import IndexingStats

logger = logging.getLogger(__name__)


class _SourceUnavailable(Exception):
    """A consolidation source became hidden after its summary was published."""


class _AlreadyConsolidated(Exception):
    """Another summary covered an original after this one was published."""


@dataclass(frozen=True)
class _ApplyOutcome:
    """How one consolidation apply ended: its reply, its run row, and its cleanup."""

    reply: str
    status: str
    summary: dict
    affected_count: int = 0
    error: str | None = None
    # A published summary makes the preview stale: applying it again would
    # write a second one.
    drop_preview: bool = False
    invalidate_cache: bool = False


async def _drop_preview(app: Any, entry: dict) -> bool:
    """Delete the consolidation preview if it still holds ``entry``'s value.

    A ``mem_consolidate`` that ran meanwhile replaced the row with its own
    preview, which this apply never read and must not delete (#2798).
    """
    return await app.storage.scratch_delete("consolidation_groups", expected_value=entry["value"])


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
    groups that an agent can summarize. Chunks an existing summary covers
    are left out. This is a dry-run — no mutations.

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
        # A held source is screened in the read, so its rows never reach the
        # previews below even if the hold lands after the listing above. So are
        # chunks a summary already covers (#2802): chunks added since form a
        # group of their own.
        chunks = await app.storage.list_chunks_by_source(
            path, limit=20, exclude_hidden=True, as_stored=True, exclude_consolidated=True
        )
        if len(chunks) < min_group_size:
            continue

        total_tokens = sum(len(c.content.split()) for c in chunks)
        previews = []
        for c in chunks[:5]:
            preview = c.content[:80].replace("\n", " ")
            previews.append(f"    - [{str(c.id)[:8]}] {preview}...")
        # Every listed chunk, not just the previewed ones: an apply links the
        # ids it is given, and an unlinked rest would be listed again.
        chunk_ids = [str(c.id) for c in chunks]

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
        lines.append(f"### Group {g['group_id']}: {Path(g['source']).name}")
        lines.append(f"  Chunks: {g['chunk_count']}, ~{g['total_tokens']} tokens")
        if g["namespace"]:
            lines.append(f"  Namespace: {g['namespace']}")
        lines.extend(g["previews"])
        lines.append(f"  → Use mem_consolidate_apply(group_id={g['group_id']}, summary='...')")
        lines.append("")

    # Persist groups to scratch storage (survives restart, auto-expires in 1 hour)
    import json
    from datetime import datetime, timedelta, timezone
    from uuid import uuid4

    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
    # ``preview_id`` makes every scan's value unique, even one that finds the
    # same groups: the apply deletes only the value it read (#2798).
    await app.storage.scratch_set(
        "consolidation_groups",
        json.dumps({"preview_id": uuid4().hex, "groups": groups}, default=str),
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
    ``mem_add`` path — the summary becomes a real markdown entry in a file
    of its own under ``consolidated/`` in its scope's memory directory, and
    each original chunk gets a ``consolidated_into`` relation pointing to the
    summary. This preserves the file-based mental model: consolidation events
    are visible in the filesystem and in git history, and the summary can be
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
            await _drop_preview(app, entry)
            return "Error: consolidation groups are stale (>1 hour). Run mem_consolidate again."

    preview = json.loads(entry["value"])
    # A bare list is the shape before ``preview_id``.
    groups = preview["groups"] if isinstance(preview, dict) else preview

    group = next((g for g in groups if g["group_id"] == group_id), None)
    if group is None:
        return f"Error: group_id {group_id} not found. Run mem_consolidate again."
    if await app.storage.is_source_hidden(Path(group["source"])):
        return "Error: source is unavailable. Run mem_consolidate after it returns."

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
    for chunk in chunks_map.values():
        if await app.storage.is_source_hidden(chunk.metadata.source_file):
            return "Error: source is unavailable. Run mem_consolidate after it returns."
    # A preview taken before an apply still lists its group (#2802). The
    # summaries are not named: one can be held while its originals are not, and
    # ``mem_related`` screens what it shows.
    covered = await app.storage.get_consolidation_targets(chunk_uuids)
    if covered:
        return (
            f"Error: group {group_id} is already consolidated: {len(covered)} of its "
            f"{len(chunk_uuids)} originals are covered by an existing summary "
            "(mem_related on an original shows it). Run mem_consolidate again; it "
            "lists only chunks no summary covers."
        )
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

    # Agent path is file-first: write a summary file + index. We use
    # ``_mem_add_core`` (not the MCP ``mem_add`` tool) so we can grab the
    # IndexingStats and recover the new chunk id without the old
    # ``recall_chunks(limit=1)`` trick, which raced with any concurrent
    # write between mem_add and the lookup — silent data corruption
    # territory.
    from memtomem.memory_scope import consolidation_file_name
    from memtomem.server.tools.memory_crud import _mem_add_core

    source_name = Path(group["source"]).name
    # Applied-run record (#2132). Opened after the scope checks above so a
    # refused call leaves no row; the policy path records its own runs through
    # ``run_policy``, which this handler deliberately does not go through.
    run_id = await app.storage.maintenance_run_start("consolidate_apply", source="mcp")
    run_summary: dict = {
        "group_id": group_id,
        "source": group["source"],
        "keep_originals": keep_originals,
        "chunk_ids": [str(c) for c in group["chunk_ids"]],
    }
    run_namespaces = [group.get("namespace") or "default"]

    async def link_published(add_result: str, stats: IndexingStats) -> _ApplyOutcome:
        if not stats.new_chunk_ids:
            logger.warning(
                "mem_consolidate_apply: mem_add produced no new chunk ids — "
                "cannot link originals for group %s",
                group_id,
            )
            return _ApplyOutcome(
                f"Consolidation applied for group {group_id} (unlinked).\n"
                f"{add_result}\n"
                f"- Original chunks: {group['chunk_count']}\n"
                f"- Originals kept: {keep_originals}\n"
                "- Warning: could not recover summary chunk id; relations not created.",
                status="ok",
                summary={**run_summary, "summary_id": None, "linked": 0, "warning": "unlinked"},
                drop_preview=True,
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

        # The checks above ran before the summary was published; a hold can
        # land in between. The summary was written from what the agent read
        # while the source was visible, so it stays. The edges and the decay
        # write to the originals' rows, so they run in one write transaction
        # that re-checks the hold first: no hold can commit between that check
        # and these writes.
        decayed_ids: list[str] = []
        try:
            async with app.storage.transaction():
                live = await app.storage.get_chunks_batch(chunk_uuids) if chunk_uuids else {}
                unavailable = await app.storage.is_source_hidden(Path(group["source"]))
                for chunk in live.values():
                    unavailable = unavailable or await app.storage.is_source_hidden(
                        chunk.metadata.source_file
                    )
                if unavailable:
                    raise _SourceUnavailable
                # A concurrent apply of the same preview passed the check above
                # too. Read under this lock, so its edges cannot commit between
                # this read and the writes below.
                if await app.storage.get_consolidation_targets(list(live)):
                    raise _AlreadyConsolidated
                # Only ids that still resolve: a re-index can drop an original
                # after mem_consolidate, and its edge would fail the foreign
                # key. With those gone, linking is strict — a lenient loop that
                # swallowed an error which ended the transaction would let the
                # next write open a fresh one, outside the check above.
                live_ids = [str(cid) for cid in chunk_uuids if cid in live]
                # Same edge type that execute_auto_consolidate uses, so queries
                # like mem_related / mem_expand work uniformly across both flows.
                linked = await link_consolidation_relations(
                    app.storage, live_ids, summary_id, strict=True
                )
                if not keep_originals and live_ids:
                    scores = await app.storage.get_importance_scores(live_ids)
                    if scores:
                        floored = {
                            cid: max(score * DECAY_FACTOR, DECAY_FLOOR)
                            for cid, score in scores.items()
                        }
                        await app.storage.update_importance_scores(floored)
                        decayed_ids = [str(cid) for cid in floored]
        except _SourceUnavailable:
            return _ApplyOutcome(
                f"Consolidation applied for group {group_id} (unlinked).\n"
                f"{add_result}\n"
                f"- Original chunks: {group['chunk_count']}\n"
                "- Warning: the source became unavailable after the summary was written; "
                "originals were not linked or decayed.",
                status="ok",
                summary={
                    **run_summary,
                    "summary_id": str(summary_id),
                    "linked": 0,
                    "warning": "source_unavailable",
                },
                drop_preview=True,
            )
        except _AlreadyConsolidated:
            return _ApplyOutcome(
                f"Consolidation applied for group {group_id} (unlinked).\n"
                f"{add_result}\n"
                f"- Summary chunk id: {summary_id}\n"
                f"- Original chunks: {group['chunk_count']}\n"
                "- Warning: another summary covered these originals while this one was "
                "written; originals were not linked or decayed, and this summary "
                "duplicates it.",
                status="ok",
                summary={
                    **run_summary,
                    "summary_id": str(summary_id),
                    "linked": 0,
                    "warning": "already_consolidated",
                },
                drop_preview=True,
            )

        return _ApplyOutcome(
            f"Consolidation applied for group {group_id}.\n"
            f"{add_result}\n"
            f"- Summary chunk id: {summary_id}\n"
            f"- Originals linked: {linked}/{group['chunk_count']}\n"
            f"- Originals kept: {keep_originals}\n"
            f"- Run id: {run_id}",
            status="ok",
            affected_count=linked,
            summary={
                **run_summary,
                "summary_id": str(summary_id),
                "linked": linked,
                "decayed_ids": decayed_ids,
            },
            drop_preview=True,
            invalidate_cache=True,
        )

    try:
        if await app.storage.is_source_hidden(Path(group["source"])):
            # Held since the checks above, with the row already open: nothing
            # was written, so the row records a refusal rather than staying
            # ``running``.
            refusal = "Error: source is unavailable. Run mem_consolidate after it returns."
            outcome = _ApplyOutcome(refusal, status="error", summary=run_summary, error=refusal)
        else:
            add_result, stats = await _mem_add_core(
                content=summary,
                title=f"Consolidated: {source_name}",
                tags=["consolidated", "summary"],
                file=None,
                namespace=group.get("namespace"),
                template=None,
                ctx=ctx,
                scope=cast(TargetScope, derived_scope),
                confirm_project_shared=confirm_project_shared,
                project_root_override=project_root_override,
                event_type="consolidate_apply",
                # The consent was given to *this* tool, so the ADR-0011 §5 line
                # the shared core emits must name it (#2306). ``mem_add`` never
                # saw this call.
                confirmation_surface="mem_consolidate_apply",
                # A file of its own, not the day file: there the summary can be
                # re-chunked with a neighbouring entry, and the new chunk id
                # drops the edges ``link_published`` writes (#2802).
                file_name=consolidation_file_name(
                    source_name,
                    run_id,
                    date_str=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                ),
            )
            if stats is None:
                # The core refused before writing anything (an excluded target
                # #2488, a namespace mix, the redaction guard): nothing was
                # consolidated, so neither record the run as applied nor drop
                # the preview a retry needs.
                outcome = _ApplyOutcome(
                    add_result, status="error", summary=run_summary, error=add_result
                )
            else:
                outcome = await link_published(add_result, stats)
    except Exception as exc:
        # The mutation may have partly landed — ``_mem_add_core`` can have
        # appended and indexed the summary before the failure — so the row
        # keeps the provenance known at this point.
        try:
            await app.storage.maintenance_run_finish(
                run_id,
                status="error",
                namespaces=run_namespaces,
                summary=run_summary,
                error=f"{type(exc).__name__}: {exc}",
            )
        except Exception:
            # Best-effort: the original failure is the one worth raising.
            logger.warning("could not finalize maintenance run %s", run_id, exc_info=True)
        from memtomem.indexing.engine import PrivacyRejection

        if isinstance(exc, PrivacyRejection) and exc.mutation_outcome == "appended":
            # The summary is in the file but its result may not be indexed;
            # say so rather than surfacing an internal error. The preview goes:
            # applying it again would append a second summary.
            try:
                dropped = await _drop_preview(app, entry)
            except Exception:
                logger.warning("could not drop the consolidation preview", exc_info=True)
                after = (
                    " The preview could not be discarded; do not apply it again, or the "
                    "summary is appended a second time."
                )
            else:
                if dropped:
                    after = (
                        " The preview was discarded; run mem_consolidate again once the file "
                        "is clean."
                    )
                else:
                    after = (
                        " The preview had been replaced or removed meanwhile; if a newer "
                        "mem_consolidate preview lists this group, do not apply it, or the "
                        "summary is appended a second time."
                    )
            return (
                exc.mutation_detail("mem_consolidate_apply", outcome="appended", surface="mcp")
                + after
            )
        raise

    # The one place a returned outcome finishes the row, outside the handler
    # above and before the cleanup below: the row records what happened to
    # memory, so a cleanup that fails must not rewrite it as an error.
    await app.storage.maintenance_run_finish(
        run_id,
        status=outcome.status,
        affected_count=outcome.affected_count,
        namespaces=run_namespaces,
        summary=outcome.summary,
        error=outcome.error,
    )
    if outcome.invalidate_cache:
        app.search_pipeline.invalidate_cache()
    if outcome.drop_preview:
        await _drop_preview(app, entry)
    return outcome.reply
