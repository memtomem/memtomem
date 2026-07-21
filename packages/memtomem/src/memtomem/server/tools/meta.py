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
    # Deprecated: mem_context_migrate was renamed to mem_context_memory_migrate
    # in #1147 (B5-2). Keep the old mem_do action name working.
    "context_migrate": "context_memory_migrate",
    # Discoverability shortcut for the artifact-migration tool (#1147 B5-1).
    "artifact_migrate": "context_artifact_migrate",
    # Discoverability shortcut for the ADR-0022 promote tool (PR2). There is
    # deliberately NO "version" alias: the bare ``version`` action is already
    # taken by ``mem_version`` (protocol negotiation for memtomem-stm), and
    # aliases resolve BEFORE real actions in ``mem_do`` (see below), so a
    # "version" alias would silently shadow it. Use the canonical
    # ``context_version`` action instead. The alias-collision guard in
    # test_meta_tool.py pins this invariant for every alias.
    "promote": "context_promote",
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

    action="help" lists every action grouped by category;
    params={"category": "<name>"} adds per-parameter detail and, for some
    categories, usage notes.

    Common aliases resolve automatically (e.g. "orphans" →
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


#: Narrative that does not fit in a one-line action description and must not
#: live in the server ``instructions`` (clients truncate that text when they
#: render it into the prompt — the tail was being cut off in every mode).
#: A note is only rendered when the caller asks for that category, so it is
#: paid per response instead of on every prompt; it is still budgeted, see
#: ``test_category_notes_fit_their_own_budget``.
#:
#: These notes must stay **mode-neutral**. ``ACTIONS`` is populated regardless
#: of tool-mode pruning and ``_help`` has no mode awareness, so a bare
#: ``mem_batch_add`` here would name a tool that does not exist in core mode.
#: Always spell non-core calls as ``mem_do(action="...", params={...})``.
_CATEGORY_NOTES: dict[str, str] = {
    "sessions": """\
Starting a session with an agent_id changes where writes go.

mem_do(action="session_start", params={"agent_id": "planner"}) binds that
agent. Four paths then inherit the agent scope when they are called without an
explicit namespace=: mem_add, mem_index, and the batch_add and fetch actions.
They write to "agent-runtime:planner". Other ingestion paths do NOT inherit it
— the Notion and Obsidian importers keep defaulting to their own "notion" and
"obsidian" namespaces.
Omitting agent_id, or passing the reserved id "default", binds no agent: writes
route exactly as they would with no session at all.

To write somewhere else while an agent is bound, pass namespace= on the write
call itself (e.g. namespace="shared"). namespace= on session_start re-points
the session *record* only — it does not become the inherited write namespace,
so it will not redirect a single later write.

Reads are not symmetric. mem_search reads from the current namespace, so a note
written into an agent scope is not in its default results; use
mem_do(action="agent_search") to read inside the agent scope.

That read resolves its scope in order: an explicit agent_id parameter, then the
session's bound agent, then the current namespace, and only if none of those is
set does it search unpinned. An unbound session therefore does not by itself
mean an unpinned search — it removes the second step, nothing more.
""",
    "multi_agent": """\
Typical flow: mem_do(action="session_start", params={"agent_id": "planner"})
→ mem_do(action="agent_search") or mem_do(action="agent_share")
→ mem_do(action="session_end"). Full parameter forms below.
mem_do(action="agent_register", params={"agent_id": "planner"}) is optional —
it records agent metadata and sets up shared scopes; session_start and scoped
search work without it.

Per-project teams (several teams against one server): prefix the project onto
the agent_id (agent_id="projA-planner") for private memory, and keep
team-shared notes in a per-project bucket —
  write: mem_do(action="agent_share",
                params={"chunk_id": "...", "target": "shared:projA"})
  read:  mem_do(action="agent_search",
                params={"query": "...", "shared_namespace": "shared:projA"})
See ADR-0028.

agent_search with no agent_id, no session agent and no current namespace does
not fail, and it does not sweep every namespace either: it runs UNPINNED, and
the system-namespace filter then hides, by default, exactly the scopes you were
probably after ("agent-runtime:" and "archive:"), while default / shared /
custom namespaces are still searched. That filter is an exclusion list the
operator can change (search.system_namespace_prefixes): extra prefixes hide
more, an empty list hides nothing, so on a customized server the scopes an
unpinned search reaches may be wider OR narrower than the default. Pass
agent_id, or start an agent-bound session first, when you mean to search one
agent's scope.

"agent-runtime:<id>" is a routing scope, not an access boundary — anything
that can call the server can read it.
""",
}


def _help(category: str | None = None) -> str:
    """Generate action catalog."""
    if category:
        actions = {k: v for k, v in ACTIONS.items() if v.category == category}
        if not actions:
            cats = sorted({v.category for v in ACTIONS.values()})
            return f"Unknown category '{category}'. Available: {', '.join(cats)}"
        lines = [f"## {category} ({len(actions)} actions)\n"]
        note = _CATEGORY_NOTES.get(category)
        if note:
            lines.append(note)
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
