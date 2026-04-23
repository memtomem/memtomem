"""Tools: mem_policy_add, mem_policy_list, mem_policy_delete, mem_policy_run."""

from __future__ import annotations

import json
import logging

from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app_initialized
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register

logger = logging.getLogger(__name__)

_VALID_TYPES = {"auto_archive", "auto_consolidate", "auto_expire", "auto_promote", "auto_tag"}


@mcp.tool()
@tool_handler
@register("policy")
async def mem_policy_add(
    name: str,
    policy_type: str,
    config: str = "{}",
    namespace_filter: str | None = None,
    ctx: CtxType = None,
) -> str:
    """Create a memory lifecycle policy.

    Policies automate memory management: archiving old memories,
    expiring unused ones, auto-tagging untagged chunks, or consolidating
    related chunks into heuristic summaries.

    Args:
        name: Unique policy name
        policy_type: One of 'auto_archive', 'auto_consolidate',
            'auto_expire', 'auto_promote', 'auto_tag'
        config: JSON config string. Examples:
            auto_archive (flat — single target):
              {"max_age_days": 30, "archive_namespace": "archive"}

            auto_archive (rule + categorized buckets):
              {
                "max_age_days": 90,
                "age_field": "last_accessed_at",
                "min_access_count": 3,
                "max_importance_score": 0.3,
                "archive_namespace_template": "archive:{first_tag}"
              }
              - age_field: "created_at" (default) or "last_accessed_at"
                (null-safe: falls back to created_at via COALESCE).
              - min_access_count: only chunks with access_count <= this.
              - max_importance_score: only chunks with importance_score < this.
              - archive_namespace_template: per-chunk target. Supports the
                {first_tag} placeholder (empty tags → "misc"). Chunks already
                in their resolved target namespace are skipped.

            auto_consolidate:
              {
                "min_group_size": 3,
                "max_groups": 10,
                "max_bullets": 20,
                "keep_originals": true,
                "summary_namespace": "archive:summary"
              }
              - Groups chunks by source file (min chunks = min_group_size)
                and creates one deterministic heuristic summary per source,
                linking originals via ``consolidated_into`` relations.
              - Idempotent: re-runs with the same chunk set are a no-op;
                added/removed chunks trigger regeneration.
              - Mixed-namespace sources are skipped with a warning.
              - keep_originals=false soft-decays originals
                (importance_score *= 0.5, floor 0.3) instead of deleting.
              - Summaries default to the ``archive:summary`` namespace so
                they don't pollute default search results.

            auto_promote (inverse of auto_archive):
              {"min_access_count": 5, "target_namespace": "default"}
              {
                "source_prefix": "archive",
                "target_namespace": "default",
                "min_access_count": 3,
                "min_importance_score": 0.5,
                "recency_days": 30
              }
              - source_prefix: namespace prefix to scan (default "archive").
              - target_namespace: destination (default "default").
              - min_access_count: chunks need at least this many accesses.
              - min_importance_score: optional importance floor (AND).
              - recency_days: only if last_accessed_at is within N days
                (opposite of auto_archive's age cutoff — *recent* access
                qualifies). Null last_accessed_at disqualifies.
              - Resets last_accessed_at on promotion to prevent immediate
                re-archival (ping-pong prevention).

            auto_expire: {"max_age_days": 90}
            auto_tag: {"max_tags": 5}
        namespace_filter: Only apply to chunks in this namespace
    """
    if not name or not name.strip():
        return "Error: policy name cannot be empty."
    if policy_type not in _VALID_TYPES:
        return f"Error: policy_type must be one of: {', '.join(sorted(_VALID_TYPES))}"

    try:
        cfg = json.loads(config)
    except json.JSONDecodeError as exc:
        return f"Error: invalid JSON config: {exc}"

    app = await _get_app_initialized(ctx)
    existing = await app.storage.policy_get(name)
    if existing:
        return f"Error: policy '{name}' already exists."

    policy_id = await app.storage.policy_add(name, policy_type, cfg, namespace_filter)
    return f"Policy '{name}' created (type={policy_type}, id={policy_id})"


@mcp.tool()
@tool_handler
@register("policy")
async def mem_policy_list(
    ctx: CtxType = None,
) -> str:
    """List all memory lifecycle policies."""
    app = await _get_app_initialized(ctx)
    policies = await app.storage.policy_list()

    if not policies:
        return "No policies configured. Use mem_policy_add to create one."

    lines = [f"Memory Policies ({len(policies)}):"]
    for p in policies:
        status = "enabled" if p["enabled"] else "disabled"
        ns = f" [ns={p['namespace_filter']}]" if p["namespace_filter"] else ""
        last_run = f" (last run: {p['last_run_at']})" if p["last_run_at"] else ""
        lines.append(f"\n- **{p['name']}** ({p['policy_type']}, {status}){ns}{last_run}")
        lines.append(f"  Config: {json.dumps(p['config'])}")

    return "\n".join(lines)


@mcp.tool()
@tool_handler
@register("policy")
async def mem_policy_delete(
    name: str,
    ctx: CtxType = None,
) -> str:
    """Delete a memory lifecycle policy.

    Args:
        name: Policy name to delete
    """
    app = await _get_app_initialized(ctx)
    deleted = await app.storage.policy_delete(name)
    if not deleted:
        return f"Error: policy '{name}' not found."
    return f"Policy '{name}' deleted."


@mcp.tool()
@tool_handler
@register("policy")
async def mem_policy_run(
    name: str | None = None,
    dry_run: bool = True,
    ctx: CtxType = None,
) -> str:
    """Run memory lifecycle policies.

    Args:
        name: Run specific policy by name. If omitted, runs all enabled policies.
        dry_run: Preview what would happen without making changes (default: true)
    """
    from memtomem.tools.policy_engine import run_all_enabled, run_policy

    app = await _get_app_initialized(ctx)

    if name:
        policy = await app.storage.policy_get(name)
        if not policy:
            return f"Error: policy '{name}' not found."
        result = await run_policy(
            app.storage, policy, dry_run=dry_run, llm_provider=app.llm_provider
        )
        if not dry_run:
            await app.storage.policy_update_last_run(name)
            app.search_pipeline.invalidate_cache()
        return f"{'[DRY RUN] ' if dry_run else ''}{result.details}"

    results = await run_all_enabled(app.storage, dry_run=dry_run, llm_provider=app.llm_provider)
    if not results:
        return "No enabled policies to run."

    if not dry_run:
        app.search_pipeline.invalidate_cache()

    lines = [f"Policy run {'(dry run) ' if dry_run else ''}results:"]
    for r in results:
        lines.append(f"\n- **{r.policy_name}** ({r.policy_type}): {r.details}")

    return "\n".join(lines)
