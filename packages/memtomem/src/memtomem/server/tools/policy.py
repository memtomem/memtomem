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
        config: JSON config string; the keys depend on policy_type —
            auto_archive: max_age_days, archive_namespace (or
            archive_namespace_template), age_field, min_access_count,
            max_importance_score; auto_consolidate: min_group_size,
            max_groups, max_bullets, keep_originals, summary_namespace;
            auto_promote: source_prefix, target_namespace, min_access_count,
            min_importance_score, recency_days; auto_expire: max_age_days;
            auto_tag: max_tags. ``{}`` uses the per-type defaults. Field
            semantics and worked examples:
            docs/guides/reference/automation.md.
        namespace_filter: Only apply to chunks in this namespace

    Config schemas (by policy_type)::

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


_MAX_RUNS_SHOWN = 20
_IDS_PREVIEW = 5


def _format_ids(key: str, ids: list, omitted: int) -> str:
    preview = ", ".join(str(i) for i in ids[:_IDS_PREVIEW])
    total = len(ids) + omitted
    more = total - min(len(ids), _IDS_PREVIEW)
    suffix = f", … (+{more} more)" if more > 0 else ""
    return f"{key}: {total} — {preview}{suffix}"


def _summary_line(summary: dict) -> str | None:
    """One-line rendering of a run's per-kind outcome summary."""
    if not summary:
        return None
    for key in ("deleted_ids", "promoted_ids"):
        if key in summary:
            return _format_ids(key, summary[key], summary.get(f"{key}_omitted", 0))
    if "moved" in summary:
        moved = summary["moved"]
        per_target = "; ".join(
            f"{target}: {len(ids)}"
            for target, ids in sorted(moved.items())
            if not target.endswith("_omitted")
        )
        return f"moved → {per_target}" if per_target else None
    if "groups" in summary or "deleted_summary_ids" in summary:
        groups = len(summary.get("groups", []))
        deleted = len(summary.get("deleted_summary_ids", []))
        return f"{groups} groups consolidated, {deleted} stale summaries deleted"
    if "tagged_chunks" in summary:
        return (
            f"tagged {summary['tagged_chunks']}/{summary.get('total_chunks', '?')}"
            f" (skipped {summary.get('skipped_chunks', 0)})"
        )
    if "summary_id" in summary:
        return f"summary {summary['summary_id']} — {summary.get('linked', 0)} originals linked"
    return None


def _format_run(run: dict) -> list[str]:
    """Render one maintenance run row as indented markdown lines."""
    # ``running`` is either genuinely in flight or a run whose process died
    # between the mutation and the finish write. Both are reported as-is; the
    # missing ``completed_at`` is the signal, and nothing here can tell them
    # apart without a liveness check.
    head = f"  - #{run['id']} {run['status']} {run['started_at']} ({run['source']})"
    if run["status"] != "running":
        head += f" affected={run['affected_count']}"
    if run["namespaces"]:
        head += f" ns={','.join(run['namespaces'])}"
    if run["error"]:
        head += f" — {run['error']}"
    lines = [head]
    detail = _summary_line(run["summary"])
    if detail:
        lines.append(f"    {detail}")
    return lines


async def _other_runs_block(storage, runs: int, known_policies: set[str]) -> list[str]:
    """Runs that no listed policy owns: the agent path, and deleted policies."""
    candidates = await storage.maintenance_run_latest(limit=runs * (len(known_policies) + 2) + 20)
    orphans = [
        r for r in candidates if r["policy_name"] is None or r["policy_name"] not in known_policies
    ][:runs]
    if not orphans:
        return []
    lines = ["\nOther maintenance runs:"]
    for run in orphans:
        lines.extend(_format_run(run))
    return lines


@mcp.tool()
@tool_handler
@register("policy")
async def mem_policy_list(
    runs: int = 0,
    ctx: CtxType = None,
) -> str:
    """List all memory lifecycle policies.

    Args:
        runs: Also show the last N applied (non-dry-run) runs per policy,
            newest first, with status, source and what each run changed
            (default: 0 = none, max 20)
    """
    app = await _get_app_initialized(ctx)
    policies = await app.storage.policy_list()

    runs = max(0, min(int(runs), _MAX_RUNS_SHOWN))

    if not policies:
        base = "No policies configured. Use mem_policy_add to create one."
        if runs:
            # Records outlive the policies that wrote them; a deleted policy
            # must not take its history out of reach.
            extra = await _other_runs_block(app.storage, runs, set())
            if extra:
                return "\n".join([base, *extra])
        return base

    lines = [f"Memory Policies ({len(policies)}):"]
    for p in policies:
        status = "enabled" if p["enabled"] else "disabled"
        ns = f" [ns={p['namespace_filter']}]" if p["namespace_filter"] else ""
        last_run = f" (last run: {p['last_run_at']})" if p["last_run_at"] else ""
        lines.append(f"\n- **{p['name']}** ({p['policy_type']}, {status}){ns}{last_run}")
        lines.append(f"  Config: {json.dumps(p['config'])}")
        if runs:
            history = await app.storage.maintenance_run_latest(policy_name=p["name"], limit=runs)
            if history:
                lines.append("  Runs:")
                for run in history:
                    lines.extend(_format_run(run))
            else:
                lines.append("  Runs: none recorded")

    if runs:
        lines.extend(await _other_runs_block(app.storage, runs, {p["name"] for p in policies}))

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
            app.storage,
            policy,
            dry_run=dry_run,
            llm_provider=app.llm_provider,
            extract_entities=app.config.indexing.extract_entities,
        )
        if not dry_run:
            await app.storage.policy_update_last_run(name)
            app.search_pipeline.invalidate_cache()
        run_ref = f" (run #{result.run_id})" if result.run_id is not None else ""
        return f"{'[DRY RUN] ' if dry_run else ''}{result.details}{run_ref}"

    results = await run_all_enabled(
        app.storage,
        dry_run=dry_run,
        llm_provider=app.llm_provider,
        extract_entities=app.config.indexing.extract_entities,
    )
    if not results:
        return "No enabled policies to run."

    if not dry_run:
        app.search_pipeline.invalidate_cache()

    lines = [f"Policy run {'(dry run) ' if dry_run else ''}results:"]
    for r in results:
        run_ref = f" (run #{r.run_id})" if r.run_id is not None else ""
        lines.append(f"\n- **{r.policy_name}** ({r.policy_type}): {r.details}{run_ref}")

    return "\n".join(lines)
