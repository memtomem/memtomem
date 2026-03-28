"""Tools: mem_policy_add, mem_policy_list, mem_policy_delete, mem_policy_run."""

from __future__ import annotations

import json
import logging

from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register

logger = logging.getLogger(__name__)

_VALID_TYPES = {"auto_archive", "auto_expire", "auto_tag"}


@mcp.tool()
@tool_handler
@register("policy")
async def mem_policy_add(
    name: str,
    policy_type: str,
    config: str = "{}",
    namespace_filter: str | None = None,
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Create a memory lifecycle policy.

    Policies automate memory management: archiving old memories,
    expiring unused ones, or auto-tagging untagged chunks.

    Args:
        name: Unique policy name
        policy_type: One of 'auto_archive', 'auto_expire', 'auto_tag'
        config: JSON config string. Examples:
            auto_archive: {"max_age_days": 30, "archive_namespace": "archive"}
            auto_expire: {"max_age_days": 90}
            auto_tag: {"max_tags": 5}
        namespace_filter: Only apply to chunks in this namespace
    """
    if policy_type not in _VALID_TYPES:
        return f"Error: policy_type must be one of {_VALID_TYPES}"

    try:
        cfg = json.loads(config)
    except json.JSONDecodeError as exc:
        return f"Error: invalid JSON config: {exc}"

    app = _get_app(ctx)
    existing = await app.storage.policy_get(name)
    if existing:
        return f"Error: policy '{name}' already exists."

    policy_id = await app.storage.policy_add(name, policy_type, cfg, namespace_filter)
    return f"Policy '{name}' created (type={policy_type}, id={policy_id})"


@mcp.tool()
@tool_handler
@register("policy")
async def mem_policy_list(
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """List all memory lifecycle policies."""
    app = _get_app(ctx)
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
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Delete a memory lifecycle policy.

    Args:
        name: Policy name to delete
    """
    app = _get_app(ctx)
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
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Run memory lifecycle policies.

    Args:
        name: Run specific policy by name. If omitted, runs all enabled policies.
        dry_run: Preview what would happen without making changes (default: true)
    """
    from memtomem.tools.policy_engine import run_all_enabled, run_policy

    app = _get_app(ctx)

    if name:
        policy = await app.storage.policy_get(name)
        if not policy:
            return f"Error: policy '{name}' not found."
        result = await run_policy(app.storage, policy, dry_run=dry_run)
        if not dry_run:
            await app.storage.policy_update_last_run(name)
        return f"{'[DRY RUN] ' if dry_run else ''}{result.details}"

    results = await run_all_enabled(app.storage, dry_run=dry_run)
    if not results:
        return "No enabled policies to run."

    lines = [f"Policy run {'(dry run) ' if dry_run else ''}results:"]
    for r in results:
        lines.append(f"\n- **{r.policy_name}** ({r.policy_type}): {r.details}")

    return "\n".join(lines)
