"""Tool: mem_do — meta-tool that routes to any registered action."""

from __future__ import annotations

from memtomem.server import mcp
from memtomem.server.context import CtxType
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import ACTIONS

# Common aliases for discoverability — maps intuitive names to actual actions
_ALIASES: dict[str, str] = {
    "health": "eval",
    "health_report": "eval",
    "health_check": "watchdog",
    "suggest": "search_suggest",
    "history": "search_history",
    "namespace_set": "ns_set",
    "namespace_list": "ns_list",
    "namespace_delete": "ns_delete",
    "namespace_rename": "ns_rename",
    "namespace_assign": "ns_assign",
    "tag_auto": "auto_tag",
    "orphans": "cleanup_orphans",
}


@mcp.tool()
@tool_handler
async def mem_do(
    action: str,
    params: dict | None = None,
    ctx: CtxType = None,
) -> str:
    """Execute a memtomem action by name.

    This is the gateway to all advanced memtomem features beyond the
    core tools (search, add, index, recall, status, stats, list, read).

    Use action="help" to see all available actions grouped by category.
    Use action="help" with params={"category": "sessions"} for details.

    Categories: crud, namespace, tags, sessions, scratch, relations,
    analytics, maintenance, policy, entity, multi_agent, importers,
    procedures, advanced, ingest, search, context

    Common aliases are resolved automatically (e.g. "orphans" →
    "cleanup_orphans", "health" → "eval").

    Args:
        action: The action name (e.g. "session_start", "tag_list", "help")
        params: Optional dict of parameters for the action
    """
    if not action or not action.strip():
        return "Error: action name cannot be empty. Use action='help' to list all."

    if action == "help":
        category = (params or {}).get("category")
        return _help(category)

    # Resolve aliases
    resolved = _ALIASES.get(action, action)
    info = ACTIONS.get(resolved)
    if not info:
        similar = [k for k in ACTIONS if action in k or k in action]
        # Also check aliases
        alias_matches = [k for k, v in _ALIASES.items() if action in k]
        similar = list(dict.fromkeys(similar + [_ALIASES[a] for a in alias_matches]))
        hint = f" Did you mean: {', '.join(similar[:3])}?" if similar else ""
        return f"Error: unknown action '{action}'.{hint} Use action='help' to list all."

    kwargs = dict(params) if params else {}
    kwargs["ctx"] = ctx
    try:
        return await info.fn(**kwargs)
    except TypeError as exc:
        return (
            f"Error: invalid parameter for action '{resolved}' — {exc}. "
            f'Use action=\'help\' with params={{"category": "{info.category}"}} for details.'
        )


def _help(category: str | None = None) -> str:
    """Generate action catalog."""
    if category:
        actions = {k: v for k, v in ACTIONS.items() if v.category == category}
        if not actions:
            cats = sorted({v.category for v in ACTIONS.values()})
            return f"Unknown category '{category}'. Available: {', '.join(cats)}"
        lines = [f"## {category} ({len(actions)} actions)\n"]
        for name, info in sorted(actions.items()):
            lines.append(f"**{name}**: {info.description}")
            for p, t in info.params.items():
                doc = info.param_docs.get(p, "")
                if doc:
                    lines.append(f"  - {p}: {t} — {doc}")
                else:
                    lines.append(f"  - {p}: {t}")
            lines.append("")
        return "\n".join(lines)

    by_cat: dict[str, list[str]] = {}
    for name, info in ACTIONS.items():
        by_cat.setdefault(info.category, []).append(name)

    lines = [f"# Available Actions ({len(ACTIONS)} total)\n"]
    for cat, names in sorted(by_cat.items()):
        lines.append(f"**{cat}** ({len(names)}): {', '.join(sorted(names))}")
    lines.append('\nUse params={"category": "<name>"} for details.')
    return "\n".join(lines)
