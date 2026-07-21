"""Server-level instructions string, built per tool mode.

Passed to ``FastMCP(instructions=...)`` and surfaced to every MCP client
on the ``initialize`` response. This is the only documentation surface
most LLMs see before deciding which tool to call — keep it tight and
focused on workflow recognition, not a full reference. Per-tool
docstrings cover argument-level detail, and the long-form narrative
lives in ``_CATEGORY_NOTES`` (``server/tools/meta.py``), reachable in
every mode through ``mem_do(action="help", params={"category": ...})``.

Clients truncate this text when they render it into the model's prompt,
so it is budgeted: see ``test_instructions_fit_the_client_render_budget``
in ``tests/test_server_instructions.py`` for the evidence and the number.
Anything that does not fit belongs in a category note, which is fetched
on demand rather than paid on every prompt.

The text MUST match the tool surface the client actually sees (#1608):
``MEMTOMEM_TOOL_MODE=core`` (the default) exposes only the core tools,
so every multi-agent/session call is shown in its ``mem_do(action=...)``
routed form there; ``standard`` exposes the session/crud packs directly
but still routes ``multi_agent`` tools through ``mem_do``; ``full``
exposes every MCP-registered tool directly, while registry-only actions
(e.g. ``ingest``) stay dispatcher-only in that mode too. Action name =
tool name minus the ``mem_`` prefix (see ``server/tool_registry.py``).

When changing these strings, also update the pin test
``tests/test_server_instructions.py`` so that renamed tools, removed
namespace conventions, or mode/exposure mismatches don't silently drift.
"""

from __future__ import annotations

VALID_TOOL_MODES: tuple[str, ...] = ("core", "standard", "full")

_HEADER = """\
memtomem — markdown-first long-term memory MCP server.
"""

# The session summary is deliberately mode-neutral prose so it is paid once
# instead of three times: ``mem_do`` exists in every mode, so the pointer at
# the end resolves everywhere. The routing rules it summarizes are a property
# of the session binding, not of the calling form.
_SINGLE_AGENT = """\
Default usage (single-agent — the common case):
- mem_add to record a note, mem_search to find one. That's it.
- Notes go to the "default" namespace; agent_id and namespace=
  can be ignored unless you're orchestrating multiple agents.

Sessions decide where writes land. Starting one with an agent_id opts into
an isolated "agent-runtime:<id>" write scope that a default mem_search does
not read; starting one without agent_id binds no agent, and mem_add then
routes exactly as it would with no session at all. If a note seems to have
gone missing, read
mem_do(action="help", params={"category": "sessions"}).
"""

_NAMESPACES = """\
Namespace conventions:
  default              single-agent / pre-multi-agent
  agent-runtime:<id>   per-agent routing scope (not an access boundary)
  shared               cross-agent shared scope
Pass explicit namespace= only when overriding the derived value.
"""

_FOOTER = """\
When in doubt, default to mem_add / mem_search with no extras.
"""

# Each mode section is a recipe plus a pointer. The narrative that used to be
# inlined here (per-project teams, the session-bound write contract, the
# pitfalls) now lives in ``_CATEGORY_NOTES`` in ``server/tools/meta.py``.
# ``{core_count}`` is interpolated from the exposed set rather than typed, so
# the number cannot drift from the surface it describes; every literal ``{``
# in these templates is therefore doubled.
_CORE_MODE = """\
This server runs in the default "core" tool mode: only the {core_count} core
tools are exposed individually. Everything else goes through the dispatcher:
mem_do(action="<tool name minus mem_>", params={{...}}). mem_do(action="help")
lists every action by category, including ones with no individual tool in any
mode (e.g. "ingest"). Set MEMTOMEM_TOOL_MODE=standard|full for more.

Multi-agent (only when the user asks for per-agent isolation or sharing):
mem_do(action="session_start", params={{"agent_id": "planner"}})
→ mem_do(action="agent_search") or mem_do(action="agent_share")
→ mem_do(action="session_end"). agent_register is optional. Read
mem_do(action="help", params={{"category": "multi_agent"}}) first.
"""

_STANDARD_MODE = """\
This server runs in "standard" tool mode: the {core_count} core tools plus the
crud / namespace / tags / sessions / scratch / relations / schedule packs are
exposed individually. Everything else — including the multi-agent tools —
goes through mem_do(action="<name>", params={{...}}); mem_do(action="help")
lists them. Set MEMTOMEM_TOOL_MODE=full to expose everything individually.

Multi-agent (only when the user asks for per-agent isolation or sharing):
mem_session_start(agent_id="planner")
→ mem_do(action="agent_search") or mem_do(action="agent_share")
→ mem_session_end(). agent_register is optional. Read
mem_do(action="help", params={{"category": "multi_agent"}}) first.
"""

_FULL_MODE = """\
This server runs in "full" tool mode: every registered MCP tool is exposed
individually. A few actions exist only in the registry (e.g. "ingest") and
still need mem_do(action="<name>", params={{...}}); mem_do(action="help")
lists everything.

Multi-agent (only when the user asks for per-agent isolation or sharing):
mem_session_start(agent_id="planner")
→ mem_agent_search(query="...") or mem_agent_share(chunk_id="...",
target="shared") → mem_session_end(). mem_agent_register is optional. Read
mem_do(action="help", params={{"category": "multi_agent"}}) first.
"""

_MODE_SECTIONS: dict[str, str] = {
    "core": _CORE_MODE,
    "standard": _STANDARD_MODE,
    "full": _FULL_MODE,
}


def build_instructions(tool_mode: str, *, core_count: int) -> str:
    """Return the instructions text matching ``tool_mode``'s tool surface.

    Unknown modes fall back to ``core`` — mirroring the pruning logic in
    ``server/__init__.py``, where anything that isn't ``standard`` or
    ``full`` prunes down to the core tool set.

    ``core_count`` is the size of the individually-exposed core tool set
    (``len(server._CORE_TOOLS)``) and is passed in rather than imported:
    the sole production caller is the ``FastMCP(...)`` construction in
    ``server/__init__.py``, which runs while that module is still
    executing, so importing back into it would read a half-built module.

    Section order matters: the mode-specific block is the longest and the
    least universally useful, so it goes last-but-footer. Previously
    ``_NAMESPACES`` and ``_FOOTER`` were appended after it and were the
    first things a client's truncation removed — in every mode.
    """
    mode = tool_mode if tool_mode in _MODE_SECTIONS else "core"
    return "\n".join(
        (
            _HEADER,
            _SINGLE_AGENT,
            _NAMESPACES,
            _MODE_SECTIONS[mode].format(core_count=core_count),
            _FOOTER,
        )
    )
