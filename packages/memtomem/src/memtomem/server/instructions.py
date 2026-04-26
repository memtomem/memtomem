"""Server-level instructions string.

Passed to ``FastMCP(instructions=...)`` and surfaced to every MCP client
on the ``initialize`` response. This is the only documentation surface
most LLMs see before deciding which tool to call — keep it tight and
focused on workflow recognition, not a full reference. Per-tool
docstrings cover argument-level detail.

When changing this string, also update the pin test
``tests/test_server_instructions.py`` so that renamed tools or removed
namespace conventions don't silently drift.
"""

from __future__ import annotations

INSTRUCTIONS: str = """\
memtomem — markdown-first long-term memory MCP server.

Default usage (single-agent — the common case):
- mem_add to record a note, mem_search to find one. That's it.
- Notes go to the "default" namespace; agent_id and namespace=
  can be ignored unless you're orchestrating multiple agents.

Multi-agent workflow (only when the user asks for per-agent
isolation or shared knowledge between agents):
1. Register each agent once: mem_agent_register(agent_id="planner")
2. Start a session per agent run:
     mem_session_start(agent_id="planner")
   The session record's namespace auto-derives to
   "agent-runtime:planner" — no explicit namespace= needed.
3. Search / share inside the agent scope:
     mem_agent_search(query=..., include_shared=True)
     mem_agent_share(chunk_id=..., target="shared")   # copy chunk to shared scope
4. End the session: mem_session_end(summary=...)

Namespace conventions:
  default                 single-agent / pre-multi-agent
  agent-runtime:<id>      per-agent isolated scope
  shared:                 cross-agent shared scope
Pass explicit namespace= only when overriding the derived value.

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

When in doubt, default to mem_add / mem_search with no extras.
"""
