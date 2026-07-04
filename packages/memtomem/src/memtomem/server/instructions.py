"""Server-level instructions string, built per tool mode.

Passed to ``FastMCP(instructions=...)`` and surfaced to every MCP client
on the ``initialize`` response. This is the only documentation surface
most LLMs see before deciding which tool to call — keep it tight and
focused on workflow recognition, not a full reference. Per-tool
docstrings cover argument-level detail.

The text MUST match the tool surface the client actually sees (#1608):
``MEMTOMEM_TOOL_MODE=core`` (the default) exposes only the 9 core tools,
so every multi-agent/session call is shown in its ``mem_do(action=...)``
routed form there; ``standard`` exposes the session/crud packs directly
but still routes ``multi_agent`` tools through ``mem_do``; ``full``
shows direct calls for everything. Action name = tool name minus the
``mem_`` prefix (see ``server/tool_registry.py``).

When changing these strings, also update the pin test
``tests/test_server_instructions.py`` so that renamed tools, removed
namespace conventions, or mode/exposure mismatches don't silently drift.
"""

from __future__ import annotations

VALID_TOOL_MODES: tuple[str, ...] = ("core", "standard", "full")

_HEADER = """\
memtomem — markdown-first long-term memory MCP server.
"""

_SINGLE_AGENT = """\
Default usage (single-agent — the common case):
- mem_add to record a note, mem_search to find one. That's it.
- Notes go to the "default" namespace; agent_id and namespace=
  can be ignored unless you're orchestrating multiple agents.
"""

_NAMESPACES = """\
Namespace conventions:
  default                 single-agent / pre-multi-agent
  agent-runtime:<id>      per-agent isolated scope
  shared                  cross-agent shared scope
Pass explicit namespace= only when overriding the derived value.
"""

_FOOTER = """\
When in doubt, default to mem_add / mem_search with no extras.
"""

_CORE_MODE = """\
This server runs in the default "core" tool mode: only the 9 core tools
are exposed individually. Every other action is invoked through the
mem_do dispatcher — mem_do(action="<name>", params={...}) where <name>
is the tool name minus the "mem_" prefix. mem_do(action="help") lists
every available action grouped by category (including actions that have
no individual tool in any mode, e.g. "ingest"). Set
MEMTOMEM_TOOL_MODE=standard|full on the server to expose more tools
individually.

Multi-agent workflow (only when the user asks for per-agent
isolation or shared knowledge between agents):
1. Register each agent once:
     mem_do(action="agent_register", params={"agent_id": "planner"})
2. Start a session per agent run:
     mem_do(action="session_start", params={"agent_id": "planner"})
   The session record's namespace auto-derives to
   "agent-runtime:planner" — no explicit namespace= needed.
3. Search / share inside the agent scope:
     mem_do(action="agent_search", params={"query": "...", "include_shared": True})
     mem_do(action="agent_share", params={"chunk_id": "...", "target": "shared"})
4. End the session: mem_do(action="session_end", params={"summary": "..."})

Per-project teams (multiple teams against one server): prefix the project
onto the agent_id (agent_id="projA-planner") for private memory, and keep
team-shared notes in a per-project bucket — write with
mem_do(action="agent_share", params={"chunk_id": "...", "target": "shared:projA"}),
read with
mem_do(action="agent_search", params={"query": "...", "shared_namespace": "shared:projA"}).
See ADR-0028.

Session-bound write contract:
- After mem_do(action="session_start", params={"agent_id": "..."}),
  subsequent mem_add and mem_do(action="batch_add") calls without an
  explicit namespace= argument automatically write to
  "agent-runtime:<id>" — the session's agent scope. Pass namespace=
  explicitly to write somewhere else (e.g. namespace="shared").
- mem_search still reads from current_namespace by default; use
  mem_do(action="agent_search") to read inside the agent scope.
  (Symmetric search-side support is tracked separately.)

Common pitfalls:
- session_start without agent_id falls back to the "default"
  namespace — pass agent_id whenever you want isolation.
- agent_search needs an active session (or current_agent_id);
  run the session_start action first.
"""

_STANDARD_MODE = """\
This server runs in "standard" tool mode: core tools plus the session /
crud / namespace / tags / scratch / relations / schedule packs are
exposed individually. Multi-agent tools (agent_register, agent_search,
agent_share) are NOT individual tools here — invoke them through
mem_do(action="<name>", params={...}). mem_do(action="help") lists every
available action. Set MEMTOMEM_TOOL_MODE=full to expose everything
individually.

Multi-agent workflow (only when the user asks for per-agent
isolation or shared knowledge between agents):
1. Register each agent once:
     mem_do(action="agent_register", params={"agent_id": "planner"})
2. Start a session per agent run:
     mem_session_start(agent_id="planner")
   The session record's namespace auto-derives to
   "agent-runtime:planner" — no explicit namespace= needed.
3. Search / share inside the agent scope:
     mem_do(action="agent_search", params={"query": "...", "include_shared": True})
     mem_do(action="agent_share", params={"chunk_id": "...", "target": "shared"})
4. End the session: mem_session_end(summary="...")

Per-project teams (multiple teams against one server): prefix the project
onto the agent_id (agent_id="projA-planner") for private memory, and keep
team-shared notes in a per-project bucket — write with
mem_do(action="agent_share", params={"chunk_id": "...", "target": "shared:projA"}),
read with
mem_do(action="agent_search", params={"query": "...", "shared_namespace": "shared:projA"}).
See ADR-0028.

Session-bound write contract:
- After mem_session_start(agent_id="..."), subsequent mem_add and
  mem_batch_add calls without an explicit namespace= argument
  automatically write to "agent-runtime:<id>" — the session's
  agent scope. Pass namespace= explicitly to write somewhere
  else (e.g. namespace="shared").
- mem_search still reads from current_namespace by default; use
  mem_do(action="agent_search") to read inside the agent scope.
  (Symmetric search-side support is tracked separately.)

Common pitfalls:
- mem_session_start() without agent_id falls back to the "default"
  namespace — pass agent_id whenever you want isolation.
- agent_search needs an active session (or current_agent_id);
  call mem_session_start first.
"""

_FULL_MODE = """\
Multi-agent workflow (only when the user asks for per-agent
isolation or shared knowledge between agents):
1. Register each agent once: mem_agent_register(agent_id="planner")
2. Start a session per agent run:
     mem_session_start(agent_id="planner")
   The session record's namespace auto-derives to
   "agent-runtime:planner" — no explicit namespace= needed.
3. Search / share inside the agent scope:
     mem_agent_search(query="...", include_shared=True)
     mem_agent_share(chunk_id="...", target="shared")   # copy chunk to shared scope
4. End the session: mem_session_end(summary="...")

Per-project teams (multiple teams against one server): prefix the project
onto the agent_id (agent_id="projA-planner") for private memory, and keep
team-shared notes in a per-project bucket — write with
mem_agent_share(chunk_id="...", target="shared:projA"), read with
mem_agent_search(query="...", shared_namespace="shared:projA"). See ADR-0028.

Session-bound write contract:
- After mem_session_start(agent_id="..."), subsequent mem_add and
  mem_batch_add calls without an explicit namespace= argument
  automatically write to "agent-runtime:<id>" — the session's
  agent scope. Pass namespace= explicitly to write somewhere
  else (e.g. namespace="shared").
- mem_search still reads from current_namespace by default;
  use mem_agent_search to read inside the agent scope.
  (Symmetric search-side support is tracked separately.)

Common pitfalls:
- mem_session_start() without agent_id falls back to the "default"
  namespace — pass agent_id whenever you want isolation.
- mem_agent_search needs an active session (or current_agent_id);
  call mem_session_start first.
"""

_MODE_SECTIONS: dict[str, str] = {
    "core": _CORE_MODE,
    "standard": _STANDARD_MODE,
    "full": _FULL_MODE,
}


def build_instructions(tool_mode: str) -> str:
    """Return the instructions text matching ``tool_mode``'s tool surface.

    Unknown modes fall back to ``core`` — mirroring the pruning logic in
    ``server/__init__.py``, where anything that isn't ``standard`` or
    ``full`` prunes down to the core tool set.
    """
    mode = tool_mode if tool_mode in _MODE_SECTIONS else "core"
    return "\n".join(
        (
            _HEADER,
            _SINGLE_AGENT,
            _MODE_SECTIONS[mode],
            _NAMESPACES,
            _FOOTER,
        )
    )
